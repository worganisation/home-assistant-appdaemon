"""AppDaemon lifecycle, registry-event coalescing and diagnostics."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from appdaemon.plugins.hass.hassapi import Hass
from appdaemon.plugins.hass.hassplugin import HassPlugin

from .api import HomeAssistant
from .manager import Manager
from .model import Policy
from .store import Store

if TYPE_CHECKING:
    from collections.abc import Awaitable


class StatisticsSensorManager(Hass):
    """Maintain native helpers from direct source labels without HA restarts."""

    async def initialize(self) -> None:
        """Reuse AppDaemon's authenticated connection and start one reconciliation worker."""
        plugin = self.AD.plugins.get_plugin_object(self.get_namespace())
        if not isinstance(plugin, HassPlugin):
            raise TypeError("Statistics manager requires the HASS plugin")
        policy = Policy(
            namespace=str(self.args.get("manager_id", self.name)),
            policy_id=str(self.args.get("policy_id", "change72h")),
            label=str(self.args.get("label", "trend72Hours")),
            hours=int(self.args.get("hours", 72)),
            precision=int(self.args.get("precision", 2)),
        )
        self.interval = float(self.args.get("reconcile_interval", 3600))
        self.debounce = float(self.args.get("debounce", 2))
        self.minimum_coverage = float(self.args.get("minimum_coverage", 0.8))
        self.last_success = ""
        if not 0 <= self.minimum_coverage <= 1:
            raise ValueError("Minimum coverage must be between zero and one")
        if self.interval <= 0 or self.debounce <= 0:
            raise ValueError("Reconciliation and debounce intervals must be positive")
        self.manager = Manager(
            HomeAssistant(plugin),
            Store(
                Path(
                    str(
                        self.args.get(
                            "storage_path",
                            "/data/statistics_manager/ownership.json",
                        ),
                    ),
                ),
            ),
            policy,
            dry_run=bool(self.args.get("dry_run", False)),
        )
        if self.manager.store.corrupt:
            self.log(
                "Ownership metadata is invalid; recovering native provenance",
                level="WARNING",
            )
        self.wake = asyncio.Event()
        self.loop = asyncio.get_running_loop()
        for event in (
            "entity_registry_updated",
            "label_registry_updated",
            "plugin_started",
            "homeassistant_started",
        ):
            await cast("Awaitable[str]", self.listen_event(self.changed, event))
        self.worker = asyncio.create_task(self.run())

    def changed(self, event_type: str, data: dict[str, object], **kwargs: object) -> None:
        """Coalesce callbacks without performing network work on callback threads."""
        del event_type, data, kwargs
        self.loop.call_soon_threadsafe(self.wake.set)

    async def run(self) -> None:
        """Retry failed passes with bounded backoff and fresh authoritative discovery."""
        failures = 0
        while True:
            self.wake.clear()
            try:
                result = await self.manager.reconcile()
                failures = failures + 1 if result["warnings"] else 0
                if not failures:
                    self.last_success = datetime.now(UTC).isoformat()
                await cast(
                    "Awaitable[object]",
                    self.set_state(
                        "sensor.statistics_sensor_manager",
                        state="warning" if result["warnings"] else "ok",
                        attributes={
                            **result,
                            "last_success": self.last_success,
                            "minimum_coverage": self.minimum_coverage,
                        },
                    ),
                )
                if result["changes"] or result["warnings"]:
                    self.log("Statistics reconciliation: %s", result)
                delay = (
                    min(300, 5 * 2 ** min(failures - 1, 6)) if failures else self.interval
                )
            except Exception as exc:
                failures += 1
                delay = min(300, 5 * 2 ** min(failures - 1, 6))
                self.log(
                    "Statistics reconciliation failed (%s): %s; retry in %ss",
                    type(exc).__name__,
                    exc,
                    delay,
                    level="WARNING",
                )
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
                await asyncio.sleep(self.debounce)
            except TimeoutError:
                pass

    async def terminate(self) -> None:
        """Cancel the worker before a replacement app starts."""
        if worker := getattr(self, "worker", None):
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
