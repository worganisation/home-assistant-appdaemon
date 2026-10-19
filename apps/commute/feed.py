"""TfL route eligibility, source freshness and commute train selection."""

from __future__ import annotations

from calendar import SATURDAY
from dataclasses import dataclass, field, replace
from datetime import datetime
from math import isfinite
from typing import TYPE_CHECKING, cast

from .constants import COMMUTE_TTL, FRESHNESS, MINUTE

if TYPE_CHECKING:
    from .session import Session

TIMETABLE_START = 6
TIMETABLE_END = 14
MAX_INTERVAL_MINUTES = 5
GOOD_SERVICE = 10
MINOR_DELAYS = 9

LINES = ("circle", "hammersmith-city")
HAMMERSMITH = "940GZZLUHSC"
GOLDHAWK = "940GZZLUGHK"
PADDINGTON = "940GZZLUPAH"


def mapping(value: object) -> dict[str, object]:
    """Narrow external JSON objects before inspecting their fields."""
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def items(value: object) -> list[object]:
    """Reject strings and mappings where TfL requires an array."""
    return cast("list[object]", value) if isinstance(value, list) else []


def number(value: object, default: float = 0) -> float:
    """Read numeric state and timestamp fields with an explicit fallback."""
    if isinstance(value, (int, float, str)):
        try:
            parsed = float(value)
            return parsed if isfinite(parsed) else default
        except ValueError:
            pass

    return default


def timestamp(value: object) -> float:
    """Read a TfL ISO timestamp; invalid values are stale."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass

    return 0


@dataclass(frozen=True)
class Journey:
    """One explicitly applicable daytime Hammersmith timetable journey."""

    departure_at: float
    line: str
    destination: str
    destination_id: str
    via: list[str]
    interval: float


@dataclass(frozen=True)
class Departure:
    """A scheduled departure or an estimate with its source timestamp."""

    departure_at: float
    line: str
    destination: str
    source_at: float = 0
    platform: str = ""


@dataclass(frozen=True)
class Service:
    """Eligibility for timetable and inferred departure use."""

    good: bool = False
    estimated: bool = False


@dataclass
class Snapshot:
    """A short-lived cache of departures and independently fetched status."""

    source: str = "unavailable"
    fetched_at: float = 0
    status_at: float = 0
    departures: list[Departure] = field(default_factory=list)
    scheduled_departures: list[Departure] = field(default_factory=list)
    disruption: str = ""
    countdown_suppressed: bool = True


@dataclass
class Plan:
    """A target train plus conservative walking and preparation estimates."""

    reason: str = "inactive"
    source: str = "unavailable"
    departures: list[Departure] = field(default_factory=list)
    arrival_at: float | None = None
    leave_at: float | None = None
    walk_minutes: float = 5
    buffer_minutes: float = 1


def timetable(body: object, line: str, clock: datetime) -> list[Journey]:
    """Select exactly one day schedule per route and its explicit interval variants."""
    data = mapping(body)
    table = mapping(data.get("timetable"))
    if (
        data.get("lineId") != line
        or data.get("direction") != "outbound"
        or table.get("departureStopId") != HAMMERSMITH
    ):
        return []

    day = clock.strftime("%A").lower()
    aliases = {day, day + "s"}
    if clock.weekday() < SATURDAY:
        aliases.update({"monday - friday", "monday to friday", "mondays to fridays"})
    elif clock.weekday() == SATURDAY:
        aliases.add("saturdays and public holidays")

    stations = [
        mapping(item) for item in items(data.get("stops")) + items(data.get("stations"))
    ]
    rows: list[Journey] = []
    for raw in items(table.get("routes")):
        route = mapping(raw)
        schedules = [
            mapping(value)
            for value in items(route.get("schedules"))
            if str(mapping(value).get("name", "")).lower() in aliases
        ]
        if len(schedules) != 1:
            continue

        for value in items(schedules[0].get("knownJourneys")):
            journey = mapping(value)
            hour = int(number(journey.get("hour"), -1))
            minute = int(number(journey.get("minute"), -1))
            if not (TIMETABLE_START <= hour < TIMETABLE_END and 0 <= minute < MINUTE):
                continue

            rows.extend(
                route_journeys(
                    items(route.get("stationIntervals")),
                    journey.get("intervalId"),
                    stations,
                    line,
                    clock.replace(
                        hour=hour,
                        minute=minute,
                        second=0,
                        microsecond=0,
                    ).timestamp(),
                ),
            )

    return sorted(rows, key=lambda row: row.departure_at)


def route_journeys(
    variants: list[object],
    interval_id: object,
    stations: list[dict[str, object]],
    line: str,
    departure_at: float,
) -> list[Journey]:
    """Resolve journey intervals only for Goldhawk followed by Paddington H&C."""
    rows: list[Journey] = []
    for value in variants:
        variant = mapping(value)
        if str(variant.get("id")) != str(interval_id):
            continue

        stops = [
            mapping(stop)
            for stop in items(variant.get("intervals"))
            if isinstance(stop, dict)
        ]
        ids = [str(stop.get("stopId", "")) for stop in stops]
        if not ids[1:] or ids[0] != GOLDHAWK or PADDINGTON not in ids:
            continue

        interval = stops[0].get("timeToArrival")
        if (
            not isinstance(interval, (int, float))
            or not 0 < interval <= MAX_INTERVAL_MINUTES
        ):
            continue

        names = [
            str(station.get("name", ""))
            for station in stations
            if station.get("id") == ids[-1]
        ]
        if not names or not names[-1]:
            continue

        rows.append(
            Journey(
                departure_at,
                line,
                names[-1],
                ids[-1],
                ids[ids.index(PADDINGTON) :],
                interval * 60,
            ),
        )
    return rows


def service_status(body: object) -> tuple[dict[str, Service], str]:
    """Permit estimates for good/minor service, timetables for good service only."""
    services: dict[str, Service] = {}
    warnings: list[str] = []
    for line in LINES:
        found = [mapping(item) for item in items(body) if mapping(item).get("id") == line]
        statuses = items(found[0].get("lineStatuses")) if len(found) == 1 else []
        codes: list[object] = []
        descriptions: list[str] = []
        for raw in statuses:
            if not isinstance(raw, dict):
                continue

            item = mapping(raw)
            code = item.get("statusSeverity", -1)
            codes.append(code)
            if code != GOOD_SERVICE:
                description = str(item.get("statusSeverityDescription", "Status unknown"))
                if reason := item.get("reason"):
                    description += f": {reason}"
                descriptions.append(description)

        good = bool(codes) and all(code == GOOD_SERVICE for code in codes)
        services[line] = Service(
            good,
            bool(codes) and all(code in (MINOR_DELAYS, GOOD_SERVICE) for code in codes),
        )
        if not good:
            label = "Circle" if line == "circle" else "H&C"
            text = "; ".join(dict.fromkeys(descriptions)) or "Service status unavailable"
            warnings.append(f"{label}: {text}")

    return services, " ".join(warnings)


def departures(
    arrivals: object,
    tables: list[Journey],
    services: dict[str, Service],
    now: float,
) -> Snapshot:
    """Infer departures only for fresh trains explicitly at a Hammersmith platform."""
    records = [mapping(item) for item in items(arrivals) if isinstance(item, dict)]
    records.sort(key=lambda item: timestamp(item.get("timestamp")), reverse=True)
    seen: set[tuple[str, str]] = set()
    estimates: list[Departure] = []
    for item in records:
        line = str(item.get("lineId", ""))
        vehicle = item.get("vehicleId")
        if line not in LINES or not vehicle:
            continue

        identity = (line, str(vehicle))
        if identity in seen:
            continue

        seen.add(identity)
        offsets = {
            journey.interval
            for journey in tables
            if journey.line == line
            and abs(journey.departure_at - now) <= COMMUTE_TTL / 2
            and item.get("destinationNaptanId") in journey.via
        }
        location = item.get("currentLocation")
        source_at = timestamp(item.get("timestamp"))
        if (
            not services.get(line, Service()).estimated
            or item.get("naptanId") != GOLDHAWK
            or item.get("direction") != "outbound"
            or not isinstance(location, str)
            or not location.startswith("At Hammersmith Platform")
            or not 0 <= now - source_at < FRESHNESS
            or len(offsets) != 1
            or not item.get("destinationName")
        ):
            continue

        departure_at = timestamp(item.get("expectedArrival")) - next(iter(offsets))
        if now < departure_at <= now + COMMUTE_TTL / 2:
            estimates.append(
                Departure(
                    departure_at,
                    line,
                    str(item["destinationName"]),
                    source_at,
                    location.replace("At Hammersmith Platform ", ""),
                ),
            )

    scheduled = [
        Departure(journey.departure_at, journey.line, journey.destination)
        for journey in tables
        if now < journey.departure_at <= now + COMMUTE_TTL
        and services.get(journey.line, Service()).good
    ]
    source = "estimated" if estimates else "scheduled" if scheduled else "unavailable"
    return Snapshot(
        source=source,
        fetched_at=now,
        departures=sorted(estimates or scheduled, key=lambda row: row.departure_at)[:10],
        scheduled_departures=sorted(scheduled, key=lambda row: row.departure_at)[:10],
        countdown_suppressed=source == "unavailable",
    )


def visible(snapshot: Snapshot, now: float) -> list[Departure]:
    """Recheck snapshot/status freshness and each prediction at rendering time."""
    if (
        snapshot.source not in {"estimated", "scheduled"}
        or snapshot.countdown_suppressed
        or not 0 <= now - snapshot.fetched_at < FRESHNESS
        or not 0 <= now - snapshot.status_at < FRESHNESS
    ):
        return []

    return sorted(
        [
            row
            for row in snapshot.departures
            if now < row.departure_at <= now + COMMUTE_TTL
            and (snapshot.source == "scheduled" or 0 <= now - row.source_at < FRESHNESS)
        ],
        key=lambda row: row.departure_at,
    )[:10]


def plan(
    snapshot: Snapshot,
    session: Session,
    total_minutes: float,
    walk_minutes: float,
    now: float,
) -> Plan:
    """Choose a catchable target without mixing live and scheduled departures."""
    walk = min(max(walk_minutes, 3), 10.5) * 60
    total = min(max(total_minutes, 5), 60) * 60
    result = Plan(walk_minutes=walk / 60)
    if not session.polling(now):
        return result

    if session.phase == "preparing":
        result.arrival_at = max((session.completed_at or now) + total, now + walk) + 60
    elif 0 < session.left_at <= now and session.started_at <= session.left_at:
        result.arrival_at = max(session.left_at + walk, now + 60) + 60
    else:
        result.arrival_at = now + walk + 60

    available = visible(snapshot, now)
    result.departures = [
        row for row in available if row.departure_at >= result.arrival_at
    ][:3]
    result.source = snapshot.source if result.departures else "unavailable"
    scheduled = (
        visible(
            replace(
                snapshot,
                source="scheduled",
                departures=snapshot.scheduled_departures,
            ),
            now,
        )
        if snapshot.source == "estimated"
        else []
    )
    if not result.departures:
        result.departures = [
            row for row in scheduled if row.departure_at >= result.arrival_at
        ][:3]
        result.source = "scheduled" if result.departures else "unavailable"

    result.reason = (
        "ready"
        if result.departures
        else "no_suitable_departure"
        if available or scheduled
        else "no_reliable_data"
    )
    if result.departures and session.phase == "preparing":
        result.leave_at = result.departures[0].departure_at - walk - 60

    return result
