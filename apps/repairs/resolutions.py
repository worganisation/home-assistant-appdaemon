"""Source-controlled actions; generated summaries never define repair procedures."""

from __future__ import annotations

from typing import Any

from .catalogue import KITCHEN_KEY, KITCHEN_NOTE

NATIVE_REPAIR = (
    "Open this issue in Home Assistant Repairs and follow its repair instructions"
)

# These describe repair semantics, not version-dependent menu/button sequences.
PROCEDURES = {
    ("homeassistant", "config_entry_reauth"): (
        "Open this issue in Home Assistant Repairs and complete reauthentication",
    ),
    ("esphome", "ble_firmware_outdated"): (
        "Open this issue in Home Assistant Repairs for the required ESPHome version",
        "Update the affected Bluetooth proxy using its ESPHome update procedure",
    ),
    ("spook", "orphaned_statistics"): (
        "Use this issue in Home Assistant Repairs to identify the orphaned statistics",
        "Delete unwanted orphaned statistics; keep any history you still want",
    ),
    ("spook", "lovelace_missing_resources"): (
        "Locate the dashboard resource registrations listed in Evidence",
        "Restore missing resources you still use; remove obsolete registrations",
    ),
    ("spook", "unknown_customized_entities"): (
        "Locate the customization entries listed in Evidence",
        "Remove obsolete entries; correct a reference only if its intended entity is known",
    ),
}


def resolution(value: dict[str, Any]) -> dict[str, str]:
    """Keep notes authoritative and unknown procedures in the native repair flow."""
    repair = value["repair"]
    note = value.get("user_note", "")
    key = f"{repair['domain']}:{repair['issue_id']}"
    involvement = "None"
    if key == KITCHEN_KEY and note == KITCHEN_NOTE:
        steps = (
            "Re-pair the original kitchen radiator in Zigbee2MQTT; do not substitute the small radiator",
            "Restore its original entity ID from Evidence; keep existing dashboard references",
        )
        involvement = "Pair the original kitchen radiator"
    elif note:
        steps = (
            "Use the saved note's constraints to choose the repair action",
            NATIVE_REPAIR,
        )
    else:
        steps = PROCEDURES.get(
            (repair["domain"], repair["translation_key"]),
            (NATIVE_REPAIR,),
        )
    return {
        "steps": "\n".join(f"{index}. {step}" for index, step in enumerate(steps, 1)),
        "involvement": involvement,
    }
