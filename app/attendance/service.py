"""Attendance (docs/PLAN.md, *Supervisor: attendance*), without discord.py.

- **Trigger.** A published internal, hosted or Zeus event is watched every minute from its assembly. Whichever
  comes first ends it: (a) a play in the window ends ``finished`` and no new play starts within the settle delay,
  counted only after start + a share of the duration (a ``finished`` before that point counts once the point
  passes with no new play since); (b) the reservation ends, at its end time or ended by hand (*End reservation*,
  *End event*); with neither, start + duration + buffer. A Zeus event, and one with *Don't end automatically*,
  ignore (a). Nothing is remembered between checks: each one reads the plays again, so a restart loses nothing.
- **Reading.** For (a) the settle delay is over already, so the stats are read at once; otherwise after it. An
  unreachable supervisor is retried until the deadline, then the report says the stats are unavailable.
- **Report.** Slot holders (in a Zeus event those *Coming*) are attended with at least N minutes of mission time,
  else no-show, or not linked. Players without a slot: linked members by name, unknown Steam IDs by in-game name,
  except in a hosted event, where they are the invited groups' players and only counted. An unknown player whose
  in-game name matches an unlinked holder's names is offered as *probably* that member.
- **Guest events** are on another group's server: at start + duration + buffer every slot holder is *to confirm*;
  the organisers are pinged after 24 h if some still are, and after N days those count as attended.
- **Corrections**: organisers move members between attended, no-show and excused, and confirm probable links.

The Discord side hears through :class:`AttendanceNotifier`; refusals are the events service's ``Problem`` codes."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import delete, select

from app.attendance.stats import Play, Player, Stats, StatsReader
from app.db import Database
from app.db.models import AttendanceEntry, Event, Signup, Slot
from app.events.service import EventError, Problem
from app.links.service import LinkError, LinkService
from app.reservations.service import ReservationService
from app.scheduler import Action, Scheduler
from app.settings import SettingsStore
from app.signup.service import COMING, Member
from app.supervisor_client import SupervisorError

log = logging.getLogger(__name__)
WATCH, READ, CONFIRM_PING, AUTO_CONFIRM = ("attendance_watch", "attendance_read", "attendance_confirm_ping",
                                           "attendance_autoconfirm")
POLL = timedelta(minutes=1)
RETRY = timedelta(minutes=5)
PING_AFTER = timedelta(hours=24)
CORRECTABLE = ("attended", "no_show", "excused")


class AttendanceNotifier(Protocol):
    async def changed(self, event_id: int) -> None: ...  # redraw (the info embed says ENDED, the report)
    async def attendance_reported(self, event_id: int) -> None: ...  # staff summary, the archive after the delay
    async def attendance_unconfirmed(self, event_id: int) -> None: ...  # a guest event: ping the organisers
    async def attendance_confirmed(self, event_id: int, count: int) -> None: ...  # ... after N days: staff note


def finished_at(plays: Iterable[Play], now: datetime, settle: timedelta, earliest: datetime) -> datetime | None:
    """When trigger (a) fired, or None: the last ``finished`` play, no play begun since it ended, the settle delay
    over and the share of the duration passed."""
    plays = list(plays)
    finished = [p for p in plays if p.end == "finished" and p.ended_at is not None]
    if not finished:
        return None
    last = max(finished, key=lambda p: p.ended_at)
    if any(p.id != last.id and p.started_at >= last.ended_at for p in plays):
        return None  # the evening went on: a crash or a lobby after it doesn't bring it back
    due = max(last.ended_at + settle, earliest)
    return due if now >= due else None


@dataclass
class Line:
    """One report line before it is stored."""

    status: str
    member_id: int | None = None
    steam_id: str | None = None
    name: str = ""
    seconds: int = 0
    holder: bool = False
    probable_steam_id: str | None = None
    probable_name: str | None = None


def compute(holders: list[int], links: dict[int, str], players: list[Player], min_seconds: int, hosted: bool,
            names: Callable[[int], list[str]]) -> tuple[list[Line], int]:
    """The report's lines and, for a hosted event, how many guests played."""
    by_uid: dict[str, Player] = {}
    unnamed: list[Player] = []
    for p in players:
        if p.uid is None:
            unnamed.append(p)
        else:
            known = by_uid.get(p.uid)
            by_uid[p.uid] = Player(p.uid, p.name or (known.name if known else ""), p.seconds + (known.seconds if known else 0))
    owners = {steam: member for member, steam in links.items()}
    lines, unlinked = [], []
    for member_id in dict.fromkeys(holders):
        steam = links.get(member_id)
        if steam is None:
            unlinked.append(member_id)
            continue
        played = by_uid.pop(steam, None)
        seconds = played.seconds if played else 0
        lines.append(Line("attended" if seconds >= min_seconds else "no_show", member_id, steam,
                          played.name if played else "", seconds, holder=True))
    strangers = [p for uid, p in by_uid.items() if uid not in owners] + unnamed
    for member_id in unlinked:
        wanted = {n.casefold() for n in names(member_id) if n}
        match = next((p for p in strangers if p.name and p.name.casefold() in wanted), None)
        line = Line("not_linked", member_id, holder=True)
        if match is not None:
            strangers.remove(match)
            by_uid.pop(match.uid, None)
            line.probable_steam_id, line.probable_name, line.seconds = match.uid, match.name, match.seconds
        lines.append(line)
    guests = 0
    for uid, p in by_uid.items():
        if p.seconds < min_seconds:
            continue
        if uid in owners:
            lines.append(Line("walk_in", owners[uid], uid, p.name, p.seconds))
        elif hosted:
            guests += 1
        else:
            lines.append(Line("walk_in", None, uid, p.name, p.seconds))
    for p in unnamed:
        if p in strangers and p.seconds >= min_seconds:
            if hosted:
                guests += 1
            else:
                lines.append(Line("walk_in", None, None, p.name, p.seconds))
    return lines, guests


class AttendanceService:
    def __init__(self, db: Database, store: SettingsStore, scheduler: Scheduler, reservations: ReservationService,
                 reader: StatsReader, links: LinkService, events: Any, notifier: AttendanceNotifier,
                 names: Callable[[int], list[str]] = lambda _m: [], clock: Callable[[], datetime] | None = None) -> None:
        """``events``: the EventService (who organises an event). ``names(member)``: the member's names on the
        server (nick, display name, username), for matching an unknown in-game name."""
        self.db, self.store, self.scheduler, self.reservations = db, store, scheduler, reservations
        self.reader, self.links, self.events, self.notifier, self.names = reader, links, events, notifier, names
        self.clock = clock or scheduler.clock
        self.feedback: Any = None  # app.feedback.service.FeedbackService: a correction may take a rating away
        for kind, handler in ((WATCH, self._watch), (READ, self._read), (CONFIRM_PING, self._confirm_ping),
                              (AUTO_CONFIRM, self._auto_confirm)):
            scheduler.register(kind, handler)

    @property
    def settings(self) -> Any:
        return self.store.current

    # ---------------------------------------------------------------- reading

    async def entries(self, event_id: int) -> list[AttendanceEntry]:
        async with self.db.session() as session:
            return list((await session.execute(select(AttendanceEntry).where(
                AttendanceEntry.event_id == event_id).order_by(AttendanceEntry.id))).scalars())

    async def _event(self, event_id: int | None) -> Event | None:
        async with self.db.session() as session:
            return await session.get(Event, event_id)

    async def _holders(self, event: Event) -> list[int]:
        async with self.db.session() as session:
            if event.kind in COMING:
                return list((await session.execute(select(Signup.member_id).where(
                    Signup.event_id == event.id, Signup.status == "coming").order_by(Signup.id))).scalars())
            return list((await session.execute(select(Slot.holder_id).where(
                Slot.event_id == event.id, Slot.holder_id.is_not(None)).order_by(Slot.squad_id, Slot.position))).scalars())

    # ---------------------------------------------------------------- the event's life

    async def published(self, event: Event) -> None:
        """Published, or its times moved: watch it from the assembly (a guest event has its own end)."""
        if event.kind != "guest":
            await self.scheduler.schedule(WATCH, event.assembly_at, key=f"{WATCH}:{event.id}", event_id=event.id)

    async def forget(self, event_id: int) -> None:
        """Cancelled: nothing to watch."""
        await self.scheduler.cancel(f"{WATCH}:{event_id}")

    async def _window_start(self, event: Event) -> datetime:
        booked = await self.reservations.get(event.id)
        if booked is not None and booked.state in ("booked", "ended"):
            return booked.starts_at
        return event.assembly_at - timedelta(minutes=self.settings.times.lead_minutes)

    async def end_now(self, event_id: int) -> None:
        """*End event*: the reservation ends, then the stats are read after the settle delay."""
        now = self.clock()
        await self._trigger(event_id, "manual", now, now + timedelta(minutes=self.settings.attendance.settle_minutes),
                            end_reservation=True)

    async def _trigger(self, event_id: int, how: str, at: datetime, read_at: datetime, end_reservation: bool) -> bool:
        event = await self._event(event_id)
        if event is None or event.status != "published":
            return False
        start = await self._window_start(event)
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event_id)
            row.status, row.attendance_state, row.attendance_trigger = "ended", "waiting", how
            row.attendance_from, row.attendance_to = start, max(at, start + timedelta(seconds=1))
        if end_reservation:
            await self.reservations.end(event_id)
        await self.scheduler.cancel(f"{WATCH}:{event_id}")
        await self.scheduler.schedule(READ, read_at, key=f"{READ}:{event_id}", event_id=event_id)
        await self.notifier.changed(event_id)
        log.info("Event %s ended (%s); attendance read at %s", event_id, how, read_at)
        return True

    async def _watch(self, action: Action, now: datetime) -> datetime | None:
        event = await self._event(action.event_id)
        if event is None or event.status != "published" or event.kind == "guest":
            return None
        a = self.settings.attendance
        settle = timedelta(minutes=a.settle_minutes)
        booked = await self.reservations.get(event.id)
        if booked is not None and booked.state == "ended":  # ended by hand (End reservation), a minute ago at most
            await self._trigger(event.id, "reservation", now, now + settle, end_reservation=False)
            return None
        if booked is not None and booked.state == "booked" and now >= booked.ends_at:
            await self._trigger(event.id, "reservation", booked.ends_at, booked.ends_at + settle, end_reservation=False)
            return None
        fallback = event.start_at + timedelta(minutes=event.duration_minutes + self.settings.times.buffer_minutes)
        if (booked is None or booked.state != "booked") and now >= fallback:
            await self._trigger(event.id, "fallback", fallback, fallback + settle, end_reservation=False)
            return None
        server = self.settings.supervisor.event_server
        if event.kind not in COMING and not event.dont_end_automatically and server and now >= event.start_at:
            earliest = event.start_at + timedelta(minutes=event.duration_minutes * a.finish_share_percent / 100)
            try:
                stats = await self.reader.read(server, await self._window_start(event), now, players=False)
            except (SupervisorError, LookupError) as exc:
                log.info("Plays of event %s not read: %s", event.id, exc)
                return now + POLL
            if finished_at(stats.plays, now, settle, earliest) is not None:
                await self._trigger(event.id, "finished", now, now, end_reservation=True)
                return None
        return now + POLL

    async def _read(self, action: Action, now: datetime) -> datetime | None:
        event = await self._event(action.event_id)
        if event is None or event.attendance_state != "waiting":
            return None
        server = self.settings.supervisor.event_server
        deadline = (event.attendance_to or now) + timedelta(hours=self.settings.attendance.deadline_hours)
        try:
            if not server:
                raise LookupError("no event server (BOT_SUPERVISOR__EVENT_SERVER)")
            stats = await self.reader.read(server, event.attendance_from, event.attendance_to)
        except (SupervisorError, LookupError) as exc:
            if now >= deadline or isinstance(exc, LookupError):
                log.warning("Attendance of event %s unavailable: %s", event.id, exc)
                await self._store(event.id, [], 0, "unavailable")
                return None
            log.info("Attendance of event %s not read yet: %s", event.id, exc)
            return now + RETRY
        await self.report(event, stats)
        return None

    async def report(self, event: Event, stats: Stats) -> None:
        links = {link.member_id: link.steam_id for link in await self.links.all()}
        lines, guests = compute(await self._holders(event), links, stats.players,
                                self.settings.attendance.min_minutes * 60, event.kind == "hosted", self.names)
        await self._store(event.id, lines, guests, "reported")

    async def _store(self, event_id: int, lines: list[Line], guests: int, state: str) -> None:
        async with self.db.session() as session, session.begin():
            await session.execute(delete(AttendanceEntry).where(AttendanceEntry.event_id == event_id))
            session.add_all(AttendanceEntry(event_id=event_id, **vars(line)) for line in lines)
            row = await session.get(Event, event_id)
            row.attendance_state, row.attendance_guests, row.reported_at = state, guests, self.clock()
        await self.notifier.changed(event_id)
        await self.notifier.attendance_reported(event_id)

    # ---------------------------------------------------------------- guest events

    async def guest_ended(self, event_id: int) -> None:
        """At start + duration + buffer: every slot holder is to confirm."""
        event = await self._event(event_id)
        if event is None or event.status != "published":
            return
        lines = [Line("to_confirm", member_id, holder=True) for member_id in dict.fromkeys(await self._holders(event))]
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event_id)
            row.status, row.attendance_trigger = "ended", "guest"
            row.attendance_from, row.attendance_to = event.assembly_at, self.clock()
        now = self.clock()
        await self.scheduler.schedule(CONFIRM_PING, now + PING_AFTER, key=f"{CONFIRM_PING}:{event_id}", event_id=event_id)
        await self.scheduler.schedule(AUTO_CONFIRM, now + timedelta(days=self.settings.attendance.guest_confirm_days),
                                      key=f"{AUTO_CONFIRM}:{event_id}", event_id=event_id)
        await self._store(event_id, lines, 0, "reported")

    async def _unconfirmed(self, event_id: int | None) -> list[AttendanceEntry]:
        return [e for e in await self.entries(event_id) if e.status == "to_confirm"]

    async def _confirm_ping(self, action: Action, now: datetime) -> None:
        if await self._unconfirmed(action.event_id):
            await self.notifier.attendance_unconfirmed(action.event_id)

    async def _auto_confirm(self, action: Action, now: datetime) -> None:
        waiting = await self._unconfirmed(action.event_id)
        if not waiting:
            return
        async with self.db.session() as session, session.begin():
            for entry in waiting:
                (await session.get(AttendanceEntry, entry.id)).status = "attended"
        await self.notifier.changed(action.event_id)
        await self.notifier.attendance_confirmed(action.event_id, len(waiting))

    # ---------------------------------------------------------------- corrections

    async def _organised(self, event_id: int, member: Member) -> Event:
        event = await self.events.get(event_id)
        if not await self.events.can_organise(event, member):
            raise EventError(Problem.NOT_ALLOWED)
        if event.attendance_state not in ("reported", "unavailable"):
            raise EventError(Problem.NOT_STARTED)
        return event

    async def correct(self, event_id: int, member: Member, member_ids: Iterable[int], status: str) -> int:
        """*Edit attendance*: these members are now ``status``; someone not in the report is added. Returns how
        many lines changed."""
        if status not in CORRECTABLE:
            raise ValueError(status)
        await self._organised(event_id, member)
        now, changed, unattended = self.clock(), 0, []
        async with self.db.session() as session, session.begin():
            for member_id in dict.fromkeys(member_ids):
                entry = (await session.execute(select(AttendanceEntry).where(
                    AttendanceEntry.event_id == event_id, AttendanceEntry.member_id == member_id))).scalars().first()
                if entry is None:
                    entry = AttendanceEntry(event_id=event_id, member_id=member_id, status=status)
                    session.add(entry)
                elif entry.status == status:
                    continue
                elif entry.status in ("attended", "walk_in") and status != "attended":
                    unattended.append(member_id)
                entry.status, entry.corrected_by, entry.corrected_at = status, member.id, now
                changed += 1
        if self.feedback is not None:
            for member_id in unattended:
                await self.feedback.forget(event_id, member_id)
        if changed:
            await self.notifier.changed(event_id)
        return changed

    async def probable(self, event_id: int) -> list[AttendanceEntry]:
        return [e for e in await self.entries(event_id) if e.probable_steam_id and e.member_id is not None]

    async def confirm_probable(self, event_id: int, member: Member, entry_ids: Iterable[int],
                               has_rank: Callable[[int], bool]) -> list[tuple[AttendanceEntry, LinkError | None]]:
        """*Link*: the probable Steam ID becomes the member's link (nick: the in-game name), and their time counts."""
        await self._organised(event_id, member)
        wanted = set(entry_ids)
        done = []
        min_seconds = self.settings.attendance.min_minutes * 60
        for entry in await self.probable(event_id):
            if entry.id not in wanted:
                continue
            try:
                await self.links.link(entry.member_id, entry.probable_steam_id, entry.probable_name or "?", member.id,
                                      has_rank(entry.member_id))
            except LinkError as exc:
                done.append((entry, exc))
                continue
            async with self.db.session() as session, session.begin():
                row = await session.get(AttendanceEntry, entry.id)
                row.steam_id, row.name = row.probable_steam_id, row.probable_name or ""
                row.status = "attended" if row.seconds >= min_seconds else "no_show"
                row.probable_steam_id = row.probable_name = None
                row.corrected_by, row.corrected_at = member.id, self.clock()
            done.append((entry, None))
        await self.notifier.changed(event_id)
        return done
