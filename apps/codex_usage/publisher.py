"""MQTT discovery and independently expiring quota/activity readings."""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from pydantic import TypeAdapter

from .models import STALE_SECONDS, Configuration, Profile, Reading, bucket_key

if TYPE_CHECKING:
    from pathlib import Path

    from paho.mqtt.properties import Properties
    from paho.mqtt.reasoncodes import ReasonCode


@dataclass
class AccountState:
    """Cached public values and monotonic freshness deadlines."""

    profile: Profile
    readings: dict[str, Reading] = field(default_factory=dict)
    successes: dict[str, float] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    last_success: str | None = None
    authentication_failed: bool = False


class Publisher:
    """Own a single MQTT client; mutate caches only on the asyncio loop."""

    def __init__(self, config: Configuration, root: Path) -> None:
        self.config = config
        self.root = root
        self.loop = asyncio.get_running_loop()
        self.accounts: dict[str, AccountState] = {}
        self.connected = False
        self.closed = False
        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2,
            client_id="appdaemon-codex-usage",
        )
        self.client.username_pw_set(
            config.mqtt_username,
            config.mqtt_password.get_secret_value(),
        )
        self.client.will_set(self.topic("availability"), "offline", qos=1, retain=True)
        self.client.on_connect = self._connected
        self.client.on_disconnect = self._disconnected

    def topic(self, suffix: str) -> str:
        """Return a namespaced MQTT topic."""
        return f"{self.config.mqtt_base_topic}/{suffix}"

    @staticmethod
    def object_id(profile: str, key: str) -> str:
        """Generate stable collision-resistant entity IDs."""
        return f"codex_{bucket_key(profile)}_{key}"

    def start(self) -> None:
        """Connect in the MQTT thread, including retries when initially offline."""
        self.client.connect_async(
            self.config.mqtt_host,
            self.config.mqtt_port,
            keepalive=30,
        )
        self.client.loop_start()

    def add(self, profile: Profile) -> None:
        """Register an account without inheriting freshness across restarts."""
        self.accounts[profile.name] = AccountState(profile)
        self.refresh()

    def _connected(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _flags: mqtt.ConnectFlags,
        reason: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        if not reason.is_failure and not self.closed:
            self.loop.call_soon_threadsafe(self._republish)

    def _disconnected(
        self,
        _client: mqtt.Client,
        _userdata: object,
        _flags: mqtt.DisconnectFlags,
        _reason: ReasonCode,
        _properties: Properties | None,
    ) -> None:
        if not self.closed:
            self.loop.call_soon_threadsafe(self._mark_disconnected)

    def _mark_disconnected(self) -> None:
        self.connected = False

    def _publish(self, topic: str, payload: str) -> None:
        if self.connected:
            self.client.publish(topic, payload, qos=1, retain=True)

    def _republish(self) -> None:
        if self.closed:
            return

        self.connected = True
        for name, state in self.accounts.items():
            for reading in state.readings.values():
                self._reading(name, reading)
        self.refresh()
        self._publish(self.topic("availability"), "online")

    def success(self, name: str, group: str, readings: list[Reading]) -> None:
        """Replace a successful group and retire windows absent from its snapshot."""
        state = self.accounts[name]
        manifest = self.root / f"discovery-{name}-{group}.json"
        previous = (
            TypeAdapter(list[str]).validate_json(manifest.read_text())
            if manifest.exists()
            else []
        )
        keys = {reading.key for reading in readings}
        for key in set(previous) - keys:
            self._publish(self._discovery_topic(name, key), "")
            self._publish(self.topic(f"{name}/{key}"), "")
            state.readings.pop(key, None)

        if self.connected:
            temporary = manifest.with_suffix(".tmp")
            temporary.write_text(json.dumps(sorted(keys)))
            temporary.replace(manifest)

        state.successes[group] = time.monotonic()
        state.failures.pop(group, None)
        state.last_success = datetime.now(UTC).isoformat()
        state.authentication_failed = False
        for reading in readings:
            state.readings[reading.key] = reading
            self._reading(name, reading)
        self.refresh()

    def failure(self, name: str, group: str, *, authentication: bool = False) -> None:
        """Preserve good values while tracking endpoint-specific health."""
        state = self.accounts[name]
        state.failures[group] = "needs_login" if authentication else "poll_failed"
        if authentication:
            state.authentication_failed = True
        self.refresh()

    def refresh(self) -> None:
        """Publish freshness without treating cached values as new observations."""
        now = time.monotonic()
        for name, state in self.accounts.items():
            stale = False
            for group in ("quota", "tokens"):
                observed = state.successes.get(group)
                fresh = observed is not None and now - observed < STALE_SECONDS
                available = fresh and not state.authentication_failed
                stale = stale or not fresh
                self._publish(
                    self.topic(f"{name}/{group}/availability"),
                    "online" if available else "offline",
                )

            status = (
                "needs_login"
                if state.authentication_failed
                else "degraded"
                if state.failures
                else "stale"
                if stale
                else "ok"
            )
            diagnostics = [
                Reading(
                    "status",
                    "Monitor status",
                    status,
                    "diagnostic",
                    attributes={
                        "quota_status": state.failures.get(
                            "quota",
                            "ok" if "quota" in state.successes else "pending",
                        ),
                        "tokens_status": state.failures.get(
                            "tokens",
                            "ok" if "tokens" in state.successes else "pending",
                        ),
                    },
                ),
                Reading(
                    "last_success",
                    "Last successful update",
                    state.last_success,
                    "diagnostic",
                    device_class="timestamp",
                ),
            ]
            for reading in diagnostics:
                state.readings[reading.key] = reading
                self._reading(name, reading)

    def _discovery_topic(self, name: str, key: str) -> str:
        return f"{self.config.mqtt_discovery_prefix}/sensor/{self.object_id(name, key)}/config"

    def _reading(self, name: str, reading: Reading) -> None:
        entity = self.object_id(name, reading.key)
        state_topic = self.topic(f"{name}/{reading.key}")
        availability: list[dict[str, str]] = [{"topic": self.topic("availability")}]
        if reading.group != "diagnostic":
            availability.append(
                {"topic": self.topic(f"{name}/{reading.group}/availability")},
            )

        discovery: dict[str, object] = {
            "name": reading.name,
            "unique_id": f"appdaemon_{entity}",
            "object_id": entity,
            "state_topic": state_topic,
            "value_template": "{{ value_json.value if value_json.value is not none else '' }}",
            "json_attributes_topic": state_topic,
            "json_attributes_template": "{{ value_json.attributes | tojson }}",
            "availability": availability,
            "availability_mode": "all",
            "device": {
                "identifiers": [f"appdaemon_codex_{bucket_key(name)}"],
                "name": f"Codex — {name}",
                "manufacturer": "OpenAI",
                "model": "Codex usage",
            },
        }
        discovery.update(
            {
                key: value
                for key, value in (
                    ("unit_of_measurement", reading.unit),
                    ("device_class", reading.device_class),
                    ("state_class", reading.state_class),
                )
                if value is not None
            },
        )
        if reading.group == "diagnostic":
            discovery["entity_category"] = "diagnostic"
        else:
            observed = self.accounts[name].successes.get(reading.group)
            age = time.monotonic() - observed if observed is not None else STALE_SECONDS
            discovery["expire_after"] = max(1, math.ceil(STALE_SECONDS - age))

        self._publish(self._discovery_topic(name, reading.key), json.dumps(discovery))
        self._publish(
            state_topic,
            json.dumps({"value": reading.value, "attributes": reading.attributes}),
        )

    async def close(self) -> None:
        """Flush offline availability before stopping the network thread."""
        self.closed = True
        if self.connected:
            result = self.client.publish(
                self.topic("availability"),
                "offline",
                qos=1,
                retain=True,
            )
            await asyncio.to_thread(result.wait_for_publish, timeout=3)

        self.client.disconnect()
        await asyncio.to_thread(self.client.loop_stop)
