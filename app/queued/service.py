"""Members' writes queued while the supervisor is away (docs/PLAN-remote-update.md, *Queued actions*). No Discord calls
(the words come from ``discord_util.supervisor_words``; telling the member is ``tell``'s).

A write is queued only when it can't have reached the supervisor: the health says it is updating or down before
sending, or the call failed with a refused connection (``SupervisorUnreachable.sent is False``). A timeout is never
queued: it may have been applied. A member who still has queued rows has their next write queued too, so their
writes reach the supervisor in the order they made them.

A row is a client method from :data:`QUEUEABLE` and its arguments after the member's id, so the call made later is the
very one that would have been made then, in their name (``Roster.as_member``), with the ``If-Match`` revision it was
typed against. When the health turns ``up`` the rows go out oldest first; ``tell(row id, member, text)`` gives the
member the answer (app/queued/ui.py: in place of the *Queued* reply, or by DM). A row with a ``collapse`` key replaces
the member's queued rows with the same key (their last sign-up choice for an operation counts). Rows older than
``queue.max_minutes`` are not sent; the member hears it."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update

from app.db.models import QueuedAction
from app.discord_util import supervisor_words
from app.i18n import t
from app.supervisor_client import SupervisorError, SupervisorUnreachable

if TYPE_CHECKING:
    from app.db import Database
    from app.health import SupervisorHealth
    from app.settings import SettingsStore
    from app.supervisor_client import SupervisorClient

log = logging.getLogger(__name__)

# The writes worth sending late (docs/PLAN-remote-update.md, *Queued actions*): each takes the member's id first.
QUEUEABLE = frozenset({
    "answer", "leave", "take_slot", "accept_offer", "decline_offer",  # sign-up
    "rate", "correct_attendance", "approve_request", "reject_request",
    "update_operation", "set_opord", "set_organisers", "set_orbat",  # organisers (with If-Match)
    "operation_step", "cancel_operation", "end_operation",
})
KEEP = timedelta(days=7)
TICK = 60.0
MAX_TYPED = 1500

Tell = Callable[[int, int, str], Awaitable[None]]


def typed(args: Any) -> list[str]:
    """The texts among a call's arguments (what an organiser typed), for a refusal that would otherwise lose them."""
    if isinstance(args, str):
        return [args] if args.strip() else []
    if isinstance(args, dict):
        return [line for value in args.values() for line in typed(value)]
    if isinstance(args, (list, tuple)):
        return [line for value in args for line in typed(value)]
    return []


class Queue:
    def __init__(self, db: Database, client: SupervisorClient, roster: Any, store: SettingsStore,
                 clock: Callable[[], datetime], health: SupervisorHealth | None = None) -> None:
        self.db, self.client, self.roster, self.store, self.clock, self.health = db, client, roster, store, clock, health
        self.tell: Tell | None = None
        self._wake = asyncio.Event()
        self._flushing = asyncio.Lock()
        if health is not None:
            health.listeners.append(lambda state: self.wake() if state == "up" else None)

    def wake(self) -> None:
        self._wake.set()

    # ---------------------------------------------------------------- queuing

    async def waiting(self, discord_id: int) -> int:
        """How many of the member's writes wait."""
        async with self.db.session() as session:
            return int(await session.scalar(select(func.count()).select_from(QueuedAction).where(
                QueuedAction.discord_id == discord_id, QueuedAction.state == "queued")) or 0)

    async def must_wait(self, discord_id: int) -> bool:
        """Queue rather than send: the supervisor is away, or the member's earlier writes still wait."""
        return (self.health is not None and self.health.away) or await self.waiting(discord_id) > 0

    async def add(self, discord_id: int, method: str, args: list[Any], done: str, locale: str,
                  collapse: str | None = None) -> tuple[int | None, list[int]]:
        """(the new row's id, or None when the member has too many waiting; the ids of rows it replaced)."""
        if method not in QUEUEABLE:
            raise ValueError(f"{method} is not queued")
        settings = self.store.current.queue
        now = self.clock()
        async with self.db.session() as session, session.begin():
            replaced: list[int] = []
            if collapse:
                replaced = list((await session.scalars(select(QueuedAction.id).where(
                    QueuedAction.discord_id == discord_id, QueuedAction.collapse == collapse,
                    QueuedAction.state == "queued"))).all())
                if replaced:
                    await session.execute(update(QueuedAction).where(QueuedAction.id.in_(replaced)).values(
                        state="replaced", finished_at=now))
            count = await session.scalar(select(func.count()).select_from(QueuedAction).where(
                QueuedAction.discord_id == discord_id, QueuedAction.state == "queued"))
            if int(count or 0) >= settings.per_member:
                return None, []
            row = QueuedAction(discord_id=discord_id, method=method, args={"args": args}, done=done, locale=locale,
                               collapse=collapse, created_at=now, expires_at=now + timedelta(minutes=settings.max_minutes))
            session.add(row)
            await session.flush()
            row_id = row.id
        log.info("Queued %s for %s (row %s)", method, discord_id, row_id)
        return row_id, replaced

    # ---------------------------------------------------------------- sending

    async def _say(self, row: QueuedAction, text: str) -> None:
        if self.tell is not None:
            try:
                await self.tell(row.id, row.discord_id, text)
            except Exception:  # noqa: BLE001 - the row is done either way
                log.exception("Telling %s about queued row %s failed", row.discord_id, row.id)

    async def _finish(self, row: QueuedAction, state: str, answer: str) -> None:
        async with self.db.session() as session, session.begin():
            await session.execute(update(QueuedAction).where(QueuedAction.id == row.id).values(
                state=state, answer=answer[:4000], finished_at=self.clock()))
        await self._say(row, answer)

    async def expire(self) -> int:
        """Rows past their time: not sent, the member told."""
        now = self.clock()
        async with self.db.session() as session:
            rows = (await session.scalars(select(QueuedAction).where(
                QueuedAction.state == "queued", QueuedAction.expires_at <= now).order_by(QueuedAction.id))).all()
        for row in rows:
            await self._finish(row, "expired", t("Not sent: the app was away too long. Press again.", row.locale))
        return len(rows)

    async def flush(self) -> int:
        """Send what waits, oldest first, until the supervisor is away again. Returns how many were finished."""
        async with self._flushing:
            await self.expire()
            async with self.db.session() as session:
                rows = (await session.scalars(select(QueuedAction).where(QueuedAction.state == "queued")
                                              .order_by(QueuedAction.id))).all()
            finished = 0
            for row in rows:
                if self.health is not None and self.health.state != "up":
                    break
                args = list((row.args or {}).get("args") or [])
                method = getattr(self.client, row.method)
                try:
                    body = await self.roster.as_member(row.discord_id, lambda me, m=method, a=args: m(me, *a))
                except SupervisorUnreachable as exc:
                    if exc.sent is False:
                        break  # away again: it stays queued
                    await self._finish(row, "failed", t(
                        "The app did not answer in time: check whether this went through.", row.locale))
                except SupervisorError as exc:
                    await self._finish(row, "failed", self.refusal(row, exc, args))
                else:
                    await self._finish(row, "sent", self.success(row, body))
                finished += 1
            await self.prune()
            return finished

    def success(self, row: QueuedAction, body: Any) -> str:
        text = row.done or t("Done.", row.locale)
        missing = ((body.get("outcome") or {}).get("missing_dlc") or []) if isinstance(body, dict) else []
        if missing:
            text += "\n" + t("⚠ This mission needs Creator DLC you have no role for: {dlc}.", row.locale,
                             dlc=", ".join(str(m) for m in missing))
        return t("Sent now that the app is back: {text}", row.locale, text=text)

    def refusal(self, row: QueuedAction, error: SupervisorError, args: list[Any]) -> str:
        if error.code == "stale":
            lines = typed(args[1:])  # (after the operation's id)
            text = t("Not saved: the operation changed while the app was away. Open it again and redo the change.",
                     row.locale)
            if lines:
                text += "\n" + t("What you sent:", row.locale) + "\n" + "\n".join(lines)[:MAX_TYPED]
            return text
        return t("Not done now that the app is back: {reason}", row.locale,
                 reason=supervisor_words(error, row.locale))

    async def prune(self) -> int:
        async with self.db.session() as session, session.begin():
            done = await session.execute(delete(QueuedAction).where(
                QueuedAction.state != "queued", QueuedAction.finished_at < self.clock() - KEEP))
        return done.rowcount

    async def run(self) -> None:
        """Every minute and whenever the supervisor is back: expire, then send what waits if it is up."""
        while True:
            try:
                if self.health is None or self.health.state == "up":
                    await self.flush()
                else:
                    await self.expire()
            except Exception:  # noqa: BLE001 - the loop must survive a database hiccup
                log.exception("Sending queued actions failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), TICK)
            except TimeoutError:
                pass
