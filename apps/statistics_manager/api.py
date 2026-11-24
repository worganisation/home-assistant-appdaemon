"""Small native Home Assistant API adapter using AppDaemon's authenticated plugin."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from .model import Policy, Record, Snapshot, record, text

if TYPE_CHECKING:
    from appdaemon.plugins.hass.hassplugin import HassPlugin

FLOW = "/api/config/config_entries/flow"
OPTIONS = "/api/config/config_entries/options/flow"


class HomeAssistant:
    """Keep transport and flow details outside the reconciliation engine."""

    def __init__(self, plugin: HassPlugin) -> None:
        self.plugin = plugin

    async def ws(self, command: str, **data: object) -> object:
        """Require an explicit successful WebSocket result."""
        response = await self.plugin.websocket_send_json(
            timeout=30,
            silent=True,
            type=command,
            **data,
        )
        result = record(response)
        if result.get("ad_status") == "TERMINATING":
            raise asyncio.CancelledError

        if result.get("success") is not True:
            raise RuntimeError(f"Home Assistant command failed: {command}")
        return result.get("result")

    async def rest(
        self,
        method: Literal["get", "post", "delete"],
        path: str,
        **data: object,
    ) -> Record:
        """Read a native REST result and propagate shutdown cancellation.

        Args:
            method: HTTP method for the native call.
            path: Home Assistant API path.
            **data: Request fields forwarded to AppDaemon.

        Returns:
            The response object returned by Home Assistant.

        Raises:
            asyncio.CancelledError: The worker was cancelled during the request.
            TypeError: AppDaemon returned a non-object response.
        """
        response = await self.plugin.http_method(method, path, timeout=30, **data)
        if (task := asyncio.current_task()) is not None and task.cancelling():
            raise asyncio.CancelledError

        return record(response)

    async def snapshot(self) -> Snapshot:
        """Read full registries and states before making lifecycle decisions."""
        config = record(await self.ws("get_config"))
        if config.get("state") != "RUNNING":
            raise RuntimeError("Home Assistant is not running yet")
        entities = await self.ws("config/entity_registry/list")
        entries = await self.ws("config_entries/get")
        labels = await self.ws("config/label_registry/list")
        states = await self.ws("get_states")
        return Snapshot.parse(entities, entries, labels, states)

    @staticmethod
    def form(flow: Record, step: str) -> str:
        """Check the installed flow's current stage and validation result."""
        if (
            flow.get("type") != "form"
            or flow.get("step_id") != step
            or flow.get("errors")
        ):
            raise ValueError(f"Statistics flow did not reach {step}: {flow.get('type')}")
        return text(flow, "flow_id")

    async def create(self, policy: Policy, source_id: str, entity_id: str) -> str:
        """Drive the native three-stage Statistics creation flow."""
        flow = await self.rest(
            "post",
            FLOW,
            handler="statistics",
            show_advanced_options=True,
        )
        flow_id = self.form(flow, "user")
        flow = await self.rest(
            "post",
            f"{FLOW}/{flow_id}",
            name=policy.marker(source_id),
            entity_id=entity_id,
        )
        self.form(flow, "state_characteristic")
        flow = await self.rest(
            "post",
            f"{FLOW}/{flow_id}",
            state_characteristic=policy.characteristic,
        )
        self.form(flow, "options")
        flow = await self.rest("post", f"{FLOW}/{flow_id}", **policy.options(entity_id))
        if flow.get("type") != "create_entry":
            raise ValueError("Statistics creation was not confirmed")
        return text(record(flow.get("result")), "entry_id")

    async def configure(self, policy: Policy, entry_id: str, source: str) -> bool:
        """Read native options and submit only when the policy differs."""
        flow = await self.rest(
            "post",
            OPTIONS,
            handler=entry_id,
            show_advanced_options=True,
        )
        flow_id = self.form(flow, "init")
        schema = flow.get("data_schema")
        if not isinstance(schema, list):
            raise TypeError("Statistics options schema is missing")
        current: Record = {}
        for raw in schema:
            field = record(raw)
            description = record(field.get("description", {}))
            current[text(field, "name")] = description.get(
                "suggested_value",
                field.get("default"),
            )
        expected = policy.options(source)
        age = current.get("max_age")
        duration = 0.0
        if isinstance(age, dict):
            age_values = record(age)
            for unit, multiplier in (
                ("days", 86400),
                ("hours", 3600),
                ("minutes", 60),
                ("seconds", 1),
            ):
                value = age_values.get(unit, 0)
                if not isinstance(value, (int, float)):
                    raise TypeError("Invalid Statistics duration")
                duration += value * multiplier
        changed = (
            duration != policy.hours * 3600
            or current.get("sampling_size") is not None
            or any(
                current.get(key) != value
                for key, value in expected.items()
                if key != "max_age"
            )
        )
        if not changed:
            await self.rest("delete", f"{OPTIONS}/{flow_id}")
            return False
        result = await self.rest("post", f"{OPTIONS}/{flow_id}", **expected)
        if result.get("type") != "create_entry":
            raise ValueError("Statistics options update was not confirmed")
        return True

    async def delete(self, entry_id: str) -> None:
        """Delete the exact owned config entry through the native REST API."""
        result = await self.rest("delete", f"/api/config/config_entries/entry/{entry_id}")
        if result.get("require_restart"):
            raise RuntimeError(
                "Helper removal requires user-approved Home Assistant restart",
            )
