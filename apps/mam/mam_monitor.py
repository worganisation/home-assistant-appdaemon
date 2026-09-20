"""Publish a read-only MAM monitor and deduplicated actionable notifications."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import RLock
from typing import Any

import appdaemon.plugins.hass.hassapi as hass
import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from mam.clients import (
    MAM_INTERVAL,
    QBT_INTERVAL,
    MamClient,
    PollError,
    QbtClient,
    load_private,
    save_private,
    timestamp,
)
from mam.model import account_snapshot, assess, number, reconcile_torrents
from mam.sensors import SENSORS

DETAIL_LIMIT = 200
MAX_RESERVE = 1024
DAY_SECONDS = 86400
MESSAGE_LIMIT = 8
STALE_SECONDS = {"mam": 5400, "qbt": 900}
TRANSIENT_ISSUES = {"tracker_error", "not_seeding", "connectable"}


class MamMonitor(hass.Hass):
    """Collect independent snapshots, preserving history and source freshness."""

    mqtt_client: Any
    state: dict[str, Any]

    def initialize(self) -> None:
        """Restore durable schedules and initialize independent MQTT availability."""
        self.lock = RLock()
        self.directory = Path(self.args.get("state_directory", "/data/mam"))
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.state_path = self.directory / "monitor.json"
        self.state = load_private(self.state_path)
        self.state.setdefault("started", time.time())
        self.state.setdefault("torrents", {})
        self.state.setdefault("alerts", {})
        self.state.setdefault("account", {})
        for source in ("mam", "qbt"):
            self.state.setdefault(
                source,
                {"next_poll": 0, "last_success": 0, "error": "not_polled", "failures": 0},
            )
        self.mam = MamClient(self.directory, self.args.get("mam_id"))
        self.qbt = QbtClient(
            str(self.args["qbittorrent_url"]),
            str(self.args["qbittorrent_username"]),
            str(self.args["qbittorrent_password"]),
        )
        self.base_topic = str(self.args.get("mqtt_base_topic", "appdaemon/mam"))
        self.reserve_entity = str(self.args["reserve_entity"])
        self.limit_entity = str(self.args["limit_override_entity"])
        self.mqtt_client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id="appdaemon-mam-monitor",
        )
        self.mqtt_client.username_pw_set(
            str(self.args["mqtt_username"]),
            str(self.args["mqtt_password"]),
        )
        self.mqtt_client.will_set(
            f"{self.base_topic}/availability",
            "offline",
            qos=1,
            retain=True,
        )
        self.mqtt_client.on_connect = self.mqtt_connected
        self.mqtt_client.connect_async(
            str(self.args["mqtt_host"]),
            int(self.args.get("mqtt_port", 1883)),
            keepalive=60,
        )
        self.mqtt_client.loop_start()
        self.listen_state(self.helper_changed, self.reserve_entity)
        self.listen_state(self.helper_changed, self.limit_entity)
        self.run_every(self.tick, "now", 60)

    def terminate(self) -> None:
        """Mark the device unavailable on graceful shutdown."""
        if hasattr(self, "mqtt_client"):
            self.publish("availability", "offline")
            self.mqtt_client.disconnect()
            self.mqtt_client.loop_stop()

    def persist(self) -> None:
        """Commit monitor state before outbound polls or notifications."""
        save_private(self.state_path, self.state)

    def publish(self, topic: str, value: Any) -> None:
        """Publish retained QoS 1 payloads with JSON null for unknown numbers."""
        payload = value if isinstance(value, str) else json.dumps(value, allow_nan=False)
        self.mqtt_client.publish(
            f"{self.base_topic}/{topic}",
            payload,
            qos=1,
            retain=True,
        )

    def mqtt_connected(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        """Restore discovery and fresh state on every broker reconnect."""
        del client, userdata, flags, properties
        if reason_code != 0:
            return
        with self.lock:
            for spec in SENSORS:
                availability = [{"topic": f"{self.base_topic}/availability"}]
                sources = ("mam", "qbt") if spec.source == "both" else (spec.source,)
                availability.extend(
                    {"topic": f"{self.base_topic}/{source}/availability"}
                    for source in sources
                    if source != "monitor"
                )
                config: dict[str, Any] = {
                    "name": spec.name,
                    "unique_id": f"appdaemon_mam_{spec.key}",
                    "default_entity_id": f"sensor.mam_{spec.key}",
                    "state_topic": f"{self.base_topic}/state",
                    "value_template": "{{ value_json."
                    + spec.key
                    + " if value_json."
                    + spec.key
                    + " is not none else 'None' }}",
                    "availability": availability,
                    "availability_mode": "all",
                    "expire_after": 180,
                    "device": {
                        "identifiers": ["appdaemon_mam"],
                        "name": "MAM",
                        "manufacturer": "MyAnonamouse",
                        "model": "Account and seeding monitor",
                    },
                    "origin": {"name": "AppDaemon MAM monitor"},
                }
                config.update(
                    {
                        attr: value
                        for attr, value in (
                            ("unit_of_measurement", spec.unit),
                            ("device_class", spec.device_class),
                            ("state_class", spec.state_class),
                        )
                        if value is not None
                    },
                )
                if spec.key in {"details", "notices", "issues"}:
                    config["json_attributes_topic"] = f"{self.base_topic}/{spec.key}"
                topic = f"{self.args.get('mqtt_discovery_prefix', 'homeassistant')}/sensor/mam_{spec.key}/config"
                self.mqtt_client.publish(topic, json.dumps(config), qos=1, retain=True)
            self.render(time.time(), notify=False)
            self.publish("availability", "online")

    def helper_changed(
        self,
        entity: str,
        attribute: str,
        old: Any,
        new: Any,
        **kwargs: Any,
    ) -> None:
        """Recalculate immediately without another network request."""
        del entity, attribute, old, new, kwargs
        with self.lock:
            self.render(time.time())

    def tick(self, **kwargs: Any) -> None:
        """Poll due sources independently; persist reserved time before each attempt."""
        del kwargs
        with self.lock:
            for source, interval in (("mam", MAM_INTERVAL), ("qbt", QBT_INTERVAL)):
                now = time.time()
                record = self.state[source]
                if now < record["next_poll"]:
                    continue
                record["next_poll"] = now + interval
                self.persist()
                try:
                    self.poll_source(source, now)
                except Exception as error:
                    record["failures"] += 1
                    category = (
                        error.category
                        if isinstance(error, PollError)
                        else "request_or_payload_error"
                    )
                    record["error"] = category
                    retry = error.retry_after if isinstance(error, PollError) else 0
                    record["next_poll"] = now + max(
                        retry,
                        min(interval * 2 ** min(record["failures"] - 1, 4), 21600),
                    )
                    self.log("MAM monitor %s: %s", source, category, level="WARNING")
                self.persist()
            self.render(time.time())

    def poll_source(self, source: str, now: float) -> None:
        """Commit snapshots only after their complete validation succeeds."""
        record = self.state[source]
        if source == "mam":
            token, fingerprint = self.mam.credential()
            changed = fingerprint != record.get("credential_fingerprint")
            if record.get("auth_blocked") and not changed:
                raise PollError("authentication_failed")
            # Record the fingerprint only after a response, so transport failures
            # on a new credential cannot fall back to cookies from its predecessor.
            try:
                payload = self.mam.fetch(token, reset_cookies=changed)
                snapshot = account_snapshot(payload, self.args.get("response_paths", {}))
            except PollError as error:
                if error.category == "authentication_failed":
                    record.update(
                        {"auth_blocked": True, "credential_fingerprint": fingerprint},
                    )
                raise
            self.state["account"] = snapshot
            record.update({"auth_blocked": False, "credential_fingerprint": fingerprint})
        else:
            current = self.qbt.fetch(self.state["torrents"], now)
            self.state["torrents"] = reconcile_torrents(current, self.state["torrents"])
        record.update({"last_success": time.time(), "error": "", "failures": 0})

    def fresh(self, source: str, now: float) -> bool:
        """Authentication failure invalidates data immediately; stale caches expire."""
        record = self.state[source]
        return (
            bool(record["last_success"])
            and now - record["last_success"] <= STALE_SECONDS[source]
            and record["error"]
            not in {
                "authentication_failed",
                "session_not_configured",
                "invalid_session",
            }
        )

    def reserve(self) -> float:
        """Initialize a new helper once, preserving its restored value thereafter."""
        value = number(self.get_state(self.reserve_entity))
        if value is not None and not self.state.get("reserve_initialized"):
            if value == 0:
                self.call_service(
                    "input_number/set_value",
                    entity_id=self.reserve_entity,
                    value=2,
                )
                return 2
            self.state["reserve_initialized"] = True
            self.persist()
        if value is not None and 0 <= value <= MAX_RESERVE:
            self.state["reserve"] = value
        return self.state.get("reserve", 2)

    def render(self, now: float, *, notify: bool = True) -> None:
        """Publish scalar history separately from bounded volatile detail payloads."""
        mam_fresh, qbt_fresh = self.fresh("mam", now), self.fresh("qbt", now)
        override = int(number(self.get_state(self.limit_entity)) or 0)
        values, issues = assess(
            self.state["account"],
            self.state["torrents"],
            mam_fresh=mam_fresh,
            qbt_fresh=qbt_fresh,
            reserve=self.reserve(),
            limit_override=override,
        )
        for source, fresh in (("mam", mam_fresh), ("qbt", qbt_fresh)):
            record = self.state[source]
            self.publish(f"{source}/availability", "online" if fresh else "offline")
            values[f"{source}_health"] = record["error"] or ("ok" if fresh else "stale")
            values[f"{source}_last_success"] = timestamp(record["last_success"])
            values[f"{source}_next_poll"] = timestamp(record["next_poll"])
        torrents = sorted(
            self.state["torrents"].values(),
            key=lambda t: (not bool(t["issues"]), t["name"]),
        )
        omitted = max(0, len(torrents) - DETAIL_LIMIT)
        if omitted:
            issues["details_truncated"] = (
                "warning",
                f"Dashboard detail is limited to {DETAIL_LIMIT} torrents; aggregate checks include all torrents.",
            )
            if values["status"] == "no_detected_issues":
                values["status"] = "attention"
        values["details"] = len(torrents) if qbt_fresh else None
        notices = self.state["account"].get("notices", [])
        values["notices"] = (
            len(notices)
            if mam_fresh and self.state["account"].get("notices_available")
            else None
        )
        values["issues"] = len(issues)
        self.publish("state", {spec.key: values.get(spec.key) for spec in SENSORS})
        self.publish(
            "details",
            {
                "torrents": torrents[:DETAIL_LIMIT] if qbt_fresh else [],
                "omitted": omitted,
                "source": "qBittorrent estimates; not tracker-confirmed satisfaction",
            },
        )
        self.publish("notices", {"messages": notices if mam_fresh else []})
        self.publish(
            "issues",
            {
                "messages": [
                    {"severity": severity, "message": message}
                    for severity, message in list(issues.values())[:DETAIL_LIMIT]
                ],
                "total": len(issues),
            },
        )
        if notify:
            self.notify_issues(issues, now, mam_fresh=mam_fresh, qbt_fresh=qbt_fresh)
            self.persist()

    def notify_issues(  # noqa: C901 - independent issue debounce and recovery paths
        self,
        issues: dict[str, tuple[str, str]],
        now: float,
        *,
        mam_fresh: bool,
        qbt_fresh: bool,
    ) -> None:
        """Debounce transient issues and persist deduplication across restarts."""
        alerts = self.state["alerts"]
        messages = []
        critical = False
        for key, (severity, message) in issues.items():
            alert = alerts.setdefault(
                key,
                {"first_seen": now, "last_sent": 0, "severity": severity},
            )
            delay = 900 if key.split(":")[-1] in TRANSIENT_ISSUES else 0
            if key.endswith("_stale"):
                source = key.removesuffix("_stale")
                alert["first_seen"] = (
                    self.state[source]["last_success"] or self.state["started"]
                )
                delay = STALE_SECONDS[source]
            eligible = now - alert["first_seen"] >= delay
            due = (
                not alert["last_sent"]
                or severity != alert["severity"]
                or (severity == "critical" and now - alert["last_sent"] >= DAY_SECONDS)
            )
            if eligible and due:
                messages.append(message)
                critical |= severity == "critical"
                alert.update({"last_sent": now, "severity": severity})
        for key in list(alerts):
            if key in issues:
                continue
            dependencies_fresh = (
                qbt_fresh
                if key.startswith("torrent:") or key == "qbt_stale"
                else mam_fresh
            )
            if key == "budget":
                dependencies_fresh = mam_fresh and qbt_fresh
            if not dependencies_fresh:
                continue
            if alerts[key]["last_sent"]:
                messages.append(f"Resolved: {key.split(':')[-1].replace('_', ' ')}.")
            del alerts[key]
        if not messages:
            return
        self.persist()
        try:
            self.call_service(
                str(self.args.get("notify_script", "script.notify_will")).replace(
                    ".",
                    "/",
                    1,
                ),
                title="MAM monitoring",
                message="\n".join(messages[:MESSAGE_LIMIT])
                + (
                    f"\nPlus {len(messages) - MESSAGE_LIMIT} updates; see dashboard."
                    if len(messages) > MESSAGE_LIMIT
                    else ""
                ),
                notification_id="mam_monitor",
                mobile_notification_icon="mdi:book-alert",
                phone_only=True,
                sticky=True,
                persistent=critical,
                url="/mam-monitor/overview",
            )
        except Exception:
            self.log("MAM monitor notification delivery failed", level="WARNING")
