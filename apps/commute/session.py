"""Commute session transitions and observed journey timings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median

from pydantic import BaseModel, ConfigDict

from .constants import (
    ARRIVAL_TTL,
    ATTRIBUTION_THRESHOLD,
    COMMUTE_TTL,
    FRESHNESS,
    MAX_WALK_SAMPLE,
    MIN_TOTAL_SAMPLES,
    MIN_WALK_SAMPLE,
    MINUTE,
    MORNING_END,
    MORNING_START,
    SHOWER_TTL,
)


class Session(BaseModel):
    """Persisted Home Assistant session; completion is independent of start."""

    model_config = ConfigDict(extra="ignore")

    phase: str = "idle"
    started_at: float = 0
    shower_started_at: float = 0
    completed_at: float = 0
    date: str = ""
    left_at: float = 0
    station_at: float = 0
    office_at: float = 0
    dismissed: bool = False
    sample_recorded: bool = False

    @property
    def reference(self) -> float:
        """Return the timestamp governing notification lifetime."""
        return self.completed_at or self.started_at

    def polling(self, now: float) -> bool:
        """Whether the session needs fresh departures."""
        return (
            self.phase in {"preparing", "travelling"}
            and not self.dismissed
            and 0 <= now - self.reference < COMMUTE_TTL
        )


class Shower(BaseModel):
    """Evidence supplied by the Home Assistant shower detector."""

    started_at: float = 0
    completed_at: float = 0
    attribution_probability: float = 0


@dataclass(frozen=True)
class Observation:
    """Current inputs and the event driving a session transition."""

    clock: datetime
    person: str
    office_day: bool
    at_station: bool
    shower_active: bool
    shower: Shower
    event: str = "tick"
    leaving: bool = False


def start_session(old: Session, observation: Observation) -> Session | None:
    """Start once per office morning using a fresh attributed shower."""
    now = observation.clock.timestamp()
    today = observation.clock.date().isoformat()
    shower = observation.shower
    recent = 0 <= now - shower.started_at < SHOWER_TTL
    if recent:
        started = datetime.fromtimestamp(shower.started_at, observation.clock.tzinfo)
        recent = (
            started.date() == observation.clock.date()
            and MORNING_START <= started.hour < MORNING_END
            and shower.attribution_probability >= ATTRIBUTION_THRESHOLD
        )

    if (
        old.date != today
        and observation.office_day
        and MORNING_START <= observation.clock.hour < MORNING_END
        and recent
        and (
            observation.leaving
            or (observation.person == "home" and observation.shower_active)
        )
    ):
        phase = "travelling" if observation.leaving else "preparing"
        if observation.leaving and observation.at_station:
            phase = "station"
        elif observation.leaving and observation.person == "Yantra":
            phase = "complete"

        return Session(
            phase=phase,
            started_at=now,
            shower_started_at=shower.started_at,
            date=today,
            left_at=now if observation.leaving else 0,
            station_at=now if phase == "station" else 0,
            office_at=now if phase == "complete" else 0,
        )

    return None


def advance(old: Session, observation: Observation) -> Session:
    """Apply one observation without resetting dismissal or inventing completion."""
    if started := start_session(old, observation):
        return started

    now = observation.clock.timestamp()
    shower = observation.shower
    session = old.model_copy()
    if (
        observation.event == "shower"
        and old.phase == "preparing"
        and not old.completed_at
    ):
        if (
            shower.started_at == old.shower_started_at
            and 0 <= now - shower.completed_at < FRESHNESS
            and shower.completed_at >= old.started_at
            and observation.person == "home"
            and shower.attribution_probability >= ATTRIBUTION_THRESHOLD
        ):
            session.completed_at = shower.completed_at

    elif observation.event == "dismissed" and old.phase in {"preparing", "travelling"}:
        session.dismissed = True

    elif old.phase in {"preparing", "travelling", "station"}:
        progress(session, observation)

    return session


def progress(session: Session, observation: Observation) -> None:
    """Record expiry and ordered location observations for an active commute."""
    old = session.model_copy()
    now = observation.clock.timestamp()
    if now - old.reference > (ARRIVAL_TTL if old.phase == "station" else COMMUTE_TTL):
        session.phase = "expired"
    elif observation.person == "Yantra":
        session.office_at = now
        session.phase = "complete"
        session.sample_recorded = (
            0 < old.completed_at < old.left_at < old.station_at <= now
            and now - old.completed_at <= ARRIVAL_TTL
        )
    elif old.left_at > 0 and observation.person == "home":
        session.phase = "cancelled"
    elif old.phase != "station" and observation.at_station:
        session.station_at = now
        session.phase = "station"
    elif old.phase == "preparing" and observation.person not in {
        "home",
        "unknown",
        "unavailable",
    }:
        session.left_at = now
        session.phase = "travelling"


def walk_samples(values: list[float], session: Session) -> list[float]:
    """Use observed walks, bootstrapping only a qualifying restored session."""
    samples = [value for value in values if MIN_WALK_SAMPLE <= value <= MAX_WALK_SAMPLE]
    if not samples and session.phase == "complete" and session.sample_recorded:
        duration = (session.station_at - session.left_at) / MINUTE
        if (
            0
            < session.completed_at
            < session.left_at
            < session.station_at
            <= session.office_at
            and session.office_at - session.completed_at <= ARRIVAL_TTL
            and MIN_WALK_SAMPLE <= duration <= MAX_WALK_SAMPLE
        ):
            samples = [round(duration, 2)]

    return samples[-10:]


def timings(samples: list[float], walks: list[float]) -> tuple[float, float]:
    """Return the established total and walking estimates in minutes."""
    total = round(median(samples), 1) if len(samples) >= MIN_TOTAL_SAMPLES else 20
    walk = round(min(max(median(walks), 3), 10) + 0.5, 1) if walks else 5
    return total, walk
