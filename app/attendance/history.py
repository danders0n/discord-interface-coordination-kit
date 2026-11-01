"""What the attendance and the ratings add up to (docs/PLAN.md, *Supervisor: attendance*, *Use*, *Member profile*,
*Mission feedback*), without discord.py: a member's record, totals per member for a period, a mission maker's
missions, and the events with another group. Who may see which part is the Discord layer's to decide.

The rate is attended / (attended + no-show): *excused*, *to confirm* and *not linked* (the bot can't tell) don't
count either way. Playing without a slot is attending."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import or_, select

from app.db import Database
from app.db.models import AttendanceEntry, Event, EventGuestGroup, MissionRating

ATTENDED = ("attended", "walk_in")


@dataclass
class Record:
    attended: int = 0
    no_show: int = 0
    excused: int = 0
    recent: list[tuple[Event, str]] = field(default_factory=list)  # newest first

    @property
    def rate(self) -> float | None:
        counted = self.attended + self.no_show
        return self.attended / counted if counted else None


@dataclass
class Rated:
    """An event with its ratings."""

    event: Event
    ratings: list[MissionRating]

    @property
    def average(self) -> float | None:
        return sum(r.stars for r in self.ratings) / len(self.ratings) if self.ratings else None


def average(rated: list[Rated]) -> tuple[float | None, int]:
    """Over every rating of the events: the average and how many there are."""
    stars = [r.stars for item in rated for r in item.ratings]
    return (sum(stars) / len(stars) if stars else None), len(stars)


class History:
    def __init__(self, db: Database, clock: Callable[[], datetime] | None = None) -> None:
        self.db = db
        self.clock = clock or (lambda: datetime.now(UTC))

    async def member(self, member_id: int, recent: int = 10) -> Record:
        async with self.db.session() as session:
            rows = (await session.execute(select(Event, AttendanceEntry.status).join(
                AttendanceEntry, AttendanceEntry.event_id == Event.id).where(
                AttendanceEntry.member_id == member_id).order_by(Event.start_at.desc()))).all()
        record = Record()
        for event, status in rows:
            if status in ATTENDED:
                record.attended += 1
            elif status == "no_show":
                record.no_show += 1
            elif status == "excused":
                record.excused += 1
            if len(record.recent) < recent:
                record.recent.append((event, status))
        return record

    async def totals(self, since: datetime | None) -> dict[int, Record]:
        """Per member, the events that started since then (None: all)."""
        query = select(AttendanceEntry.member_id, AttendanceEntry.status).join(
            Event, Event.id == AttendanceEntry.event_id).where(AttendanceEntry.member_id.is_not(None))
        if since is not None:
            query = query.where(Event.start_at >= since)
        async with self.db.session() as session:
            rows = (await session.execute(query)).all()
        found: dict[int, Record] = {}
        for member_id, status in rows:
            record = found.setdefault(member_id, Record())
            if status in ATTENDED:
                record.attended += 1
            elif status == "no_show":
                record.no_show += 1
            elif status == "excused":
                record.excused += 1
        return found

    async def _rated(self, query) -> list[Rated]:
        async with self.db.session() as session:
            events = list((await session.execute(query.order_by(Event.start_at.desc()))).scalars())
            ratings = list((await session.execute(select(MissionRating).where(
                MissionRating.event_id.in_([e.id for e in events])))).scalars()) if events else []
        by_event: dict[int, list[MissionRating]] = {}
        for rating in ratings:
            by_event.setdefault(rating.event_id, []).append(rating)
        return [Rated(event, by_event.get(event.id, [])) for event in events]

    async def maker(self, member_id: int) -> list[Rated]:
        """The missions this member authored (not guest events: those are another group's), newest first."""
        return await self._rated(select(Event).where(Event.author_id == member_id, Event.kind != "guest",
                                                     Event.status == "ended"))

    async def group(self, group_id: int) -> list[Rated]:
        """Our events with that group: as host (guest events, with the cooperation ratings) or invited."""
        invited = select(EventGuestGroup.event_id).where(EventGuestGroup.group_id == group_id)
        return await self._rated(select(Event).where(
            or_(Event.host_group_id == group_id, Event.id.in_(invited)), Event.status.in_(("published", "ended"))))
