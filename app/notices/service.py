"""The supervisor's notices: what it wants said on Discord (docs/PLAN-front-end.md, *Notices*).

``GET /api/notices?after=<id>`` (oldest first), each one delivered by the handler for its ``kind``, then
``POST /api/notices/{id}/done`` with ``delivered`` or ``failed`` and a reason. The rules:

- **At most once.** A ``notice_delivery`` row is written before the handler acts. One still ``started`` after a
  restart was cut off by a crash: it is reported ``failed`` (``bot_crashed``), never delivered again. That holds for
  offers and login links too: the supervisor re-offers, and staff share a login link in the app.
- **In order.** The cursor (``bot_state`` ``notices``) moves past a notice only once the supervisor has heard its
  outcome; if it can't be told (down), the batch stops there and the next run reports it without delivering again.
- A notice past its ``expires_at`` by the bot's clock is reported ``failed`` (``expired``) without being said.
- A handler that can't work yet (the supervisor down halfway) raises :class:`Later`: the row is dropped, nothing
  was said, the next run tries again. Any other exception is a ``failed`` delivery with the error as the reason.
- A kind without a handler is reported ``failed`` (``unsupported``), so the supervisor knows nobody heard it.

A ``notice`` live event (``app.live``) wakes the worker; it also looks every minute."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete

from app.db import Database
from app.db import state
from app.db.models import NoticeDelivery
from app.supervisor_client import SupervisorClient, SupervisorError, SupervisorUnreachable

log = logging.getLogger(__name__)
CURSOR = "notices"
POLL = timedelta(minutes=1)
KEEP = timedelta(days=30)
DELIVERED, FAILED, STARTED = "delivered", "failed", "started"

Outcome = tuple[str, str | None]  # (DELIVERED, None) or (FAILED, reason)
Handler = Callable[[dict[str, Any]], Awaitable[Outcome]]


class Later(Exception):
    """The handler couldn't start its work (nothing was said): try the notice again on the next run."""


def delivered() -> Outcome:
    return DELIVERED, None


def failed(reason: str) -> Outcome:
    return FAILED, reason[:200]


class Notices:
    def __init__(self, db: Database, client: SupervisorClient, clock: Callable[[], datetime],
                 poll: timedelta = POLL) -> None:
        self.db, self.client, self.clock, self.poll = db, client, clock, poll
        self.handlers: dict[str, Handler] = {}
        self._wake = asyncio.Event()
        self._down = False

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def wake(self) -> None:
        self._wake.set()

    # ---------------------------------------------------------------- one run

    async def run_once(self) -> int:
        """Deliver what waits; returns how many notices were finished (reported). Raises SupervisorUnreachable if
        the list can't be read."""
        await self._prune()
        after = int((await state.get(self.db, CURSOR) or {}).get("after", 0))
        finished = 0
        for notice in await self.client.notices(after):
            if not await self._handle(notice):
                break
            after = int(notice["id"])
            await state.put(self.db, CURSOR, {"after": after})
            finished += 1
        return finished

    async def _handle(self, notice: dict[str, Any]) -> bool:
        """Deliver (or recognise as done) and report one notice. False: stop the batch here."""
        notice_id = int(notice["id"])
        row = await self._row(notice_id)
        if row is None:
            if self._expired(notice):
                await self._write(notice, FAILED, "expired", finished=True)
            else:
                await self._write(notice, STARTED, None, finished=False)
                try:
                    outcome = await self._deliver(notice)
                except (Later, SupervisorUnreachable) as exc:
                    log.info("Notice %s (%s) waits: %s", notice_id, notice.get("kind"), exc)
                    await self._drop(notice_id)
                    return False
                await self._write(notice, *outcome, finished=True)
        elif row.state == STARTED:  # a run that crashed while saying it
            log.warning("Notice %s (%s) was cut off by a restart: reported as failed", notice_id, row.kind)
            await self._write(notice, FAILED, "bot_crashed", finished=True)
        return await self._report(notice_id)

    async def _deliver(self, notice: dict[str, Any]) -> Outcome:
        handler = self.handlers.get(str(notice.get("kind")))
        if handler is None:
            log.warning("Notice %s has a kind this bot doesn't know: %s", notice["id"], notice.get("kind"))
            return failed("unsupported")
        try:
            return await handler(notice)
        except (Later, SupervisorUnreachable):
            raise
        except Exception as exc:  # noqa: BLE001 - one notice must not stop the others
            log.exception("Delivering notice %s (%s) failed", notice["id"], notice.get("kind"))
            return failed(f"error: {exc}")

    def _expired(self, notice: dict[str, Any]) -> bool:
        expires = notice.get("expires_at")
        if not expires:
            return False
        try:
            return datetime.fromisoformat(expires) <= self.clock()
        except (TypeError, ValueError):
            return False

    async def _report(self, notice_id: int) -> bool:
        row = await self._row(notice_id)
        if row is None or row.reported_at is not None:
            return True
        try:
            await self.client.notice_done(notice_id, row.state, row.reason)
        except SupervisorUnreachable:
            return False
        except SupervisorError as exc:  # gone or refused: telling it again won't change that
            log.warning("The supervisor refused the outcome of notice %s: %s", notice_id, exc.message)
        async with self.db.session() as session, session.begin():
            found = await session.get(NoticeDelivery, notice_id)
            if found is not None:
                found.reported_at = self.clock()
        return True

    # ---------------------------------------------------------------- rows

    async def _row(self, notice_id: int) -> NoticeDelivery | None:
        async with self.db.session() as session:
            return await session.get(NoticeDelivery, notice_id)

    async def _write(self, notice: dict[str, Any], state_: str, reason: str | None, *, finished: bool) -> None:
        now = self.clock()
        async with self.db.session() as session, session.begin():
            row = await session.get(NoticeDelivery, int(notice["id"]))
            if row is None:
                row = NoticeDelivery(notice_id=int(notice["id"]), kind=str(notice.get("kind"))[:40], started_at=now)
                session.add(row)
            row.state, row.reason = state_, reason
            row.finished_at = now if finished else None

    async def _drop(self, notice_id: int) -> None:
        async with self.db.session() as session, session.begin():
            await session.execute(delete(NoticeDelivery).where(NoticeDelivery.notice_id == notice_id))

    async def _prune(self) -> None:
        """The supervisor forgets notices after 30 days; so does the bot."""
        async with self.db.session() as session, session.begin():
            await session.execute(delete(NoticeDelivery).where(
                NoticeDelivery.reported_at.is_not(None), NoticeDelivery.reported_at < self.clock() - KEEP))

    # ---------------------------------------------------------------- the loop

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.run_once()
                if self._down:
                    log.info("Notices: the supervisor answers again")
                self._down = False
            except SupervisorError as exc:
                if not self._down:  # a supervisor that is down is not news; a refused token is
                    level = logging.ERROR if exc.status in (401, 403) else logging.WARNING
                    log.log(level, "Reading the supervisor's notices failed: %s", exc.message)
                self._down = True
            except Exception:  # noqa: BLE001 - the loop must survive a database hiccup
                log.exception("The notices loop failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll.total_seconds())
            except TimeoutError:
                pass
