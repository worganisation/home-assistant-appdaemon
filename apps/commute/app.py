"""Own commute decisions while Home Assistant retains inputs and durable state."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast
from zoneinfo import ZoneInfo

from appdaemon.plugins.hass.hassapi import Hass
from httpx2 import AsyncClient, HTTPError
from pydantic import ValidationError

from .constants import (
    FRESHNESS,
    MAX_WALK_SAMPLE,
    MIN_WALK_SAMPLE,
    MINUTE,
    MORNING_END,
    MORNING_START,
)
from .feed import (
    LINES,
    Departure,
    Journey,
    Plan,
    Service,
    Snapshot,
    departures,
    items,
    mapping,
    number,
    plan,
    service_status,
    timetable,
)
from .session import Observation, Session, Shower, advance, timings, walk_samples

if TYPE_CHECKING:
    from collections.abc import Awaitable

SESSION = "var.will_commute_session"
DEPARTURES = "var.will_commute_departures"
PLAN = "var.will_commute_plan"
LEGACY = (
    "automation.event_will_commute_session",
    "automation.event_will_commute_feed",
    "automation.event_will_commute_notification",
)
INVALID = {"unknown", "unavailable"}
ZONE = ZoneInfo("Europe/London")


class Commute(Hass):
    """Serialize session changes independently of slow, bounded TfL requests."""

    async def initialize(self) -> None:
        """Wait for the HA handoff, then restore its existing session and samples."""
        self.transition_lock = asyncio.Lock()
        self.fetch_lock = asyncio.Lock()
        self.client = AsyncClient(timeout=10, headers={"Accept": "application/json"})
        self.active = False
        self.stopping = False
        self.session = Session()
        self.samples: list[float] = []
        self.walks: list[float] = []
        self.snapshot = Snapshot()
        self.tables: list[Journey] = []
        self.services: dict[str, Service] = {}
        self.table_key: tuple[str, float] | None = None
        self.table_at = 0.0
        self.tables_complete = False
        self.status_at = 0.0
        self.disruption = "Service status unavailable"
        self.departure_timer: str | None = None
        self.departure_at = 0.0

        for entity in (
            "person.will",
            "binary_sensor.will_at_hammersmith_hc",
            "sensor.will_shower_session",
            "input_boolean.will_commute_notifications",
            DEPARTURES,
        ):
            await cast(
                "Awaitable[str]",
                self.listen_state(
                    self.changed,
                    entity,
                    attribute="all" if entity == DEPARTURES else "state",
                ),
            )

        for event in ("will_shower_completed", "mobile_app_notification_cleared"):
            await cast("Awaitable[str]", self.listen_event(self.event, event))

        clock = self.clock()
        tick = clock.replace(microsecond=0) + timedelta(seconds=15 - clock.second % 15)
        poll = clock.replace(microsecond=0) + timedelta(seconds=30 - clock.second % 30)
        await cast("Awaitable[str]", self.run_every(self.tick, tick, 15))
        await cast("Awaitable[str]", self.run_every(self.notify_tick, poll, 30))
        await cast("Awaitable[str]", self.run_every(self.poll, poll, 30))
        await self.tick()

    @staticmethod
    def clock() -> datetime:
        """Use HA's configured local service day, including daylight saving."""
        return datetime.now(ZONE)

    async def state(self, entity: str) -> str:
        """Read a state from the AppDaemon HASS cache."""
        value = await cast("Awaitable[object]", self.get_state(entity))
        return str(value) if value is not None else "unavailable"

    async def attributes(self, entity: str) -> dict[str, object]:
        """Read native attributes without parsing their string representations."""
        value = await cast("Awaitable[object]", self.get_state(entity, attribute="all"))
        return mapping(mapping(value).get("attributes"))

    async def service(self, name: str, **data: object) -> dict[str, object]:
        """Surface failed HA calls instead of treating a failed write as persisted."""
        response: object = await self.call_service(name, service_data=data)
        result = mapping(response)
        if result.get("success") is not True:
            raise RuntimeError(f"Home Assistant service failed: {name}: {result}")

        return result

    async def write(self, entity: str, value: str, attributes: dict[str, object]) -> None:
        """Encode native values safely for var's second template-rendering pass."""
        encoded = {
            key: "{{ " + json.dumps(json.dumps(item)) + " | from_json }}"
            for key, item in attributes.items()
        }
        await self.service("var/set", entity_id=entity, value=value, attributes=encoded)

    async def ready(self) -> bool:
        """Require the companion entity and no enabled legacy notification owner."""
        if self.stopping or await self.state(PLAN) in INVALID:
            return False

        for entity in LEGACY:
            if await self.state(entity) == "on":
                return False

        return True

    async def restore(self) -> bool:
        """Load the sole durable store without overwriting unavailable state."""
        if await self.state(SESSION) in INVALID:
            return False

        attributes = await self.attributes(SESSION)
        data = mapping(attributes.get("session"))
        data.setdefault("started_at", data.get("completed_at", 0))
        self.session = Session.model_validate(data)
        self.samples = [
            float(value)
            for value in items(attributes.get("samples"))
            if isinstance(value, (int, float))
        ]
        self.walks = walk_samples(
            [
                float(value)
                for value in items(attributes.get("walk_samples"))
                if isinstance(value, (int, float))
            ],
            self.session,
        )
        self.snapshot = Snapshot(fetched_at=self.clock().timestamp())
        self.tables = []
        self.services = {}
        self.table_key = None
        self.table_at = self.status_at = 0
        self.tables_complete = False
        await self.persist(self.session, self.samples, self.walks)
        await self.write(DEPARTURES, "unavailable", asdict(self.snapshot))
        await self.service("script/will_commute_notification", show_notification=False)
        self.active = True
        self.log(
            "Commute owner active; restored %s total and %s walking samples",
            len(self.samples),
            len(self.walks),
        )
        return True

    async def persist(
        self,
        session: Session,
        samples: list[float],
        walks: list[float],
    ) -> None:
        """Commit a transition and its samples together using the existing var entity."""
        total, walk = timings(samples, walks)
        await self.write(
            SESSION,
            session.phase,
            {
                "session": session.model_dump(),
                "samples": samples,
                "walk_samples": walks,
                "total_minutes": total,
                "walk_minutes": walk,
            },
        )

    async def changed(
        self,
        entity: str,
        attribute: str,
        old: object,
        new: object,
        **_kwargs: object,
    ) -> None:
        """React to state changes; attribute-only shower evidence needs no transition."""
        del attribute
        if entity == DEPARTURES:
            async with self.transition_lock:
                if self.active and await self.ready():
                    await self.read_snapshot()
                    await self.publish(notify=True)
            return

        leaving = (
            entity == "person.will"
            and old == "home"
            and str(new) not in INVALID | {"home"}
        )
        await self.update(
            leaving=leaving,
            notify=entity == "input_boolean.will_commute_notifications",
        )

    def event(self, event_type: str, data: dict[str, object], **_kwargs: object) -> None:
        """Consume confirmed completion and the phone's existing dismissal tag."""
        if event_type == "will_shower_completed":
            self.create_task(
                self.update(event="shower", shower=Shower.model_validate(data)),
            )
        elif data.get("tag") == "will_commute":
            self.create_task(self.update(event="dismissed"))

    async def tick(self, **_kwargs: object) -> None:
        """Advance expiry and diagnostics at the established fifteen-second cadence."""
        try:
            await self.update()
        except (RuntimeError, ValidationError):
            self.log(
                "Commute state update failed; retaining persisted state",
                level="ERROR",
                exc_info=True,
            )

    async def update(
        self,
        *,
        event: str = "tick",
        shower: Shower | None = None,
        leaving: bool = False,
        notify: bool = False,
    ) -> None:
        """Apply one event under a lock; HTTP polling never holds this lock."""
        async with self.transition_lock:
            if not await self.ready():
                self.active = False
                return

            restored = not self.active
            if restored and not await self.restore():
                return

            attributes = await self.attributes("sensor.will_shower_session")
            observation = Observation(
                self.clock(),
                await self.state("person.will"),
                await self.state("binary_sensor.will_office_day") == "on",
                await self.state("binary_sensor.will_at_hammersmith_hc") == "on",
                await self.state("sensor.will_shower_session") == "active",
                shower or Shower.model_validate(mapping(attributes.get("session"))),
                event,
                leaving,
            )
            updated = advance(self.session, observation)
            changed = updated != self.session
            if changed:
                samples, walks = self.samples[:], self.walks[:]
                if updated.sample_recorded and not self.session.sample_recorded:
                    samples = [
                        *samples,
                        round((updated.station_at - updated.completed_at) / 60, 2),
                    ][-10:]
                    walking = (updated.station_at - updated.left_at) / 60
                    if MIN_WALK_SAMPLE <= walking <= MAX_WALK_SAMPLE:
                        walks = [*walks, round(walking, 2)][-10:]

                await self.persist(updated, samples, walks)
                self.session, self.samples, self.walks = updated, samples, walks

            await self.publish(notify=notify or changed)

        if (changed or restored) and self.session.polling(self.clock().timestamp()):
            self.create_task(self.poll())

    async def notify_tick(self, **_kwargs: object) -> None:
        """Refresh delivery independently of pending TfL requests."""
        await self.update(notify=True)

    async def read_snapshot(self) -> None:
        """Accept the existing validated HA departure-snapshot interface."""
        data = await self.attributes(DEPARTURES)

        def rows(key: str) -> list[Departure]:
            return [
                Departure(
                    number(row.get("departure_at")),
                    str(row.get("line", "")),
                    str(row.get("destination", "")),
                    number(row.get("source_at")),
                    str(row.get("platform", "")),
                )
                for value in items(data.get(key))
                if (row := mapping(value))
            ]

        self.snapshot = Snapshot(
            source=str(data.get("source", "unavailable")),
            fetched_at=number(data.get("fetched_at")),
            status_at=number(data.get("status_at")),
            departures=rows("departures"),
            scheduled_departures=rows("scheduled_departures"),
            disruption=str(data.get("disruption", "")),
            countdown_suppressed=bool(data.get("countdown_suppressed", False)),
        )

    async def status(self, selected: Plan, *, enabled: bool) -> str:
        """Explain the same delivery, calendar and session gates on the HA dashboard."""
        today = self.session.date == self.clock().date().isoformat()
        if not enabled:
            return "Notifications disabled"
        if today and self.session.dismissed:
            return "Dismissed for today"
        if today and self.session.phase in {
            "station",
            "complete",
            "cancelled",
            "expired",
        }:
            return {
                "station": "At station",
                "complete": "Commute complete",
                "cancelled": "Commute cancelled",
                "expired": "Commute expired",
            }[self.session.phase]
        if today and self.session.phase in {"preparing", "travelling"}:
            return {
                "inactive": "Commute expired",
                "no_reliable_data": "Train data unavailable",
                "no_suitable_departure": "Waiting for a later train",
            }.get(
                selected.reason,
                "Preparing to leave"
                if self.session.phase == "preparing"
                else "Walking to station",
            )
        if await self.state("binary_sensor.will_office_day") != "on":
            status = "Office day not confirmed"
        elif not MORNING_START <= self.clock().hour < MORNING_END:
            status = "Outside morning window"
        elif await self.state("person.will") != "home":
            status = "Waiting for Will at home"
        else:
            status = "Waiting for an attributed shower"

        return status

    async def publish(self, *, notify: bool) -> None:
        """Project the current plan into HA and schedule the exact next departure."""
        now = self.clock().timestamp()
        total, walk = timings(self.samples, self.walks)
        selected = plan(self.snapshot, self.session, total, walk, now)
        enabled = await self.state("input_boolean.will_commute_notifications") == "on"
        await self.write(
            PLAN,
            selected.reason,
            {
                "plan": asdict(selected),
                "generated_at": now,
                "status": await self.status(selected, enabled=enabled),
            },
        )
        departure_at = selected.departures[0].departure_at if selected.departures else 0
        if departure_at != self.departure_at:
            if self.departure_timer:
                await cast(
                    "Awaitable[bool]",
                    self.cancel_timer(self.departure_timer, silent=True),
                )
            self.departure_at = departure_at
            self.departure_timer = None
            if departure_at > now:
                self.departure_timer = await cast(
                    "Awaitable[str]",
                    self.run_at(
                        self.departure,
                        datetime.fromtimestamp(departure_at, ZONE),
                    ),
                )

        if notify:
            try:
                await self.service(
                    "script/will_commute_notification",
                    show_notification=enabled and self.session.polling(now),
                )
            except RuntimeError:
                self.log(
                    "Commute notification submission failed",
                    level="WARNING",
                    exc_info=True,
                )

    async def departure(self, **_kwargs: object) -> None:
        """Refresh the countdown when its target departs."""
        self.departure_timer = None
        await self.update(notify=True)

    async def fetch(self, path: str) -> object:
        """Bound each independent public TfL request to ten seconds."""
        try:
            async with asyncio.timeout(10):
                response = await self.client.get("https://api.tfl.gov.uk/" + path)
                response.raise_for_status()
                return cast("object", response.json())
        except (HTTPError, ValueError, TimeoutError):
            self.log("TfL request failed: %s", path, level="WARNING")
            return None

    async def poll(self, **_kwargs: object) -> None:
        """Refresh departures every thirty seconds, skipping overlapping requests."""
        if self.fetch_lock.locked():
            return

        async with self.fetch_lock:
            if not self.active or not await self.ready():
                return

            clock = self.clock()
            now = clock.timestamp()
            identity = self.session.started_at
            if not self.session.polling(now):
                await self.update(notify=True)
                return

            key = (clock.date().isoformat(), identity)
            if self.table_key != key:
                self.tables, self.tables_complete, self.table_key = [], False, key

            if not self.tables_complete and now - self.table_at >= MINUTE:
                self.table_at = now
                tables: list[Journey] = []
                for line in LINES:
                    body = await self.fetch(
                        f"Line/{line}/Timetable/940GZZLUHSC/to/940GZZLUGHK",
                    )
                    tables.extend(timetable(body, line, clock))
                self.tables = tables
                self.tables_complete = all(
                    any(row.line == line for row in tables) for line in LINES
                )

            if not 0 <= self.clock().timestamp() - self.status_at < MINUTE:
                self.status_at = self.clock().timestamp()
                self.services, self.disruption = service_status(
                    await self.fetch("Line/circle,hammersmith-city/Status"),
                )

            fetched_at = self.clock().timestamp()
            arrivals = await self.fetch("StopPoint/940GZZLUGHK/Arrivals")
            clock = self.clock()
            fresh_services = (
                self.services
                if 0 <= clock.timestamp() - self.status_at < FRESHNESS
                else {}
            )
            snapshot = departures(
                arrivals,
                self.tables if key[0] == clock.date().isoformat() else [],
                fresh_services,
                clock.timestamp(),
            )
            snapshot.fetched_at = fetched_at
            snapshot.status_at, snapshot.disruption = self.status_at, self.disruption
            async with self.transition_lock:
                if (
                    not self.active
                    or not await self.ready()
                    or self.session.started_at != identity
                    or not self.session.polling(clock.timestamp())
                ):
                    return

                await self.write(
                    "var.will_commute_feed",
                    key[0],
                    {
                        "timetable_date": key[0],
                        "timetable_session": identity,
                        "timetable": [asdict(row) for row in self.tables],
                        "timetable_at": self.table_at,
                        "timetable_complete": self.tables_complete,
                        "service": {
                            key: asdict(value) for key, value in self.services.items()
                        },
                        "status_at": self.status_at,
                        "disruption": self.disruption,
                    },
                )
                await self.write(DEPARTURES, snapshot.source, asdict(snapshot))
                self.snapshot = snapshot
                await self.publish(notify=True)

    async def terminate(self) -> None:
        """Close HTTP resources; persistent HA state remains the recovery source."""
        owned = self.active and await self.ready()
        self.stopping = True
        self.active = False
        if owned:
            try:
                await self.service(
                    "script/will_commute_notification",
                    show_notification=False,
                )
            except RuntimeError:
                self.log(
                    "Unable to clear commute notification during shutdown",
                    level="WARNING",
                )

        await self.client.aclose()
