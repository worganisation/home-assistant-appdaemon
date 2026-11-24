"""Validated registry snapshots and stable helper identities."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import cast

type Record = dict[str, object]
MARKER = "AppDaemon statistics"


def record(value: object) -> Record:
    """Require an object with string keys from an external boundary."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise TypeError("Expected a JSON object")
    return cast("Record", value)


def records(value: object, key: str) -> list[Record]:
    """Reject partial or malformed registry responses, including duplicate identities."""
    if not isinstance(value, list):
        raise TypeError("Expected a complete registry list")
    result = [record(item) for item in value]
    identities = [text(item, key) for item in result]
    if len(set(identities)) != len(identities):
        raise ValueError("Duplicate registry identities")
    return result


def text(item: Record, key: str) -> str:
    """Read a required nonempty string."""
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Missing registry field: {key}")
    return value


def numeric(value: object) -> bool:
    """Recognize finite numeric sensor states without treating booleans as numbers."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except ValueError:
        return False


@dataclass(frozen=True)
class Policy:
    """One configurable statistics policy with a stable ownership namespace."""

    namespace: str = "statistics_sensor_manager"
    policy_id: str = "change72h"
    label: str = "trend72Hours"
    hours: int = 72
    characteristic: str = "change"
    precision: int = 2

    def __post_init__(self) -> None:
        """Reject invalid policy configuration before connecting to Home Assistant."""
        for value in (self.namespace, self.policy_id):
            if not re.fullmatch(r"[a-zA-Z0-9_-]+", value):
                raise ValueError("Namespace and policy ID must be simple identifiers")
        if self.hours <= 0 or self.precision < 0 or not self.label:
            raise ValueError("Invalid statistics policy")

    def marker(self, source_id: str) -> str:
        """Encode provenance in the helper's native original name at creation."""
        return f"{MARKER} [{self.namespace}:{self.policy_id}:{source_id}]"

    def source_from_marker(self, value: object) -> str | None:
        """Decode only an exact marker for this manager and policy."""
        if not isinstance(value, str):
            return None
        prefix = f"{MARKER} [{self.namespace}:{self.policy_id}:"
        if value.startswith(prefix) and value.endswith("]"):
            source_id = value[len(prefix) : -1]
            if re.fullmatch(r"[a-zA-Z0-9_-]+", source_id):
                return source_id
        return None

    def name(self, source: Record, state: Record) -> str:
        """Use the current display name independently of the generated entity ID."""
        attributes = record(state.get("attributes", {}))
        name = source.get("name") or attributes.get("friendly_name")
        name = name or source.get("original_name") or text(source, "entity_id")
        return f"{name} · {self.hours}h Change"

    def options(self, source: str) -> Record:
        """Use a time-only buffer; omission clears any sampling-size cap."""
        return {
            "entity_id": source,
            "state_characteristic": self.characteristic,
            "max_age": {"hours": self.hours},
            "precision": self.precision,
            "keep_last_sample": False,
            "percentile": 50,
        }


@dataclass
class Snapshot:
    """An authoritative complete snapshot; state absence is not registry removal."""

    entities: dict[str, Record]
    entries: dict[str, Record]
    labels: dict[str, Record]
    states: dict[str, Record]

    @classmethod
    def parse(
        cls,
        entities: object,
        entries: object,
        labels: object,
        states: object,
    ) -> Snapshot:
        """Validate every required collection before permitting reconciliation."""
        entity_rows = records(entities, "id")
        for entity in entity_rows:
            text(entity, "entity_id")
            if not isinstance(entity.get("labels"), list):
                raise TypeError("Entity labels missing from registry")
        return cls(
            {text(row, "id"): row for row in entity_rows},
            {text(row, "entry_id"): row for row in records(entries, "entry_id")},
            {text(row, "label_id"): row for row in records(labels, "label_id")},
            {text(row, "entity_id"): row for row in records(states, "entity_id")},
        )

    def owned(self, policy: Policy) -> dict[str, tuple[Record, Record | None]]:
        """Recover helpers from native provenance, never from a similar display name."""
        result: dict[str, tuple[Record, Record | None]] = {}
        for entry_id, entry in self.entries.items():
            if entry.get("domain") != "statistics":
                continue
            helpers = [
                entity
                for entity in self.entities.values()
                if entity.get("config_entry_id") == entry_id
            ]
            source = policy.source_from_marker(entry.get("title"))
            helper = helpers[0] if len(helpers) == 1 else None
            if helper:
                if (
                    helper.get("platform") != "statistics"
                    or helper.get("unique_id") != entry_id
                ):
                    raise ValueError(
                        "Statistics helper identity does not match its entry",
                    )
                source = policy.source_from_marker(helper.get("original_name")) or source
            if source:
                if source in result or len(helpers) > 1:
                    raise ValueError(f"Ambiguous statistics ownership for {source}")
                result[source] = (entry, helper)
        return result
