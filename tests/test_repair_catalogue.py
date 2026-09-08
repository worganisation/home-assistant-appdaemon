"""Offline lifecycle tests; no Home Assistant, MQTT or paid AI requests."""

# Standard-library unittest keeps lifecycle checks dependency-free in CI.
# ruff: noqa: D102, PT009, PT027

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from apps.repairs.catalogue import (
    FIELDS,
    KITCHEN_KEY,
    KITCHEN_NOTE,
    Catalogue,
    clean_text,
    validate_analysis,
)


def issue(**changes):
    """Build a representative public repair record."""
    return {
        "domain": "spook",
        "issue_id": "unknown_entities",
        "severity": "warning",
        "ignored": False,
        "translation_placeholders": {"entities": "sensor.missing"},
        **changes,
    }


class CatalogueTests(unittest.TestCase):
    """Exercise persisted lifecycle, caching, privacy and failure behavior."""

    def setUp(self):
        """Create an isolated database."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "repairs.sqlite3")
        self.store = Catalogue(self.path)
        self.store.reconcile([issue()])
        self.key = "spook:unknown_entities"
        self.context = {"ha_version": "2026.9.1"}
        self.store.set_input(self.key, self.context, "test-ai:1")

    def row(self):
        """Read the current persisted record."""
        return self.store.records()[0]

    def finish(self):
        """Save a valid analysis."""
        return self.store.complete(
            self.key,
            self.row()["fingerprint"],
            dict.fromkeys(FIELDS, "Example"),
        )

    def test_unchanged_snapshot_and_restart_reuse_analysis(self):
        self.assertTrue(self.finish())
        digest = self.row()["fingerprint"]
        self.store.reconcile([issue(created="another timestamp")])
        self.store.set_input(self.key, self.context, "test-ai:1")
        restarted = Catalogue(self.path).records()[0]
        self.assertEqual(restarted["generation_status"], "ready")
        self.assertEqual(restarted["fingerprint"], digest)

    def test_note_invalidates_without_overwriting_analysis(self):
        self.finish()
        old_digest = self.row()["fingerprint"]
        self.store.save_note(self.key, "Keep the original radiator")
        self.store.set_input(self.key, self.context, "test-ai:1")
        self.assertNotEqual(self.row()["fingerprint"], old_digest)
        self.assertTrue(self.store.projection("active")[0]["stale"])
        self.assertFalse(
            self.store.complete(self.key, old_digest, dict.fromkeys(FIELDS, "Old")),
        )
        self.finish()
        self.assertEqual(self.row()["note"], "Keep the original radiator")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM analyses").fetchone()[0], 2)

    def test_removal_dismissal_and_recurrence(self):
        self.finish()
        old_digest = self.row()["fingerprint"]
        self.store.reconcile([issue(ignored=True)])
        self.assertEqual(self.store.projection("active"), [])
        self.assertEqual(self.store.projection("resolved"), [])
        self.store.reconcile([])
        self.assertEqual(len(self.store.projection("resolved")), 1)
        self.assertFalse(self.finish())
        self.store.reconcile([issue()])
        self.store.set_input(self.key, self.context, "test-ai:1")
        self.assertEqual(self.row()["occurrence"], 2)
        self.assertNotEqual(self.row()["fingerprint"], old_digest)

    def test_retry_limit_and_manual_retry(self):
        self.finish()
        self.store.request_analysis(self.key)
        digest = self.row()["fingerprint"]
        for attempts, clock, expected in [(1, 0, 60), (2, 60, 360), (3, 360, 660)]:
            self.store.failed(self.key, digest, clock)
            self.assertEqual(self.row()["attempts"], attempts)
            self.assertEqual(self.row()["retry_at"], expected)
        self.assertEqual(self.row()["generation_status"], "failed")
        self.assertIsNotNone(self.row()["analysis"])
        self.store.request_analysis(self.key)
        self.assertEqual(self.row()["generation_status"], "pending")

    def test_model_and_context_changes_invalidate(self):
        self.finish()
        self.store.set_input(self.key, self.context, "test-ai:2")
        self.assertEqual(self.row()["generation_status"], "pending")
        self.finish()
        self.store.set_input(self.key, {"ha_version": "2026.9.2"}, "test-ai:2")
        self.assertEqual(self.row()["generation_status"], "pending")

    def test_history_projection_is_bounded(self):
        self.store.reconcile([issue(issue_id=f"issue_{index}") for index in range(30)])
        self.store.reconcile([])
        self.assertEqual(len(self.store.projection("resolved")), 20)
        self.assertEqual(len(self.store.records()), 31)

    def test_large_metadata_and_sensitive_text(self):
        self.store.reconcile(
            [
                issue(
                    translation_placeholders={
                        "entities": "x" * 20000,
                        "info": "password=abc me@example.com https://example.com/?token=x",
                    },
                ),
            ],
        )
        data = json.loads(self.row()["metadata"])["translation_placeholders"]
        self.assertIn("truncated", data["entities"])
        self.assertNotIn("abc", data["info"])
        self.assertNotIn("example.com", data["info"])
        self.assertEqual(clean_text("sensor.missing"), "sensor.missing")

    def test_invalid_ai_does_not_replace_last_good_output(self):
        self.finish()
        with self.assertRaises(ValueError):
            validate_analysis({"title": "incomplete"})
        with self.assertRaises(ValueError):
            validate_analysis(dict.fromkeys(FIELDS, "x" * 5000))
        self.assertEqual(self.row()["generation_status"], "ready")

    def test_kitchen_instruction_seeded_only_once(self):
        domain, issue_id = KITCHEN_KEY.split(":", 1)
        self.store.reconcile([issue(domain=domain, issue_id=issue_id)])
        self.assertLessEqual(len(KITCHEN_NOTE), 255)
        self.store.save_note(KITCHEN_KEY, "User edited instruction")
        self.store.reconcile([issue(domain=domain, issue_id=issue_id)])
        row = next(row for row in self.store.records() if row["key"] == KITCHEN_KEY)
        self.assertEqual(row["note"], "User edited instruction")


if __name__ == "__main__":
    unittest.main()
