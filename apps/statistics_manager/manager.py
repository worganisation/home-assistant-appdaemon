"""Single-flight desired-state reconciliation for native Statistics helpers."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .model import MARKER, Policy, Record, Snapshot, numeric, record, text

if TYPE_CHECKING:
    from .api import HomeAssistant
    from .store import Store


class Manager:
    """Reconcile only provably owned helpers, retaining identity across source renames."""

    def __init__(
        self,
        api: HomeAssistant,
        store: Store,
        policy: Policy,
        *,
        dry_run: bool = False,
    ) -> None:
        self.api = api
        self.store = store
        self.policy = policy
        self.dry_run = dry_run
        self.lock = asyncio.Lock()

    async def label(self, snapshot: Snapshot) -> str:
        """Retain a label's stable identity when its display name changes."""
        saved = self.store.data.get("label_id")
        if isinstance(saved, str):
            if saved not in snapshot.labels:
                raise ValueError(
                    "Configured selection label is missing; cleanup suspended",
                )
            return saved
        matching = [
            key
            for key, row in snapshot.labels.items()
            if row.get("name") == self.policy.label
        ]
        if len(matching) > 1:
            raise ValueError("Selection label name is ambiguous")
        if matching:
            return matching[0]
        if self.dry_run:
            raise ValueError("Selection label is absent in dry-run mode")
        result = record(
            await self.api.ws("config/label_registry/create", name=self.policy.label),
        )
        return text(result, "label_id")

    @staticmethod
    def eligible(source: Record, state: Record, *, existing: bool) -> bool:
        """Keep known sources through temporary bad states and reject recursive outputs."""
        if not text(source, "entity_id").startswith("sensor."):
            return False
        if str(source.get("original_name", "")).startswith(MARKER):
            return False
        if existing or numeric(state.get("state")):
            return True
        attributes = record(state.get("attributes", {}))
        return bool(
            attributes.get("state_class") or attributes.get("unit_of_measurement"),
        )

    @staticmethod
    def selected(source: Record, label_id: str) -> bool:
        """Check a validated direct entity-label assignment."""
        labels = source.get("labels")
        if not isinstance(labels, list):
            raise TypeError("Missing source label assignments")
        return label_id in labels

    async def maintain(self, source_id: str, label_id: str) -> list[str]:
        """Refresh one source immediately before creating or updating its helper."""
        snapshot = await self.api.snapshot()
        source = snapshot.entities.get(source_id)
        if not source or not self.selected(source, label_id):
            return []
        entity_id = text(source, "entity_id")
        state = snapshot.states.get(entity_id, {})
        pair = snapshot.owned(self.policy).get(source_id)
        if not self.eligible(source, state, existing=pair is not None):
            raise ValueError(f"Unsupported or not yet numeric: {entity_id}")
        if self.dry_run:
            return [] if pair else [f"would create {entity_id}"]

        changes: list[str] = []
        if pair is None:
            await self.api.create(self.policy, source_id, entity_id)
            changes.append(f"created {entity_id}")
            snapshot = await self.api.snapshot()
            pair = snapshot.owned(self.policy).get(source_id)
        if not pair or pair[1] is None:
            raise RuntimeError(f"Waiting for helper registry entry: {entity_id}")
        entry, helper = pair
        if helper is None or entry.get("disabled_by") or helper.get("disabled_by"):
            return changes
        entry_id = text(entry, "entry_id")
        if await self.api.configure(self.policy, entry_id, entity_id):
            changes.append(f"configured {entity_id}")
        changes.extend(await self.rename(snapshot, source, state, entry, helper))
        return changes

    async def rename(
        self,
        snapshot: Snapshot,
        source: Record,
        state: Record,
        entry: Record,
        helper: Record,
    ) -> list[str]:
        """Set friendly metadata while preserving an established helper entity ID."""
        name = self.policy.name(source, state)
        changes: list[str] = []
        if helper.get("name") != name:
            await self.api.ws(
                "config/entity_registry/update",
                entity_id=text(helper, "entity_id"),
                name=name,
            )
            changes.append(f"named {source['entity_id']}")
        if entry.get("title") != name:
            await self.api.ws(
                "config_entries/update",
                entry_id=text(entry, "entry_id"),
                title=name,
            )
        if "appdaemon_statistics_" in text(helper, "entity_id"):
            proposed = f"{source['entity_id']}_{self.policy.hours}h_change"
            if proposed not in {row["entity_id"] for row in snapshot.entities.values()}:
                await self.api.ws(
                    "config/entity_registry/update",
                    entity_id=text(helper, "entity_id"),
                    new_entity_id=proposed,
                )
        return changes

    async def remove(self, source_id: str, entry: Record, label_id: str) -> list[str]:
        """Reconfirm selection and provenance immediately before deleting an orphan."""
        if self.dry_run:
            return [f"would delete {entry['entry_id']}"]
        fresh = await self.api.snapshot()
        source = fresh.entities.get(source_id)
        if label_id not in fresh.labels:
            raise ValueError("Selection label missing; cleanup suspended")
        if source and self.selected(source, label_id):
            return []
        current = fresh.owned(self.policy).get(source_id)
        if not current or current[0]["entry_id"] != entry["entry_id"]:
            return []
        await self.api.delete(text(entry, "entry_id"))
        verified = await self.api.snapshot()
        if entry["entry_id"] in verified.entries:
            raise RuntimeError("Statistics deletion was not confirmed")
        return [f"deleted {entry['entry_id']}"]

    async def reconcile(self) -> Record:
        """Isolate source failures and retain successful changes from the same pass."""
        async with self.lock:
            snapshot = await self.api.snapshot()
            label_id = await self.label(snapshot)
            owned = snapshot.owned(self.policy)
            desired = {
                key
                for key, source in snapshot.entities.items()
                if self.selected(source, label_id)
            }
            changes: list[str] = []
            warnings: list[str] = []
            for source_id in desired | owned.keys():
                try:
                    if source_id in desired:
                        changes.extend(await self.maintain(source_id, label_id))
                    else:
                        changes.extend(
                            await self.remove(source_id, owned[source_id][0], label_id),
                        )
                except Exception as exc:
                    warnings.append(f"{source_id}: {type(exc).__name__}: {exc}")

            final = await self.api.snapshot()
            helpers, companions = self.mapping(final)
            if not self.dry_run:
                self.store.save(helpers, label_id)
            return {
                "managed": len(helpers),
                "labelled": len(desired),
                "companions": companions,
                "changes": changes,
                "warnings": warnings,
            }

    def mapping(self, snapshot: Snapshot) -> tuple[Record, Record]:
        """Build diagnostics from verified native identities, including failed deletions."""
        helpers: Record = {}
        companions: Record = {}
        for source_id, (entry, helper) in snapshot.owned(self.policy).items():
            helpers[source_id] = {
                "config_entry_id": entry["entry_id"],
                "helper_registry_id": helper.get("id") if helper else None,
                "helper_entity_id": helper.get("entity_id") if helper else None,
                "source_entity_id": snapshot.entities.get(source_id, {}).get("entity_id"),
            }
            if helper and (source := snapshot.entities.get(source_id)):
                companions[text(source, "entity_id")] = helper["entity_id"]
        return helpers, companions
