"""Exercise AppDaemon orchestration with fake services and no network."""

# Standard-library unittest is also used by the dependency-free store tests.
# ruff: noqa: D102, PT009, PT027, SLF001

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from apps.repairs.catalogue import FIELDS, Catalogue
from apps.repairs.repair_catalogue import RepairCatalogue


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Prove service boundaries and stale-result rejection using mocks."""

    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = object.__new__(RepairCatalogue)
        self.app.store = Catalogue(str(Path(self.directory.name) / "catalogue.sqlite3"))
        self.app.ai_entity = "ai_task.repairs_ai"
        self.app.ai_identity = "ai_task.repairs_ai:1"
        self.app.stopping = False
        self.app.change_revision = 0
        self.app.refresh_due = 0
        self.app.last_sync = ""
        self.app.sync_error = False
        self.app.commands = asyncio.Queue()
        self.app.selected = "spook:test"
        self.app.draft_key = self.app.selected
        self.app.draft = ""
        self.app.connected = False
        self.app.client = Mock()
        self.app.wake = asyncio.Event()
        self.issues = [
            {
                "domain": "spook",
                "issue_id": "test",
                "severity": "warning",
                "translation_placeholders": {"entities": "sensor.missing"},
            },
        ]
        self.key = "spook:test"

        async def request(command, **kwargs):
            del kwargs
            return {
                "repairs/list_issues": {"issues": self.issues},
                "config/entity_registry/list": [],
                "get_config": {"version": "2026.9.1"},
            }[command]

        self.app._request = AsyncMock(side_effect=request)
        self.app.get_state = AsyncMock(return_value=None)
        self.output = dict.fromkeys(FIELDS, "Example")
        self.app.call_service = AsyncMock(
            return_value={"result": {"response": {"data": self.output}}},
        )
        await self.app._refresh()

    async def test_uses_dedicated_ai_and_structured_response(self):
        await self.app._analyse(self.app.store.records()[0])
        call = self.app.call_service.call_args
        self.assertEqual(call.args, ("ai_task/generate_data",))
        self.assertEqual(call.kwargs["service_data"]["entity_id"], "ai_task.repairs_ai")
        self.assertEqual(self.app.store.records()[0]["generation_status"], "ready")

    async def test_removed_while_generating_discards_output(self):
        row = self.app.store.records()[0]
        self.issues = []
        await self.app._analyse(row)
        self.assertIsNone(self.app.store.records()[0]["analysis"])
        self.assertEqual(self.app.store.records()[0]["status"], "resolved")

    async def test_note_saved_during_generation_discards_output(self):
        row = self.app.store.records()[0]
        self.app.commands.put_nowait(("note", "Do not replace this device"))
        self.app.commands.put_nowait(("save_note", "PRESS"))
        await self.app._analyse(row)
        record = self.app.store.records()[0]
        self.assertIsNone(record["analysis"])
        self.assertEqual(record["note"], "Do not replace this device")

    async def test_failed_snapshot_preserves_active_repairs(self):
        self.app._request = AsyncMock(return_value={"issues": None})
        with self.assertRaises(ValueError):
            await self.app._refresh()
        self.assertEqual(self.app.store.records()[0]["status"], "active")

    async def test_sensitive_entity_context_does_not_include_location(self):
        self.app.get_state = AsyncMock(
            return_value={"state": "Secret location", "attributes": {"latitude": 42}},
        )
        context = await self.app._context(
            {
                "domain": "test",
                "issue_id": "test",
                "translation_placeholders": {"entity": "person.will"},
            },
            {"person.will": "person"},
            "2026.9.1",
        )
        self.assertNotIn("Secret location", json.dumps(context))
        self.assertNotIn("latitude", json.dumps(context))
        self.assertEqual(context["entities"][0]["availability"], "available")

    async def test_mqtt_projection_and_note_selection(self):
        self.app._publish_catalogue()
        publications = self.app.client.publish.call_args_list
        active = next(
            call.args[1]
            for call in publications
            if call.args[0] == "appdaemon/repairs/active"
        )
        self.assertEqual(json.loads(active)["count"], 1)
        self.app.commands.put_nowait(("note", "unsaved"))
        self.app.commands.put_nowait(("select", self.key))
        self.app._drain_commands()
        self.assertEqual(self.app.draft, "")

    async def test_ai_timeout_and_invalid_output_never_save(self):
        self.app.call_service = AsyncMock(side_effect=TimeoutError)
        with self.assertRaises(TimeoutError):
            await self.app._analyse(self.app.store.records()[0])
        self.app.call_service = AsyncMock(
            return_value={"response": {"data": {"title": "partial"}}},
        )
        with self.assertRaises((TypeError, ValueError)):
            await self.app._analyse(self.app.store.records()[0])
        self.assertIsNone(self.app.store.records()[0]["analysis"])

    async def test_structural_context_never_includes_card_content(self):
        result = self.app._structural_references(
            {"cards": [{"entity": "sensor.missing", "content": "password=hidden"}]},
            {"sensor.missing"},
        )
        self.assertNotIn("hidden", json.dumps(result))
        self.assertEqual(result["references"][0]["reference"], "sensor.missing")


if __name__ == "__main__":
    unittest.main()
