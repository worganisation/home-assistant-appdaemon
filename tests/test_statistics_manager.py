"""Exercise native helper lifecycle and ownership safety without Home Assistant."""

from __future__ import annotations

# ruff: noqa: PT009, PT027 — Tests use the standard-library unittest runner.
import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING, cast

from statistics_manager.manager import Manager
from statistics_manager.model import Policy, Record, Snapshot
from statistics_manager.store import Store

if TYPE_CHECKING:
    from statistics_manager.api import HomeAssistant


class FakeHomeAssistant:
    """Model registry mutations and ambiguous creation responses."""

    def __init__(self) -> None:
        self.entities: list[Record] = [
            {
                "id": "source-1",
                "entity_id": "sensor.temperature",
                "labels": ["label-1"],
                "name": "Office",
            },
        ]
        self.entries: list[Record] = []
        self.labels: list[Record] = [{"label_id": "label-1", "name": "trend72Hours"}]
        self.states: list[Record] = [
            {"entity_id": "sensor.temperature", "state": "20", "attributes": {}},
        ]
        self.creates = 0
        self.deletes = 0
        self.fail_snapshot = False
        self.fail_delete = False
        self.lose_create_response = False
        self.sources: dict[str, str] = {}

    async def snapshot(self) -> Snapshot:
        """Return a complete snapshot or simulate a disconnected API."""
        if self.fail_snapshot:
            raise ConnectionError("Disconnected")
        return Snapshot.parse(self.entities, self.entries, self.labels, self.states)

    async def create(self, policy: Policy, source_id: str, entity_id: str) -> str:
        """Create native-shaped provenance before optionally losing the response."""
        self.creates += 1
        entry_id = f"entry-{self.creates}"
        self.entries.append(
            {
                "entry_id": entry_id,
                "domain": "statistics",
                "title": policy.marker(source_id),
            },
        )
        self.entities.append(
            {
                "id": f"helper-{self.creates}",
                "entity_id": f"sensor.appdaemon_statistics_{self.creates}",
                "labels": [],
                "platform": "statistics",
                "unique_id": entry_id,
                "config_entry_id": entry_id,
                "original_name": policy.marker(source_id),
            },
        )
        self.sources[entry_id] = entity_id
        if self.lose_create_response:
            self.lose_create_response = False
            raise TimeoutError("Response lost after creation")
        return entry_id

    async def configure(self, policy: Policy, entry_id: str, source: str) -> bool:
        """Update the native source association."""
        del policy
        changed = self.sources.get(entry_id) != source
        self.sources[entry_id] = source
        return changed

    async def ws(self, command: str, **data: object) -> object:
        """Apply supported registry mutations to the fake server."""
        if command == "config/entity_registry/update":
            target = next(
                row for row in self.entities if row["entity_id"] == data["entity_id"]
            )
            if "name" in data:
                target["name"] = data["name"]
            if "new_entity_id" in data:
                target["entity_id"] = data["new_entity_id"]
            return target
        if command == "config_entries/update":
            target = next(
                row for row in self.entries if row["entry_id"] == data["entry_id"]
            )
            target["title"] = data["title"]
            return target
        raise ValueError(command)

    async def delete(self, entry_id: str) -> None:
        """Remove the exact entry and its registered entity."""
        if self.fail_delete:
            raise ConnectionError("Deletion failed")
        self.deletes += 1
        self.entries = [row for row in self.entries if row["entry_id"] != entry_id]
        self.entities = [
            row for row in self.entities if row.get("config_entry_id") != entry_id
        ]


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Verify meaningful lifecycle and failure-path invariants."""

    def setUp(self) -> None:
        """Give each test independent persistent state and native registries."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ownership.json"
        self.api = FakeHomeAssistant()
        self.manager = Manager(
            cast("HomeAssistant", self.api),
            Store(self.path),
            Policy(),
        )

    async def test_create_rename_and_remove(self) -> None:
        """Preserve identity during renames and clean up label removal."""
        result = await self.manager.reconcile()
        self.assertEqual(result["managed"], 1)
        await self.manager.reconcile()
        self.assertEqual(self.api.creates, 1)
        helper_id = self.api.entities[1]["entity_id"]
        self.api.entities[0].update(entity_id="sensor.study", name="Study")
        await self.manager.reconcile()
        self.assertEqual(self.api.entities[1]["entity_id"], helper_id)
        self.assertEqual(self.api.entities[1]["name"], "Study · 72h Change")
        self.assertEqual(self.api.sources["entry-1"], "sensor.study")
        self.api.entities[0]["labels"] = []
        await self.manager.reconcile()
        self.assertEqual(self.api.deletes, 1)
        self.assertEqual(self.manager.store.data["helpers"], {})

    async def test_device_prefixed_generated_id_is_named_once(self) -> None:
        """Normalize device-prefixed native IDs and preserve subsequent custom IDs."""
        await self.api.create(Policy(), "source-1", "sensor.temperature")
        self.api.entities[1]["entity_id"] = "sensor.office_appdaemon_statistics_1"

        await self.manager.reconcile()
        self.assertEqual(
            self.api.entities[1]["entity_id"],
            "sensor.temperature_72h_change",
        )

        self.api.entities[1]["entity_id"] = "sensor.custom_trend"
        await self.manager.reconcile()
        self.assertEqual(self.api.entities[1]["entity_id"], "sensor.custom_trend")
        self.assertEqual(self.api.creates, 1)

    async def test_recovers_lost_creation_response_and_mapping(self) -> None:
        """Adopt an existing helper after transport or local storage loss."""
        self.api.lose_create_response = True
        result = await self.manager.reconcile()
        self.assertTrue(result["warnings"])
        await self.manager.reconcile()
        self.path.unlink()
        restarted = Manager(cast("HomeAssistant", self.api), Store(self.path), Policy())
        await restarted.reconcile()
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(len(restarted.store.data["helpers"]), 1)

    async def test_disconnection_and_failed_deletion_preserve_mapping(self) -> None:
        """Keep ownership until deletion is authoritatively confirmed."""
        await self.manager.reconcile()
        self.api.fail_snapshot = True
        with self.assertRaises(ConnectionError):
            await self.manager.reconcile()
        self.api.fail_snapshot = False
        self.api.entities[0]["labels"] = []
        self.api.fail_delete = True
        result = await self.manager.reconcile()
        self.assertTrue(result["warnings"])
        self.assertEqual(len(self.manager.store.data["helpers"]), 1)
        self.api.fail_delete = False
        await self.manager.reconcile()
        self.assertEqual(self.manager.store.data["helpers"], {})

    async def test_unavailable_and_disabled_are_preserved(self) -> None:
        """Retain a disabled helper during source unavailability."""
        await self.manager.reconcile()
        self.api.states[0]["state"] = "unavailable"
        self.api.entities[1]["disabled_by"] = "user"
        await self.manager.reconcile()
        self.assertEqual(self.api.entities[1]["disabled_by"], "user")
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(self.api.deletes, 0)

    async def test_manual_helpers_survive_source_removal(self) -> None:
        """Delete only the owned companion of a permanently removed source."""
        await self.manager.reconcile()
        self.api.entries.append(
            {
                "entry_id": "manual",
                "domain": "statistics",
                "title": "Office · 72h Change",
            },
        )
        self.api.entities.pop(0)
        await self.manager.reconcile()
        self.assertEqual([row["entry_id"] for row in self.api.entries], ["manual"])

    async def test_missing_helper_is_recreated(self) -> None:
        """Rebuild a manually removed config entry using the existing source label."""
        await self.manager.reconcile()
        await self.api.delete("entry-1")
        await self.manager.reconcile()
        self.assertEqual(self.api.creates, 2)
        self.assertEqual(len(self.api.entries), 1)

    async def test_corrupt_metadata_recovers_owned_helper(self) -> None:
        """Recover native provenance instead of trusting malformed JSON."""
        await self.manager.reconcile()
        self.path.write_text("{broken")
        store = Store(self.path)
        self.assertTrue(store.corrupt)
        restarted = Manager(cast("HomeAssistant", self.api), store, Policy())
        await restarted.reconcile()
        self.assertFalse(store.corrupt)
        self.assertEqual(self.api.creates, 1)

    async def test_label_rename_preserves_identity(self) -> None:
        """Continue selection through a display-name change of the saved label."""
        await self.manager.reconcile()
        self.api.labels[0]["name"] = "Renamed selection"
        await self.manager.reconcile()
        self.assertEqual(self.api.deletes, 0)
        self.assertEqual(self.api.creates, 1)

    async def test_concurrent_requests_are_idempotent(self) -> None:
        """Serialize overlapping worker requests around creation."""
        await asyncio.gather(self.manager.reconcile(), self.manager.reconcile())
        self.assertEqual(self.api.creates, 1)
        self.assertEqual(len(self.api.entries), 1)

    async def test_nonnumeric_source_does_not_block_numeric_source(self) -> None:
        """Report unsupported labels while maintaining eligible sources in the pass."""
        self.api.entities.append(
            {"id": "text-source", "entity_id": "sensor.text", "labels": ["label-1"]},
        )
        self.api.states.append(
            {"entity_id": "sensor.text", "state": "hello", "attributes": {}},
        )
        result = await self.manager.reconcile()
        self.assertEqual(result["managed"], 1)
        self.assertTrue(result["warnings"])

    async def test_unitless_numeric_sensor_is_supported(self) -> None:
        """Accept finite numeric states without requiring measurement units."""
        self.api.states[0]["attributes"] = {}
        result = await self.manager.reconcile()
        self.assertEqual(result["managed"], 1)

    async def test_recursive_output_label_is_rejected(self) -> None:
        """A generated helper cannot become another manager source."""
        await self.manager.reconcile()
        self.api.entities[1]["labels"] = ["label-1"]
        self.api.states.append(
            {
                "entity_id": self.api.entities[1]["entity_id"],
                "state": "2",
                "attributes": {},
            },
        )
        result = await self.manager.reconcile()
        self.assertTrue(result["warnings"])
        self.assertEqual(self.api.creates, 1)

    async def test_two_policy_identities_remain_distinct(self) -> None:
        """Keep each policy's ownership separate for a shared numeric source."""
        await self.manager.reconcile()
        second = Manager(
            cast("HomeAssistant", self.api),
            Store(self.path.with_name("second.json")),
            Policy(policy_id="change24h", hours=24),
        )
        await second.reconcile()
        await self.manager.reconcile()
        self.assertEqual(self.api.creates, 2)
        self.assertEqual(len(self.api.entries), 2)

    async def test_recreated_source_has_new_identity(self) -> None:
        """Remove the old companion and create one for the new registry identity."""
        await self.manager.reconcile()
        self.api.entities[0]["id"] = "replacement-source"
        await self.manager.reconcile()
        self.assertEqual(self.api.creates, 2)
        self.assertEqual(self.api.deletes, 1)
        self.assertEqual(len(self.api.entries), 1)

    async def test_missing_label_registry_suspends_cleanup(self) -> None:
        """Missing authoritative selection data cannot authorize bulk deletion."""
        await self.manager.reconcile()
        self.api.labels.clear()
        with self.assertRaises(ValueError):
            await self.manager.reconcile()
        self.assertEqual(len(self.api.entries), 1)

    async def test_readded_label_is_rechecked_before_deletion(self) -> None:
        """Protect a helper when a label is restored after an earlier discovery."""
        await self.manager.reconcile()
        entry = self.api.entries[0].copy()
        self.api.entities[0]["labels"] = []
        await self.api.snapshot()
        self.api.entities[0]["labels"] = ["label-1"]

        result = await self.manager.remove("source-1", entry, "label-1")
        self.assertEqual(result, [])
        self.assertEqual(len(self.api.entries), 1)

    async def test_ambiguous_ownership_requires_resolution(self) -> None:
        """Avoid choosing between two native entries claiming the same source."""
        await self.manager.reconcile()
        await self.api.create(Policy(), "source-1", "sensor.temperature")

        with self.assertRaises(ValueError):
            await self.manager.reconcile()

        self.assertEqual(len(self.api.entries), 2)


if __name__ == "__main__":
    unittest.main()
