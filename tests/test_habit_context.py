# Copyright (c) 2026 Will Garside
# ruff: noqa: PT009, PT027, S101
"""Tests for operational contextual-mood helpers."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from apps.habit.context import (
    ContextEndTrigger,
    ContextTriggerMode,
    context_prompt_message,
    next_receptivity_retry_at,
    parse_context_end_triggers,
    should_coalesce,
)


class ContextEndTriggerTest(unittest.TestCase):
    """Cover strict configuration parsing and transition detection."""

    def test_parse_defaults_to_shadow(self) -> None:
        """New trigger sources cannot notify unless explicitly activated."""
        (trigger,) = parse_context_end_triggers(
            {
                "call_end": {
                    "entity_id": "binary_sensor.will_on_call",
                    "reason": "after a call",
                },
            },
        )

        self.assertEqual(trigger.mode, ContextTriggerMode.SHADOW)

    def test_candidate_requires_clean_on_to_off_transition(self) -> None:
        """Unavailable and startup transitions do not look like ended events."""
        trigger = ContextEndTrigger(
            kind="focus_end",
            entity_id="binary_sensor.will_focus",
            reason="after focus",
        )
        now = datetime(2026, 9, 2, 12, tzinfo=UTC)

        self.assertIsNone(
            trigger.candidate(user="will", old="unavailable", new="off", occurred_at=now),
        )
        candidate = trigger.candidate(
            user="will",
            old="on",
            new="off",
            occurred_at=now,
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate.kind, "focus_end")
        self.assertEqual(candidate.source_entity, trigger.entity_id)

    def test_parse_rejects_invalid_mode(self) -> None:
        """Typos cannot silently activate or disable a trigger."""
        with self.assertRaises(ValueError):
            parse_context_end_triggers(
                {
                    "call_end": {
                        "entity_id": "binary_sensor.will_on_call",
                        "reason": "after a call",
                        "mode": "enabled",
                    },
                },
            )


class ContextPromptPolicyTest(unittest.TestCase):
    """Cover AppDaemon's local timing and copy decisions."""

    def test_coalesces_candidates_within_fifteen_minutes(self) -> None:
        """Closely related transitions produce one prompt opportunity."""
        first = datetime(2026, 9, 2, 12, tzinfo=UTC)
        second = datetime(2026, 9, 2, 12, 15, tzinfo=UTC)

        self.assertTrue(should_coalesce(first, second))

    def test_does_not_coalesce_later_candidate(self) -> None:
        """A later transition remains independently useful."""
        first = datetime(2026, 9, 2, 12, tzinfo=UTC)
        second = datetime(2026, 9, 2, 12, 16, tzinfo=UTC)

        self.assertFalse(should_coalesce(first, second))

    def test_receptivity_retry_is_capped_at_expiry(self) -> None:
        """A blocked baseline gets one final expiry callback at 22:00."""
        now = datetime(2026, 9, 2, 21, 50, tzinfo=UTC)

        self.assertEqual(
            next_receptivity_retry_at(now),
            datetime(2026, 9, 2, 22, tzinfo=UTC),
        )

    def test_receptivity_retry_expires_at_ten_pm(self) -> None:
        """No baseline is carried into the night or next logical day."""
        now = datetime(2026, 9, 2, 22, tzinfo=UTC)

        self.assertIsNone(next_receptivity_retry_at(now))

    def test_context_copy_is_specific_without_exposing_event_details(self) -> None:
        """Known trigger kinds get useful but privacy-safe notification text."""
        self.assertEqual(
            context_prompt_message("return_home"),
            "Now that you're home, how are you feeling?",
        )


if __name__ == "__main__":
    unittest.main()
