"""Scheduled actions (docs/PLAN.md, *Scheduled actions and downtime*): nothing timed lives in memory.

Every timed action is a ``scheduled_action`` row; one loop runs whatever is due. After downtime the rows are simply
late: a handler gets ``late`` and decides (state changes always run, pings may skip). A handler returns the next
time to run the same action (a repeating one, such as the daily backup) or None when it is done. A failing action
is retried with a growing delay, up to an hour; its first failure is an ERROR, so it reaches the staff log."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select

from app.db import Database
from app.db.models import ScheduledAction

log = logging.getLogger(__name__)
MAX_BACKOFF = timedelta(hours=1)


@dataclass(frozen=True)
class Action:
    id: int
    kind: str
    key: str | None
    operation_id: int | None
    due_at: datetime
    attempts: int
    payload: dict[str, Any] = field(default_factory=dict)

    def late(self, now: datetime) -> timedelta:
        return now - self.due_at


Handler = Callable[[Action, datetime], Awaitable[datetime | None]]


def backoff(attempts: int) -> timedelta:
    return min(timedelta(minutes=2 ** max(attempts - 1, 0)), MAX_BACKOFF)


class Scheduler:
    def __init__(self, db: Database, clock: Callable[[], datetime] | None = None,
                 poll: timedelta = timedelta(seconds=30)) -> None:
        self.db = db
        self.clock = clock or (lambda: datetime.now(UTC))
        self.poll = poll
        self.handlers: dict[str, Handler] = {}
        self._wake = asyncio.Event()

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    async def schedule(self, kind: str, due_at: datetime, *, key: str | None = None,
                       operation_id: int | None = None, payload: dict[str, Any] | None = None) -> int:
        """Add an action, or with a ``key`` that exists, move that one (it runs again even if it was done)."""
        async with self.db.session() as session, session.begin():
            row = None
            if key is not None:
                row = (await session.execute(select(ScheduledAction).where(ScheduledAction.key == key))).scalar_one_or_none()
            if row is None:
                row = ScheduledAction(kind=kind, key=key, operation_id=operation_id, due_at=due_at, payload=payload or {},
                                      attempts=0)
                session.add(row)
            else:
                row.kind, row.operation_id, row.due_at = kind, operation_id, due_at
                row.payload = payload or {}
                row.done_at, row.attempts, row.last_error = None, 0, None
            await session.flush()
            action_id = row.id
        self._wake.set()
        return action_id

    async def exists(self, key: str) -> bool:
        """An action with this key that hasn't finished."""
        async with self.db.session() as session:
            row = (await session.execute(select(ScheduledAction).where(ScheduledAction.key == key))).scalar_one_or_none()
            return row is not None and row.done_at is None

    async def cancel(self, key: str) -> bool:
        async with self.db.session() as session, session.begin():
            row = (await session.execute(select(ScheduledAction).where(ScheduledAction.key == key))).scalar_one_or_none()
            if row is None or row.done_at is not None:
                return False
            await session.delete(row)
            return True

    async def cancel_all(self, kind: str, operation_id: int, keep: Iterable[str] = ()) -> int:
        """Drop the operation's unfinished actions of ``kind``, except the keys in ``keep``; returns how many."""
        keep = set(keep)
        async with self.db.session() as session, session.begin():
            rows = (await session.execute(select(ScheduledAction).where(
                ScheduledAction.kind == kind, ScheduledAction.operation_id == operation_id,
                ScheduledAction.done_at.is_(None)))).scalars().all()
            gone = [row for row in rows if row.key not in keep]
            for row in gone:
                await session.delete(row)
            return len(gone)

    async def prune(self, older_than: timedelta = timedelta(days=30)) -> int:
        """Delete the actions done longer ago than that (the table would grow for ever); returns how many."""
        async with self.db.session() as session, session.begin():
            done = await session.execute(delete(ScheduledAction).where(
                ScheduledAction.done_at.is_not(None), ScheduledAction.done_at < self.clock() - older_than))
        return done.rowcount

    async def run_due(self) -> int:
        """Run every action that is due, oldest first. Returns how many ran (failed ones included)."""
        now = self.clock()
        async with self.db.session() as session:
            rows = (await session.execute(
                select(ScheduledAction).where(ScheduledAction.done_at.is_(None), ScheduledAction.due_at <= now)
                .order_by(ScheduledAction.due_at, ScheduledAction.id))).scalars().all()
            due = [Action(r.id, r.kind, r.key, r.operation_id, r.due_at, r.attempts, dict(r.payload)) for r in rows]
        for action in due:
            await self._run_one(action, now)
        return len(due)

    async def _run_one(self, action: Action, now: datetime) -> None:
        handler = self.handlers.get(action.kind)
        try:
            if handler is None:
                raise LookupError(f"No handler for scheduled action '{action.kind}'")
            next_due = await handler(action, now)
        except Exception as exc:  # noqa: BLE001 - one failing action must not stop the others
            attempts = action.attempts + 1
            level = logging.ERROR if attempts == 1 else logging.WARNING
            log.log(level, "Scheduled action %s (%s) failed, attempt %d: %s", action.id, action.kind, attempts, exc,
                    exc_info=level == logging.ERROR)
            await self._update(action.id, attempts=attempts, last_error=str(exc)[:2000],
                               due_at=self.clock() + backoff(attempts))
            return
        if next_due is None:
            await self._update(action.id, done_at=self.clock())
        else:
            await self._update(action.id, due_at=next_due, attempts=0, last_error=None)

    async def _update(self, action_id: int, **values: Any) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(ScheduledAction, action_id)
            if row is not None:  # (a handler may have cancelled its own action)
                for name, value in values.items():
                    setattr(row, name, value)

    async def run(self) -> None:
        """The loop: run what is due, then sleep until the poll interval or a new schedule."""
        while True:
            try:
                await self.run_due()
            except Exception:  # noqa: BLE001 - the loop itself must survive a database hiccup
                log.exception("Scheduler loop failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll.total_seconds())
            except TimeoutError:
                pass
