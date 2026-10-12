"""Prepare Codex sections for the existing storage-mode Tooling dashboard."""

from __future__ import annotations

import re

from pydantic import BaseModel


class Sensor(BaseModel):
    """Public sensor metadata returned by the monitor control socket."""

    entity_id: str
    name: str
    key: str
    group: str


class Account(BaseModel):
    """An enrolled account and its discovered sensors."""

    profile: str
    email: str
    sensors: list[Sensor]


class Accounts(BaseModel):
    """Control endpoint response."""

    accounts: list[Account]


def window_label(sensor: Sensor) -> str:
    """Express the reported duration in days, hours or minutes."""
    if not (
        match := re.fullmatch(r"(.+) (\d+) min (used|remaining|next reset)", sensor.name)
    ):
        return sensor.name

    bucket, duration, _ = match.groups()
    minutes = int(duration)
    if minutes and minutes % 1440 == 0:
        amount, unit = minutes // 1440, "day"
    elif minutes and minutes % 60 == 0:
        amount, unit = minutes // 60, "hour"
    else:
        amount, unit = minutes, "minute"

    label = f"{amount} {unit}{'s' if amount != 1 else ''}"
    return label if bucket == "codex" else f"{bucket} · {label}"


def compact_number(unit: str) -> str:
    """Format large readings without changing their underlying sensor values."""
    return (
        "{% set number = value | float %}"
        "{% if number | abs >= 1000000000 %}{{ '%.2f' | format(number / 1000000000) }}B"
        "{% elif number | abs >= 1000000 %}{{ '%.2f' | format(number / 1000000) }}M"
        "{% elif number | abs >= 1000 %}{{ '%.2f' | format(number / 1000) }}K"
        "{% else %}{{ number | round(2) }}{% endif %} " + unit
    )


def detail_card(sensor: Sensor) -> dict[str, object]:
    """Show short labels, readable values and metric-specific icons."""
    labels = {
        "last_success": ("Updated", "mdi:clock-check-outline"),
        "available_resets": ("Resets left", "mdi:restart"),
        "lifetime_tokens": ("Total tokens", "mdi:counter"),
        "daily_tokens": ("Daily tokens", "mdi:chart-bar"),
        "daily_date": ("Reported date", "mdi:calendar-outline"),
    }
    label, icon = labels.get(sensor.key, (sensor.name, "mdi:chart-box-outline"))
    value = "{{ value }}"
    if sensor.key.endswith("_remaining"):
        label, icon = "Remaining", "mdi:gauge"
        value = "{{ value }}% · " + window_label(sensor)
    elif sensor.key.endswith("_reset"):
        label, icon = "Next reset", "mdi:calendar-clock"
        value = "{{ as_timestamp(value) | timestamp_custom('%d %b, %H:%M') }}"
    elif sensor.key.endswith("_credits"):
        label, icon = "Credits", "mdi:wallet-outline"
        value = compact_number("credits")
        if (bucket := sensor.name.removesuffix(" credit balance")) != "codex":
            value += "\n" + bucket
    elif sensor.key in {"lifetime_tokens", "daily_tokens"}:
        value = compact_number("tokens")
    elif sensor.key == "last_success":
        value = "{{ relative_time(as_datetime(value)) }} ago"
    elif sensor.key == "daily_date":
        value = "{{ as_datetime(value).strftime('%d %b %Y') }}"

    return {
        "type": "custom:mushroom-template-card",
        "entity": sensor.entity_id,
        "primary": label,
        "secondary": (
            "{% set value = states(entity) %}"
            "{% if value == 'unavailable' %}Unavailable"
            "{% elif value in ['unknown', 'none', ''] %}Not reported"
            "{% else %}" + value + "{% endif %}"
        ),
        "icon": icon,
        "color": "blue",
        "multiline_secondary": not sensor.key.endswith("_reset"),
        "tap_action": {"action": "more-info"},
        "hold_action": {"action": "more-info"},
        "double_tap_action": {"action": "more-info"},
        "icon_tap_action": {"action": "more-info"},
    }


def sections(payload: dict[str, object]) -> list[dict[str, object]]:
    """Render existing card types without changing Home Assistant."""
    result: list[dict[str, object]] = []
    for account in Accounts.model_validate(payload).accounts:
        status = next(
            (sensor for sensor in account.sensors if sensor.key == "status"),
            None,
        )
        cards: list[dict[str, object]] = [
            {
                "type": "heading",
                "heading": f"Codex — {account.profile}",
                "icon": "mdi:robot-outline",
                "badges": [
                    {"type": "entity", "entity": status.entity_id, "show_state": True},
                ]
                if status
                else [],
            },
        ]
        used = [
            sensor
            for sensor in account.sensors
            if sensor.group == "quota" and sensor.key.endswith("_used")
        ]
        cards.extend(
            {
                "type": "gauge",
                "entity": sensor.entity_id,
                "name": f"Usage · {window_label(sensor)}",
                "min": 0,
                "max": 100,
                "needle": True,
                "severity": {"green": 0, "yellow": 80, "red": 95},
            }
            for sensor in used
        )
        details = [
            sensor
            for sensor in account.sensors
            if sensor not in used and sensor != status
        ]
        cards.append(
            {
                "type": "grid",
                "columns": 2,
                "square": False,
                "cards": [detail_card(sensor) for sensor in details],
            },
        )
        if used:
            cards.append(
                {
                    "type": "custom:mini-graph-card",
                    "name": "Codex usage — seven days",
                    "entities": [
                        {"entity": sensor.entity_id, "name": window_label(sensor)}
                        for sensor in used
                    ],
                    "hours_to_show": 168,
                    "points_per_hour": 1,
                    "height": 150,
                    "lower_bound": 0,
                    "upper_bound": 100,
                    "show": {"legend": True, "labels": True, "fill": "fade"},
                },
            )
        result.append({"type": "grid", "column_span": 3, "cards": cards})
    return result
