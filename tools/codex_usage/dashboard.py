"""Prepare Codex sections for the existing storage-mode Tooling dashboard."""

from __future__ import annotations

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
                "name": sensor.name,
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
                "cards": [
                    {
                        "type": "custom:mushroom-entity-card",
                        "entity": sensor.entity_id,
                        "name": sensor.name,
                        "primary_info": "state",
                        "secondary_info": "name",
                    }
                    for sensor in details
                ],
            },
        )
        if used:
            cards.append(
                {
                    "type": "custom:mini-graph-card",
                    "name": "Codex usage — seven days",
                    "entities": [
                        {"entity": sensor.entity_id, "name": sensor.name}
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
