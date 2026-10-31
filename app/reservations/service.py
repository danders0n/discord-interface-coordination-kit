"""An event's host reservation on the supervisor (docs/PLAN.md, *Supervisor: reservation*).

:meth:`ReservationService.book` asks for (or moves) the reservation of one event and keeps the outcome in
``host_reservation``. The event goes on whatever happens:

- the supervisor can't be reached: the state is ``retrying`` and a scheduled action tries again, with a growing
  delay, until the reservation would be over;
- it refuses (409 for a clash, or any other error): the state is ``failed`` with the supervisor's message, staff
  are told, and nothing retries until someone presses *Retry reservation* (:meth:`retry`).

From the start of the quiet period the supervisor keeps an occurrence's times and refuses to delete it, so
:meth:`book` refuses new times then (:class:`Frozen`), and :meth:`cancel` ends it before deleting it. With the
``managed_refs`` capability every reservation carries ``managed_by`` / ``external_ref``, so a create that is
retried after a lost answer finds the one it made; without it the bot looks for one with the same name and start."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db import Database
from app.db.models import HostReservation
from app.i18n import t
from app.scheduler import Action, Scheduler
from app.settings import SettingsStore
from app.supervisor_client import SupervisorClient, SupervisorError, SupervisorUnreachable

MANAGED_BY = "discord-bot"
QUIET = timedelta(minutes=30)  # the supervisor's quiet period before the start (sent explicitly)
WARN = timedelta(minutes=15)
NAME_LIMIT = 80
RETRY_KIND = "reservation_retry"
CANCEL_KIND = "reservation_cancel"
log = logging.getLogger(__name__)


class Frozen(Exception):
    """The reservation's quiet period has begun: its times can't change any more."""


@dataclass(frozen=True)
class Window:
    name: str
    starts_at: datetime
    ends_at: datetime
    link: str | None = None
    profile_id: int | None = None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class ReservationService:
    def __init__(self, db: Database, client: SupervisorClient, store: SettingsStore, scheduler: Scheduler,
                 notify: Callable[[str], None], clock: Callable[[], datetime] | None = None) -> None:
        self.db = db
        self.client = client
        self.store = store
        self.scheduler = scheduler
        self.notify = notify
        self.clock = clock or scheduler.clock
        scheduler.register(RETRY_KIND, self._retry_action)
        scheduler.register(CANCEL_KIND, self._cancel_action)

    # ---------------------------------------------------------------- reading

    async def get(self, event_id: int) -> HostReservation | None:
        async with self.db.session() as session:
            return await session.get(HostReservation, event_id)

    @staticmethod
    def frozen(row: HostReservation, now: datetime) -> bool:
        return now >= row.starts_at - QUIET

    # ---------------------------------------------------------------- booking

    async def book(self, event_id: int, window: Window) -> HostReservation:
        """Ask for the event's reservation, or move it to ``window``. Raises :class:`Frozen` for new times once
        the quiet period has begun."""
        now = self.clock()
        name = window.name[:NAME_LIMIT]
        async with self.db.session() as session, session.begin():
            row = await session.get(HostReservation, event_id)
            if row is None:
                row = HostReservation(event_id=event_id)
                session.add(row)
            elif row.reservation_id is not None and row.state == "booked" and self.frozen(row, now) and \
                    (row.starts_at, row.ends_at) != (window.starts_at, window.ends_at):
                raise Frozen(row.name)
            row.name, row.starts_at, row.ends_at = name, window.starts_at, window.ends_at
            row.link, row.profile_id = window.link, window.profile_id
            row.state, row.error = "booked" if row.reservation_id else "retrying", None
        return await self._push(event_id)

    async def retry(self, event_id: int) -> HostReservation | None:
        """*Retry reservation*: try again with what was asked last."""
        if await self.get(event_id) is None:
            return None
        return await self._push(event_id)

    def _body(self, row: HostReservation) -> dict[str, Any]:
        settings = self.store.current
        body: dict[str, Any] = {
            "name": row.name, "server_id": settings.supervisor.event_server, "repeat": "once",
            "starts_at": _iso(row.starts_at), "ends_at": _iso(row.ends_at),
            "quiet_minutes": int(QUIET.total_seconds() // 60), "warn_minutes": int(WARN.total_seconds() // 60),
            "public": False, "note": f"Discord event {row.event_id}", "enabled": True,
        }
        info = self.client.info
        if info.has("managed_refs"):
            body |= {"managed_by": MANAGED_BY, "external_ref": f"event:{row.event_id}", "link": row.link}
        if info.has("reservation_profile"):
            body["profile_id"] = row.profile_id
        return body

    async def _push(self, event_id: int) -> HostReservation:
        row = await self.get(event_id)
        assert row is not None
        if not self.store.current.supervisor.event_server:
            return await self._failed(row, t("no event server is configured (BOT_SUPERVISOR__EVENT_SERVER)",
                                             self.store.current.language))
        try:
            await self.client.ensure_info()
            body = self._body(row)
            result = await self._send(row, body)
        except SupervisorUnreachable as exc:
            return await self._unreachable(row, exc)
        except SupervisorError as exc:
            return await self._failed(row, exc.message)
        await self.scheduler.cancel(f"{RETRY_KIND}:{event_id}")
        return await self._save(event_id, reservation_id=result["id"], state="booked", error=None)

    async def _send(self, row: HostReservation, body: dict[str, Any]) -> dict[str, Any]:
        if row.reservation_id is not None:
            try:
                return await self.client.update_reservation(row.reservation_id, body)
            except SupervisorError as exc:
                if exc.status != 404:
                    raise
                row.reservation_id = None  # deleted in the console meanwhile: make it again
        found = None if self.client.info.has("managed_refs") else await self._find_by_name(row)
        if found is None:
            found = await self.client.create_reservation(body)
        if (found["name"], _parse(found["starts_at"]), _parse(found["ends_at"])) != (row.name, row.starts_at, row.ends_at):
            found = await self.client.update_reservation(found["id"], body)  # an earlier one of ours, now moved
        return found

    async def _find_by_name(self, row: HostReservation) -> dict[str, Any] | None:
        """Without ``managed_refs``: a reservation made by a create whose answer was lost has our name and start."""
        for item in await self.client.reservations():
            if item.get("name") == row.name and item.get("starts_at") and _parse(item["starts_at"]) == row.starts_at:
                return item
        return None

    async def _unreachable(self, row: HostReservation, exc: SupervisorError) -> HostReservation:
        first = row.state != "retrying" or row.error is None
        if self.clock() >= row.ends_at:
            return await self._failed(row, exc.message)
        if first:
            log.warning("Reservation for event %s: %s; retrying", row.event_id, exc.message)
            await self.scheduler.schedule(RETRY_KIND, self.clock() + timedelta(minutes=1),
                                          key=f"{RETRY_KIND}:{row.event_id}", event_id=row.event_id)
        return await self._save(row.event_id, state="retrying", error=exc.message)

    async def _failed(self, row: HostReservation, reason: str) -> HostReservation:
        await self.scheduler.cancel(f"{RETRY_KIND}:{row.event_id}")
        self.notify(t("No reservation for {name}: {reason}", self.store.current.language, name=row.name,
                      reason=reason))
        return await self._save(row.event_id, state="failed", error=reason)

    async def _retry_action(self, action: Action, now: datetime) -> datetime | None:
        row = await self.get(action.event_id)  # type: ignore[arg-type]
        if row is None or row.state != "retrying":
            return None
        if now >= row.ends_at:
            await self._failed(row, row.error or "")
            return None
        try:
            await self.client.ensure_info()
            result = await self._send(row, self._body(row))
        except SupervisorUnreachable as exc:
            await self._save(row.event_id, error=exc.message)
            raise  # the scheduler retries with a growing delay
        except SupervisorError as exc:
            await self._failed(row, exc.message)
            return None
        await self._save(row.event_id, reservation_id=result["id"], state="booked", error=None)
        return None

    # ---------------------------------------------------------------- ending and cancelling

    async def end(self, event_id: int) -> bool:
        """End the reservation now (the event is over, or *End reservation*). False when it wasn't under way."""
        row = await self.get(event_id)
        if row is None or row.reservation_id is None:
            return False
        ended = await self.client.end_reservation(row.reservation_id)
        if ended:
            await self._save(event_id, state="ended")
        return ended

    async def cancel(self, event_id: int) -> None:
        """Delete the event's reservation (the event was cancelled or rejected). One under way is ended first;
        an unreachable supervisor is tried again later."""
        row = await self.get(event_id)
        if row is None:
            return
        await self.scheduler.cancel(f"{RETRY_KIND}:{event_id}")
        if row.reservation_id is None:
            await self._save(event_id, state="cancelled", error=None)
            return
        await self._save(event_id, state="cancelling")
        try:
            await self._delete(row.reservation_id)
        except SupervisorUnreachable:
            await self.scheduler.schedule(CANCEL_KIND, self.clock() + timedelta(minutes=1),
                                          key=f"{CANCEL_KIND}:{event_id}", event_id=event_id)
            return
        except SupervisorError as exc:  # staff delete it in the console; the event is cancelled either way
            self.notify(t("Could not delete the reservation of {name}: {reason}", self.store.current.language,
                          name=row.name, reason=exc.message))
            await self._save(event_id, state="failed", error=exc.message)
            return
        await self._save(event_id, state="cancelled", reservation_id=None, error=None)

    async def _delete(self, reservation_id: int) -> None:
        try:
            await self.client.delete_reservation(reservation_id)
        except SupervisorError as exc:
            if exc.status == 404:
                return
            if exc.status != 409:
                raise
            await self.client.end_reservation(reservation_id)  # under way: end it, then it can go
            await self.client.delete_reservation(reservation_id)

    async def _cancel_action(self, action: Action, now: datetime) -> None:
        row = await self.get(action.event_id)  # type: ignore[arg-type]
        if row is None or row.state != "cancelling" or row.reservation_id is None:
            return None
        await self._delete(row.reservation_id)  # unreachable: raises, and the scheduler tries again
        await self._save(row.event_id, state="cancelled", reservation_id=None, error=None)
        return None

    # ---------------------------------------------------------------- storage

    async def _save(self, event_id: int, **values: Any) -> HostReservation:
        async with self.db.session() as session, session.begin():
            row = await session.get(HostReservation, event_id)
            assert row is not None
            for name, value in values.items():
                setattr(row, name, value)
        return row
