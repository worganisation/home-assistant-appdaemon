# Copyright (c) 2026 Will Garside
# ruff: noqa: PT009
"""Contract tests for the dormant Backplane context-capture adapter."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import UUID

from apps.habit.backplane_contract import (
    CAPTURE_PROMPTS_EVALUATE_PATH,
    CONTEXT_EVENTS_BATCH_PATH,
    CONTEXT_EVENTS_PATH,
    CONTEXT_PATH,
    CaptureBudgetClass,
    CapturePolicyUpdate,
    CapturePromptDeliveryRequest,
    CapturePromptDismissRequest,
    CapturePromptEvaluationRequest,
    CapturePromptExpireRequest,
    CapturePromptResponseRequest,
    CaptureReferenceIds,
    ContextEventBatchCreate,
    ContextEventCreate,
    ContextEventStatus,
    PrivacyClass,
    capture_policy_path,
    capture_prompt_action_path,
)

EVENT_ID = UUID("11111111-1111-4111-8111-111111111111")
PROMPT_ID = UUID("22222222-2222-4222-8222-222222222222")
RESPONSE_ID = UUID("33333333-3333-4333-8333-333333333333")
NOW = datetime(2026, 9, 2, 17, 30, tzinfo=UTC)


class BackplaneContextContractTest(unittest.TestCase):
    """Keep AppDaemon's future wire payloads aligned with Backplane."""

    def test_event_payload_matches_context_event_create(self) -> None:
        """Event fields and JSON encodings mirror ContextEventCreate."""
        request = ContextEventCreate(
            user_id="will",
            source="appdaemon",
            source_event_id="binary_sensor.will_exercising:2026-09-02T17:30:00Z",
            correlation_key="will:exercise:2026-09-02T17:30:00Z",
            idempotency_key="appdaemon:event:exercise:2026-09-02T17:30:00Z",
            kind="exercise.ended",
            occurred_at=NOW,
            ended_at=NOW,
            timezone="Europe/London",
            confidence=0.9,
            privacy_class=PrivacyClass.SENSITIVE,
            status=ContextEventStatus.OBSERVED,
            summary="Exercise ended",
            payload={"activity": "cycling"},
            provenance={"entity_id": "binary_sensor.will_exercising"},
        )

        self.assertEqual(
            request.to_payload(),
            {
                "user_id": "will",
                "source": "appdaemon",
                "source_event_id": ("binary_sensor.will_exercising:2026-09-02T17:30:00Z"),
                "correlation_key": "will:exercise:2026-09-02T17:30:00Z",
                "idempotency_key": ("appdaemon:event:exercise:2026-09-02T17:30:00Z"),
                "kind": "exercise.ended",
                "occurred_at": "2026-09-02T17:30:00+00:00",
                "ended_at": "2026-09-02T17:30:00+00:00",
                "timezone": "Europe/London",
                "confidence": 0.9,
                "privacy_class": "sensitive",
                "status": "observed",
                "summary": "Exercise ended",
                "payload": {"activity": "cycling"},
                "provenance": {"entity_id": "binary_sensor.will_exercising"},
            },
        )

    def test_event_payload_omits_optional_null_fields(self) -> None:
        """Absent optional fields use Backplane's defaults."""
        payload = ContextEventCreate(
            user_id="will",
            source="appdaemon",
            idempotency_key="event-key",
            kind="focus.ended",
            occurred_at=NOW,
            timezone="Europe/London",
        ).to_payload()

        self.assertEqual(payload["privacy_class"], "private")
        self.assertEqual(payload["status"], "observed")
        for key in (
            "source_event_id",
            "correlation_key",
            "ended_at",
            "summary",
            "supersedes_event_id",
        ):
            self.assertNotIn(key, payload)

    def test_batch_and_policy_payloads_match_backplane(self) -> None:
        """Batch ingestion and replace-style policy fields stay complete."""
        event = ContextEventCreate(
            user_id="will",
            source="appdaemon",
            idempotency_key="event-key",
            kind="focus.ended",
            occurred_at=NOW,
            timezone="Europe/London",
        )

        self.assertEqual(
            ContextEventBatchCreate(events=(event,)).to_payload(),
            {"events": [event.to_payload()]},
        )
        self.assertEqual(
            CapturePolicyUpdate(timezone="Europe/London").to_payload(),
            {
                "timezone": "Europe/London",
                "baseline_prompt_limit": 1,
                "context_prompt_limit": 2,
                "cooldown_seconds": 5400,
                "minimum_event_confidence": 0.7,
            },
        )

    def test_prompt_evaluation_uses_canonical_event_ids(self) -> None:
        """Prompt evaluation refers to canonical events without snapshots."""
        request = CapturePromptEvaluationRequest(
            user_id="will",
            source="appdaemon",
            idempotency_key="prompt-key",
            kind="mood.capture",
            budget_class=CaptureBudgetClass.CONTEXT,
            event_ids=(EVENT_ID,),
            reason="Exercise ended",
            priority=60,
            wording="How are you feeling after exercising?",
            scheduled_for=NOW,
            expires_at=datetime(2026, 9, 2, 17, 45, tzinfo=UTC),
            provenance={"adapter": "habit_tracker"},
        )

        self.assertEqual(
            request.to_payload(),
            {
                "user_id": "will",
                "source": "appdaemon",
                "idempotency_key": "prompt-key",
                "kind": "mood.capture",
                "budget_class": "context",
                "event_ids": [str(EVENT_ID)],
                "reason": "Exercise ended",
                "priority": 60,
                "wording": "How are you feeling after exercising?",
                "scheduled_for": "2026-09-02T17:30:00+00:00",
                "expires_at": "2026-09-02T17:45:00+00:00",
                "provenance": {"adapter": "habit_tracker"},
            },
        )

    def test_lifecycle_payloads_match_backplane_requests(self) -> None:
        """Delivery, response, dismissal and expiry payloads stay exact."""
        self.assertEqual(
            CapturePromptDeliveryRequest(
                delivered_at=NOW,
                delivery_context={"device": "phone"},
            ).to_payload(),
            {
                "delivered_at": "2026-09-02T17:30:00+00:00",
                "delivery_context": {"device": "phone"},
            },
        )
        self.assertEqual(
            CapturePromptResponseRequest(
                idempotency_key="response-key",
                response_kind="mood_rating",
                text="Better after moving",
                payload={"mood": "Good"},
                response_context={"receptive": True},
                provenance={"action": "MOOD_GOOD"},
                responded_at=NOW,
            ).to_payload(),
            {
                "idempotency_key": "response-key",
                "response_kind": "mood_rating",
                "text": "Better after moving",
                "payload": {"mood": "Good"},
                "response_context": {"receptive": True},
                "provenance": {"action": "MOOD_GOOD"},
                "responded_at": "2026-09-02T17:30:00+00:00",
            },
        )
        self.assertEqual(
            CapturePromptDismissRequest(dismissed_at=NOW).to_payload(),
            {
                "dismissed_at": "2026-09-02T17:30:00+00:00",
                "reason": "user_dismissed",
            },
        )
        self.assertEqual(
            CapturePromptExpireRequest(expired_at=NOW).to_payload(),
            {"expired_at": "2026-09-02T17:30:00+00:00", "reason": "stale"},
        )

    def test_reference_payload_contains_only_canonical_ids(self) -> None:
        """Local mood attribution retains IDs rather than copied context."""
        references = CaptureReferenceIds(
            event_ids=(EVENT_ID,),
            prompt_id=PROMPT_ID,
            response_id=RESPONSE_ID,
        )

        self.assertEqual(
            references.to_payload(),
            {
                "event_ids": [str(EVENT_ID)],
                "prompt_id": str(PROMPT_ID),
                "response_id": str(RESPONSE_ID),
            },
        )

    def test_routes_match_backplane_contract(self) -> None:
        """Outbox operations will target the canonical REST paths."""
        self.assertEqual(CONTEXT_EVENTS_PATH, "/context/events")
        self.assertEqual(CONTEXT_EVENTS_BATCH_PATH, "/context/events/batch")
        self.assertEqual(CONTEXT_PATH, "/context")
        self.assertEqual(CAPTURE_PROMPTS_EVALUATE_PATH, "/capture-prompts/evaluate")
        self.assertEqual(capture_policy_path("will"), "/capture-policies/will")
        self.assertEqual(
            capture_prompt_action_path(PROMPT_ID, "respond"),
            f"/capture-prompts/{PROMPT_ID}/respond",
        )


if __name__ == "__main__":
    unittest.main()
