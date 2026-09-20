"""Stable MQTT sensor names and units for MAM monitoring."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Sensor:
    """Describe a sensor and the source freshness it requires."""

    key: str
    name: str
    source: str = "monitor"
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None


SENSORS = (
    Sensor("status", "Status"),
    Sensor("class", "Account class", "mam"),
    Sensor("ratio", "Account ratio", "mam", state_class="measurement"),
    Sensor("uploaded", "Credited upload", "mam", "GiB", "data_size", "total"),
    Sensor("downloaded", "Counted download", "mam", "GiB", "data_size", "total"),
    Sensor("bonus", "Bonus points", "mam", "points", state_class="measurement"),
    Sensor(
        "credit",
        "Available credit estimate",
        "mam",
        "GiB",
        "data_size",
        "measurement",
    ),
    Sensor("reserve", "Download reserve", unit="GiB", device_class="data_size"),
    Sensor(
        "outstanding",
        "Outstanding downloads",
        "qbt",
        "GiB",
        "data_size",
        "measurement",
    ),
    Sensor(
        "projected_credit",
        "Projected credit estimate",
        "both",
        "GiB",
        "data_size",
        "measurement",
    ),
    Sensor(
        "budget",
        "Download budget estimate",
        "both",
        "GiB",
        "data_size",
        "measurement",
    ),
    Sensor(
        "unsatisfied",
        "Unsatisfied torrents",
        "mam",
        "torrents",
        state_class="measurement",
    ),
    Sensor("limit", "Unsatisfied limit", "mam", "torrents"),
    Sensor("limit_source", "Unsatisfied limit source", "mam"),
    Sensor(
        "headroom",
        "Unsatisfied headroom",
        "mam",
        "torrents",
        state_class="measurement",
    ),
    Sensor(
        "satisfied",
        "Satisfied torrents",
        "mam",
        "torrents",
        state_class="measurement",
    ),
    Sensor(
        "seeding",
        "Tracker seeding count",
        "mam",
        "torrents",
        state_class="measurement",
    ),
    Sensor(
        "leeching",
        "Tracker leeching count",
        "mam",
        "torrents",
        state_class="measurement",
    ),
    Sensor("connectable", "Tracker connectability", "mam"),
    Sensor("coverage", "Account data coverage", "mam"),
    Sensor(
        "torrent_count",
        "Local torrent count",
        "qbt",
        "torrents",
        state_class="measurement",
    ),
    Sensor(
        "local_unsatisfied",
        "Local unsatisfied estimate",
        "qbt",
        "torrents",
        state_class="measurement",
    ),
    Sensor(
        "attention_count",
        "Torrents needing attention",
        "qbt",
        "torrents",
        state_class="measurement",
    ),
    Sensor("mam_health", "Account source health"),
    Sensor("qbt_health", "Client source health"),
    Sensor("mam_last_success", "Account last success", device_class="timestamp"),
    Sensor("qbt_last_success", "Client last success", device_class="timestamp"),
    Sensor("mam_next_poll", "Account next poll", device_class="timestamp"),
    Sensor("qbt_next_poll", "Client next poll", device_class="timestamp"),
    Sensor("details", "Torrent details", "qbt"),
    Sensor("notices", "Account notices", "mam"),
    Sensor("issues", "Issues"),
)
