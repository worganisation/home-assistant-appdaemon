# Copyright (c) 2026 Will Garside
"""Operational context-trigger helpers with no durable or wire-format contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from typing import Self

CONTEXT_COALESCE_MINUTES = 15
MOOD_PROMPT_EXPIRY_TIME = time(22)
RECEPTIVITY_RETRY_MINUTES = 15


class ContextTriggerMode(StrEnum):
    """Whether an observed transition may notify or is recorded diagnostically."""

    ACTIVE = "active"
    SHADOW = "shadow"


@dataclass(frozen=True, slots=True)
class ContextCandidate:
    """One in-memory AppDaemon trigger observation, not a Backplane DTO."""

    user: str
    kind: str
    reason: str
    occurred_at: datetime
    source_entity: str
    mode: ContextTriggerMode


@dataclass(frozen=True, slots=True)
class ContextEndTrigger:
    """Configuration for a binary entity's meaningful on-to-off transition."""

    kind: str
    entity_id: str
    reason: str
    mode: ContextTriggerMode = ContextTriggerMode.SHADOW

    @classmethod
    def from_mapping(cls, kind: str, value: object) -> Self:
        """Parse one AppDaemon trigger config without defining an external schema."""
        if not kind.strip():
            raise ValueError("context trigger kind must not be empty")
        if not isinstance(value, Mapping):
            raise TypeError(f"context trigger {kind} must be a mapping")
        entity_id = value.get("entity_id")
        reason = value.get("reason")
        mode = value.get("mode", ContextTriggerMode.SHADOW)
        if not isinstance(entity_id, str) or "." not in entity_id:
            raise ValueError(f"context trigger {kind} has an invalid entity_id")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(f"context trigger {kind} has an invalid reason")
        if not isinstance(mode, str):
            raise TypeError(f"context trigger {kind} mode must be a string")
        return cls(
            kind=kind,
            entity_id=entity_id,
            reason=reason.strip(),
            mode=ContextTriggerMode(mode),
        )

    def candidate(
        self,
        *,
        user: str,
        old: object,
        new: object,
        occurred_at: datetime,
    ) -> ContextCandidate | None:
        """Return a candidate only for a clean active-to-inactive transition."""
        if old != "on" or new != "off":
            return None
        return ContextCandidate(
            user=user,
            kind=self.kind,
            reason=self.reason,
            occurred_at=occurred_at,
            source_entity=self.entity_id,
            mode=self.mode,
        )


def parse_context_end_triggers(value: object) -> tuple[ContextEndTrigger, ...]:
    """Parse the optional per-user transition configuration."""
    if value is None:
        return ()
    if not isinstance(value, Mapping):
        raise TypeError("mood_context_end_triggers must be a mapping")
    return tuple(
        ContextEndTrigger.from_mapping(str(kind), config)
        for kind, config in value.items()
    )


def should_coalesce(
    previous: datetime | None,
    candidate: datetime,
    *,
    window_minutes: int = CONTEXT_COALESCE_MINUTES,
) -> bool:
    """Return whether two observations belong to one local prompt window."""
    if previous is None:
        return False
    return abs(candidate - previous) <= timedelta(minutes=window_minutes)


def context_prompt_message(kind: str) -> str:
    """Return concise deterministic copy for a contextual mood prompt."""
    return {
        "calendar_end": "How are you feeling after that part of your day?",
        "return_home": "Now that you're home, how are you feeling?",
        "focus_end": "How are you feeling after that focus session?",
        "call_end": "How are you feeling after that call?",
        "exercise_end": "How are you feeling after exercising?",
    }.get(
        kind,
        "How are you feeling now? A quick check-in can capture how this part of your day landed.",
    )


def next_receptivity_retry_at(now: datetime) -> datetime | None:
    """Return the next baseline retry, capped by the local 22:00 expiry."""
    expiry = datetime.combine(now.date(), MOOD_PROMPT_EXPIRY_TIME, tzinfo=now.tzinfo)
    if now >= expiry:
        return None
    return min(now + timedelta(minutes=RECEPTIVITY_RETRY_MINUTES), expiry)
