"""Mission feedback (docs/PLAN.md, *Supervisor: attendance*, *Mission feedback*), without discord.py.

- After the report, members marked **attended** (or who played without a slot) rate the mission: 1-5 stars and an
  optional comment, for ``rating_days``, and may change their rating until then.
- A **guest** event asks instead for the cooperation with the host group, open at once to everyone who held a slot
  (attendance there is confirmed by hand later); those ratings are for staff only.
- The report shows only the average and the count; the ratings with authors and comments are for the mission's
  author, the organisers and the administration. A correction that takes *attended* away removes the rating.

Refusals are :class:`FeedbackError` codes the Discord layer words."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, func, select

from app.db import Database
from app.db.models import AttendanceEntry, Event, MissionRating
from app.settings import SettingsStore

MAX_COMMENT = 1000
ATTENDED = ("attended", "walk_in")


class Problem(StrEnum):
    NOT_OPEN = "not_open"  # no report yet
    CLOSED = "closed"  # the window is over
    NOT_ATTENDED = "not_attended"
    BAD_STARS = "bad_stars"
    TOO_LONG = "too_long"


class FeedbackError(Exception):
    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.value)
        self.problem = problem


class FeedbackService:
    def __init__(self, db: Database, store: SettingsStore, clock: Callable[[], datetime] | None = None) -> None:
        self.db, self.store = db, store
        self.clock = clock or (lambda: datetime.now(UTC))

    def closes_at(self, event: Event) -> datetime | None:
        if event.reported_at is None or event.attendance_state is None:
            return None
        return event.reported_at + timedelta(days=self.store.current.attendance.rating_days)

    def is_open(self, event: Event) -> bool:
        closes = self.closes_at(event)
        return closes is not None and self.clock() < closes

    async def may_rate(self, event: Event, member_id: int) -> Problem | None:
        if event.attendance_state is None or event.reported_at is None:
            return Problem.NOT_OPEN
        if not self.is_open(event):
            return Problem.CLOSED
        async with self.db.session() as session:
            entries = (await session.execute(select(AttendanceEntry).where(
                AttendanceEntry.event_id == event.id, AttendanceEntry.member_id == member_id))).scalars().all()
        ok = any(e.holder for e in entries) if event.kind == "guest" else any(e.status in ATTENDED for e in entries)
        return None if ok else Problem.NOT_ATTENDED

    async def rate(self, event: Event, member_id: int, stars_text: str, comment: str) -> MissionRating:
        problem = await self.may_rate(event, member_id)
        if problem is not None:
            raise FeedbackError(problem)
        text = stars_text.strip().rstrip("*★")
        if not text.isdigit() or not 1 <= int(text) <= 5:
            raise FeedbackError(Problem.BAD_STARS)
        comment = comment.strip()
        if len(comment) > MAX_COMMENT:
            raise FeedbackError(Problem.TOO_LONG)
        async with self.db.session() as session, session.begin():
            row = (await session.execute(select(MissionRating).where(
                MissionRating.event_id == event.id, MissionRating.member_id == member_id))).scalar_one_or_none()
            if row is None:
                row = MissionRating(event_id=event.id, member_id=member_id, stars=int(text), comment=comment)
                session.add(row)
            else:
                row.stars, row.comment = int(text), comment
        return row

    async def mine(self, event_id: int, member_id: int) -> MissionRating | None:
        async with self.db.session() as session:
            return (await session.execute(select(MissionRating).where(
                MissionRating.event_id == event_id, MissionRating.member_id == member_id))).scalar_one_or_none()

    async def ratings(self, event_id: int) -> list[MissionRating]:
        async with self.db.session() as session:
            return list((await session.execute(select(MissionRating).where(
                MissionRating.event_id == event_id).order_by(MissionRating.created_at))).scalars())

    async def summary(self, event_id: int) -> tuple[float | None, int]:
        """The average and the number of ratings."""
        async with self.db.session() as session:
            average, count = (await session.execute(select(func.avg(MissionRating.stars), func.count(MissionRating.id))
                                                    .where(MissionRating.event_id == event_id))).one()
        return (float(average) if average is not None else None), int(count)

    async def forget(self, event_id: int, member_id: int) -> bool:
        """A correction took *attended* away: the member's rating goes with it."""
        async with self.db.session() as session, session.begin():
            result = await session.execute(delete(MissionRating).where(
                MissionRating.event_id == event_id, MissionRating.member_id == member_id))
        return bool(result.rowcount)
