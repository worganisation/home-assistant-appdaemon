# Copyright (c) 2026 Will Garside
# ruff: noqa: PT009, S101, SLF001
"""Behavior tests for AppDaemon mood receptivity handling."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from apps.habit.context import ContextCandidate, ContextTriggerMode
from apps.habit.habit_tracker import MOOD_FALLBACK_MESSAGE, HabitTracker
from apps.habit.models import PendingReminder, UserData


class MoodReceptivityTest(unittest.TestCase):
    """Exercise production callbacks with a minimal AppDaemon test double."""

    def _tracker(self, now: datetime) -> HabitTracker:
        tracker = object.__new__(HabitTracker)
        user_data = UserData(mood_reminders_enabled=True)
        tracker.store = SimpleNamespace(
            data=SimpleNamespace(users={"will": user_data}),
            save=Mock(),
        )
        tracker.reminders_enabled = True
        tracker.reminders = SimpleNamespace(cancel_mood=Mock())
        tracker._mood_receptivity_deferred = set()
        tracker._pending_context = {}
        tracker._context_timers = {}
        tracker._context_recent_occurrences = {}
        tracker._aware_now = Mock(return_value=now)
        tracker._logical_today = Mock(return_value=date(2026, 9, 2))
        tracker._has_mood_for_day = Mock(return_value=False)
        tracker._publish_mood_next_reminder = Mock()
        tracker._arm_mood_pending = Mock()
        tracker._notify_mood = Mock()
        tracker._ai_mood_message = Mock(return_value=None)
        tracker.log = Mock()
        return tracker

    def test_blocked_baseline_is_deferred_without_consuming_attempt(self) -> None:
        """An unreceptive 17:00 reminder retries the same attempt at 17:15."""
        tracker = self._tracker(datetime(2026, 9, 2, 17, tzinfo=UTC))
        tracker._baseline_mood_receptive = Mock(return_value=False)

        tracker._send_mood_reminder("will", 1)

        pending = tracker.store.data.users["will"].pending_mood_reminder
        self.assertIsNotNone(pending)
        assert pending is not None
        self.assertEqual(pending.next_index, 1)
        self.assertEqual(pending.fire_at, "2026-09-02T17:15:00+00:00")
        self.assertIn("will", tracker._mood_receptivity_deferred)
        tracker._notify_mood.assert_not_called()

    def test_unknown_and_unavailable_receptivity_fail_open_for_baseline(self) -> None:
        """A broken helper cannot suppress the guaranteed daily reminder."""
        for state in ("unknown", "unavailable"):
            with self.subTest(state=state):
                tracker = self._tracker(datetime(2026, 9, 2, 17, tzinfo=UTC))
                tracker._user_config = Mock(
                    return_value={
                        "mood_receptive_entity": (
                            "binary_sensor.will_mood_prompt_receptive"
                        ),
                    },
                )
                tracker.get_state = Mock(return_value=state)

                tracker._send_mood_reminder("will", 1)

                tracker._notify_mood.assert_called_once_with(
                    "will",
                    MOOD_FALLBACK_MESSAGE,
                )

    def test_unknown_and_unavailable_receptivity_fail_closed_for_context(self) -> None:
        """Optional contextual prompts require an explicitly receptive state."""
        for state in ("unknown", "unavailable"):
            with self.subTest(state=state):
                tracker = self._tracker(datetime(2026, 9, 2, 17, tzinfo=UTC))
                tracker.store.data.users["will"].mood_context_prompts_enabled = True
                tracker._user_config = Mock(
                    return_value={
                        "mood_receptive_entity": (
                            "binary_sensor.will_mood_prompt_receptive"
                        ),
                    },
                )
                tracker.get_state = Mock(return_value=state)
                tracker._send_context_prompt = Mock()

                tracker._queue_context_prompt(
                    "will",
                    kind="return_home",
                    reason="returning home",
                    occurred_at=datetime(2026, 9, 2, 16, 55, tzinfo=UTC),
                )

                self.assertIn("will", tracker._pending_context)
                tracker._send_context_prompt.assert_not_called()

    def test_receptive_transition_sends_deferred_baseline_immediately(self) -> None:
        """The HA receptivity sensor releases a deferred reminder without polling."""
        tracker = self._tracker(datetime(2026, 9, 2, 17, 5, tzinfo=UTC))
        tracker._baseline_mood_receptive = Mock(side_effect=[False, True])
        tracker._send_mood_reminder("will", 1)

        tracker._context_receptivity_changed(
            "binary_sensor.will_mood_prompt_receptive",
            "state",
            "off",
            "on",
            user="will",
        )

        tracker._notify_mood.assert_called_once_with("will", MOOD_FALLBACK_MESSAGE)
        self.assertNotIn("will", tracker._mood_receptivity_deferred)

    def test_blocked_baseline_expires_at_ten_pm(self) -> None:
        """No scheduled reminder survives the agreed 22:00 cutoff."""
        tracker = self._tracker(datetime(2026, 9, 2, 22, tzinfo=UTC))
        tracker._baseline_mood_receptive = Mock(return_value=False)
        tracker.store.data.users["will"].pending_mood_reminder = PendingReminder(
            fire_at="2026-09-02T22:00:00+00:00",
            next_index=1,
            final_index=1,
        )

        tracker._send_mood_reminder("will", 1)

        self.assertIsNone(tracker.store.data.users["will"].pending_mood_reminder)
        tracker._notify_mood.assert_not_called()

    def test_receptive_transition_re_evaluates_pending_context(self) -> None:
        """A blocked context candidate is popped and run through current gates again."""
        tracker = self._tracker(datetime(2026, 9, 2, 18, tzinfo=UTC))
        tracker._context_mood_receptive = Mock(return_value=True)
        tracker._pending_context["will"] = {
            "kind": "return_home",
            "reason": "returning home",
            "occurred_at": "2026-09-02T17:55:00+00:00",
        }
        tracker._queue_context_prompt = Mock()

        tracker._context_receptivity_changed(
            "binary_sensor.will_mood_prompt_receptive",
            "state",
            "off",
            "on",
            user="will",
        )

        self.assertNotIn("will", tracker._pending_context)
        tracker._queue_context_prompt.assert_called_once_with(
            "will",
            kind="return_home",
            reason="returning home",
            occurred_at=datetime(2026, 9, 2, 17, 55, tzinfo=UTC),
        )

    def test_nearby_context_candidate_is_coalesced_before_delivery(self) -> None:
        """Production queueing drops a second event in the same 15-minute window."""
        tracker = self._tracker(datetime(2026, 9, 2, 18, 20, tzinfo=UTC))
        tracker.store.data.users["will"].mood_context_prompts_enabled = True
        tracker._context_recent_occurrences["will"] = datetime(
            2026,
            9,
            2,
            18,
            tzinfo=UTC,
        )
        tracker._send_context_prompt = Mock()

        tracker._queue_context_prompt(
            "will",
            kind="calendar_end",
            reason="after a calendar block",
            occurred_at=datetime(2026, 9, 2, 18, 15, tzinfo=UTC),
        )

        tracker._send_context_prompt.assert_not_called()

    def test_shadow_candidate_never_enters_prompt_queue(self) -> None:
        """Pre-Backplane focus/exercise observations cannot notify."""
        tracker = self._tracker(datetime(2026, 9, 2, 18, tzinfo=UTC))
        tracker._queue_context_prompt = Mock()
        candidate = ContextCandidate(
            user="will",
            kind="focus_end",
            reason="after a focus session",
            occurred_at=datetime(2026, 9, 2, 18, tzinfo=UTC),
            source_entity="binary_sensor.wills_macbook_pro_focus",
            mode=ContextTriggerMode.SHADOW,
        )

        tracker._handle_context_candidate(candidate)

        tracker._queue_context_prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
