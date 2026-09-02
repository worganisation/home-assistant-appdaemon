# Copyright (c) 2026 Will Garside
"""Dormant AppDaemon adapter types for Backplane's context-capture API.

This module deliberately performs no HTTP calls and owns no durable event ledger. It
only fixes the JSON boundary that the retrying outbox will use after Backplane's
context-capture branch has merged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type PromptAction = Literal["delivered", "respond", "dismiss", "expire"]

CONTEXT_EVENTS_PATH = "/context/events"
CONTEXT_EVENTS_BATCH_PATH = "/context/events/batch"
CONTEXT_PATH = "/context"
CAPTURE_PROMPTS_EVALUATE_PATH = "/capture-prompts/evaluate"


class PrivacyClass(StrEnum):
    """Backplane context-event privacy values."""

    PRIVATE = "private"
    SENSITIVE = "sensitive"
    SHARED = "shared"


class ContextEventStatus(StrEnum):
    """Backplane context-event lifecycle values."""

    OBSERVED = "observed"
    TENTATIVE = "tentative"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"
    SUPERSEDED = "superseded"


class CaptureBudgetClass(StrEnum):
    """Independent Backplane prompt-budget buckets."""

    BASELINE = "baseline"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class ContextEventCreate:
    """Exact JSON boundary for ``POST /context/events``."""

    user_id: str
    source: str
    idempotency_key: str
    kind: str
    occurred_at: datetime
    timezone: str
    confidence: float = 1.0
    privacy_class: PrivacyClass = PrivacyClass.PRIVATE
    status: ContextEventStatus = ContextEventStatus.OBSERVED
    source_event_id: str | None = None
    correlation_key: str | None = None
    ended_at: datetime | None = None
    summary: str | None = None
    payload: JsonObject = field(default_factory=dict)
    provenance: JsonObject = field(default_factory=dict)
    supersedes_event_id: UUID | None = None

    def to_payload(self) -> JsonObject:
        """Serialize the request while omitting optional null fields."""
        result: JsonObject = {
            "user_id": self.user_id,
            "source": self.source,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "occurred_at": self.occurred_at.isoformat(),
            "timezone": self.timezone,
            "confidence": self.confidence,
            "privacy_class": self.privacy_class.value,
            "status": self.status.value,
            "payload": self.payload,
            "provenance": self.provenance,
        }
        _set_optional(result, "source_event_id", self.source_event_id)
        _set_optional(result, "correlation_key", self.correlation_key)
        _set_optional_datetime(result, "ended_at", self.ended_at)
        _set_optional(result, "summary", self.summary)
        if self.supersedes_event_id is not None:
            result["supersedes_event_id"] = str(self.supersedes_event_id)
        return result


@dataclass(frozen=True, slots=True)
class ContextEventBatchCreate:
    """Exact JSON boundary for atomic context-event batch ingestion."""

    events: tuple[ContextEventCreate, ...]

    def to_payload(self) -> JsonObject:
        """Serialize each event through the same single-event boundary."""
        return {"events": [event.to_payload() for event in self.events]}


@dataclass(frozen=True, slots=True)
class CapturePolicyUpdate:
    """Exact JSON boundary for replacing a user's capture policy."""

    timezone: str = "UTC"
    baseline_prompt_limit: int = 1
    context_prompt_limit: int = 2
    cooldown_seconds: int = 5400
    minimum_event_confidence: float = 0.7

    def to_payload(self) -> JsonObject:
        """Serialize every replaceable policy field."""
        return {
            "timezone": self.timezone,
            "baseline_prompt_limit": self.baseline_prompt_limit,
            "context_prompt_limit": self.context_prompt_limit,
            "cooldown_seconds": self.cooldown_seconds,
            "minimum_event_confidence": self.minimum_event_confidence,
        }


@dataclass(frozen=True, slots=True)
class CapturePromptEvaluationRequest:
    """Exact JSON boundary for deterministic prompt-policy evaluation."""

    user_id: str
    source: str
    idempotency_key: str
    kind: str
    event_ids: tuple[UUID, ...]
    reason: str
    budget_class: CaptureBudgetClass = CaptureBudgetClass.CONTEXT
    priority: int = 50
    wording: str | None = None
    scheduled_for: datetime | None = None
    expires_at: datetime | None = None
    provenance: JsonObject = field(default_factory=dict)

    def to_payload(self) -> JsonObject:
        """Serialize the request using canonical event IDs only."""
        result: JsonObject = {
            "user_id": self.user_id,
            "source": self.source,
            "idempotency_key": self.idempotency_key,
            "kind": self.kind,
            "budget_class": self.budget_class.value,
            "event_ids": [str(event_id) for event_id in self.event_ids],
            "reason": self.reason,
            "priority": self.priority,
            "provenance": self.provenance,
        }
        _set_optional(result, "wording", self.wording)
        _set_optional_datetime(result, "scheduled_for", self.scheduled_for)
        _set_optional_datetime(result, "expires_at", self.expires_at)
        return result


@dataclass(frozen=True, slots=True)
class CapturePromptDeliveryRequest:
    """Exact JSON boundary for recording successful delivery."""

    delivered_at: datetime | None = None
    delivery_context: JsonObject = field(default_factory=dict)

    def to_payload(self) -> JsonObject:
        """Serialize delivery-time context without event snapshots."""
        result: JsonObject = {"delivery_context": self.delivery_context}
        _set_optional_datetime(result, "delivered_at", self.delivered_at)
        return result


@dataclass(frozen=True, slots=True)
class CapturePromptResponseRequest:
    """Exact JSON boundary for an immutable prompt response."""

    idempotency_key: str
    response_kind: str
    text: str | None = None
    payload: JsonObject = field(default_factory=dict)
    response_context: JsonObject = field(default_factory=dict)
    provenance: JsonObject = field(default_factory=dict)
    responded_at: datetime | None = None

    def to_payload(self) -> JsonObject:
        """Serialize one response observation."""
        result: JsonObject = {
            "idempotency_key": self.idempotency_key,
            "response_kind": self.response_kind,
            "payload": self.payload,
            "response_context": self.response_context,
            "provenance": self.provenance,
        }
        _set_optional(result, "text", self.text)
        _set_optional_datetime(result, "responded_at", self.responded_at)
        return result


@dataclass(frozen=True, slots=True)
class CapturePromptDismissRequest:
    """Exact JSON boundary for a prompt dismissal."""

    dismissed_at: datetime | None = None
    reason: str = "user_dismissed"

    def to_payload(self) -> JsonObject:
        """Serialize a dismissal transition."""
        result: JsonObject = {"reason": self.reason}
        _set_optional_datetime(result, "dismissed_at", self.dismissed_at)
        return result


@dataclass(frozen=True, slots=True)
class CapturePromptExpireRequest:
    """Exact JSON boundary for a prompt expiry."""

    expired_at: datetime | None = None
    reason: str = "stale"

    def to_payload(self) -> JsonObject:
        """Serialize an expiry transition."""
        result: JsonObject = {"reason": self.reason}
        _set_optional_datetime(result, "expired_at", self.expired_at)
        return result


@dataclass(frozen=True, slots=True)
class CaptureReferenceIds:
    """Canonical Backplane IDs retained by a future local mood record."""

    event_ids: tuple[UUID, ...] = ()
    prompt_id: UUID | None = None
    response_id: UUID | None = None

    def to_payload(self) -> JsonObject:
        """Serialize references only, never detection/delivery snapshots."""
        result: JsonObject = {"event_ids": [str(event_id) for event_id in self.event_ids]}
        if self.prompt_id is not None:
            result["prompt_id"] = str(self.prompt_id)
        if self.response_id is not None:
            result["response_id"] = str(self.response_id)
        return result


def capture_prompt_action_path(prompt_id: UUID, action: PromptAction) -> str:
    """Return the canonical Backplane lifecycle endpoint for a prompt."""
    return f"/capture-prompts/{prompt_id}/{action}"


def capture_policy_path(user_id: str) -> str:
    """Return the canonical Backplane policy endpoint for a user."""
    return f"/capture-policies/{user_id}"


def _set_optional(result: JsonObject, key: str, value: str | None) -> None:
    if value is not None:
        result[key] = value


def _set_optional_datetime(
    result: JsonObject,
    key: str,
    value: datetime | None,
) -> None:
    if value is not None:
        result[key] = value.isoformat()
