"""Events (docs/PLAN.md, *Event kinds*, *Creating an event*, *Draft and publish*, *Approval*, *Cancelling*,
*Scheduled actions and downtime*), with no discord.py.

- **Create** a draft: the four kinds, who may create which, the mission type and climate from the config (stored by
  value). Someone who may publish without approval gets the host reservation at once; a member's internal or
  hosted event gets it when sent for approval, a member's Zeus event when it asks for the server.
- **Edit**: the kind only in a draft; times are locked once the reservation's quiet period has begun; a member's
  new times for a published event are a *proposal* for the moderators, and nothing moves until one approves.
- **Publish**: refused until the checklist is complete. The roles of whoever presses decide: a mission maker,
  moderator or *manage any event* publishes, a member sends it for approval (a Zeus event is always published).
- **Approve / reject** a request; approving is refused while its reservation failed.
- **Cancel** before the start (a draft is deleted), **end** after it.
- **Timed actions**: sign-up opening, reminders, stale-draft ping and cancel, a guest event's end. After downtime
  state changes run late; the sign-up ping is dropped when more than an hour late, a reminder when a later one or
  the assembly has passed.

The Discord side (channels, messages, pings) hears about all of it through :class:`EventNotifier`."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sqlalchemy import delete, select, update

from app.db import Database
from app.db.models import ApprovalRequest, Event, EventGuestGroup, EventOrganiser, OtherGroup, Slot
from app.events.times import Times
from app.permissions import Access, Perm
from app.presets import Preset, parse_preset_html
from app.reservations.service import Frozen, ReservationService, Window
from app.resolve import Ids
from app.scheduler import Action, Scheduler
from app.settings import SettingsStore
from app.signup.service import Member

KINDS = ("internal", "guest", "hosted", "zeus")
RESERVED = ("internal", "hosted", "zeus")  # on our server: they get a host reservation
MAX_BRIEFING = 4000
UNSET: object = object()  # update(): a detail not being changed
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
LATE_PING = timedelta(hours=1)
log = logging.getLogger(__name__)


class Problem(StrEnum):
    NOT_FOUND = "not_found"
    NOT_ALLOWED = "not_allowed"
    KIND_NOT_ALLOWED = "kind_not_allowed"  # a member can't make a guest event
    NEEDS_HOST = "needs_host"  # a guest event needs its host group
    UNKNOWN_TYPE = "unknown_type"
    UNKNOWN_CLIMATE = "unknown_climate"
    NOT_DRAFT = "not_draft"
    PENDING = "pending"  # locked while waiting for approval
    INCOMPLETE = "incomplete"  # see EventError.missing
    TIMES_LOCKED = "times_locked"
    STARTED = "started"
    NOT_STARTED = "not_started"
    NOT_PENDING = "not_pending"
    NO_RESERVATION = "no_reservation"
    TOO_LONG = "too_long"
    NOT_ZEUS = "not_zeus"
    NOT_A_NUMBER = "not_a_number"
    NOT_AN_IMAGE = "not_an_image"


class EventError(Exception):
    def __init__(self, problem: Problem, missing: list[str] | None = None) -> None:
        super().__init__(problem.value)
        self.problem = problem
        self.missing = missing or []  # INCOMPLETE: briefing | orbat | how_to_join


@dataclass
class Draft:
    """What the wizard gathered."""

    kind: str
    title: str
    mission_type: str
    times: Times
    climate_role_id: int | None = None
    host_group_id: int | None = None  # guest
    guest_group_ids: list[int] = field(default_factory=list)  # hosted


class EventNotifier(Protocol):
    """What the Discord side does (phase 4b-4d)."""

    async def changed(self, event_id: int) -> None: ...  # redraw the messages; rename the channel if needed
    async def published(self, event_id: int) -> None: ...  # show the channel, post the announcement
    async def deleted(self, event_id: int) -> None: ...  # a draft gone: delete its channel
    async def cancelled(self, event_id: int) -> None: ...  # CANCELLED, ping, archive
    async def signup_opened(self, event_id: int, ping: bool) -> None: ...
    async def reminder(self, event_id: int) -> None: ...
    async def stale(self, event_id: int) -> None: ...  # still a draft: ping the creator, tell staff
    async def approval_requested(self, request_id: int) -> None: ...
    async def approval_decided(self, request_id: int) -> None: ...


class EventService:
    SIGNUP_OPEN, REMINDER, STALE_PING, STALE_CANCEL, GUEST_END = (
        "signup_open", "reminder", "stale_ping", "stale_cancel", "guest_end")

    def __init__(self, db: Database, store: SettingsStore, ids: Callable[[], Ids], scheduler: Scheduler,
                 reservations: ReservationService, notifier: EventNotifier,
                 link: Callable[[Event], str | None] = lambda _e: None,
                 clock: Callable[[], datetime] | None = None) -> None:
        """``link(event)``: the event channel's https address, for the reservation (once phase 4b makes channels)."""
        self.db, self.store, self.ids = db, store, ids
        self.scheduler, self.reservations, self.notifier, self.link = scheduler, reservations, notifier, link
        self.clock = clock or scheduler.clock
        for kind, handler in ((self.SIGNUP_OPEN, self._signup_open), (self.REMINDER, self._reminder),
                              (self.STALE_PING, self._stale_ping), (self.STALE_CANCEL, self._stale_cancel),
                              (self.GUEST_END, self._guest_end)):
            scheduler.register(kind, handler)

    # ---------------------------------------------------------------- reading

    def access(self, member: Member) -> Access:
        return Access(self.ids(), member.role_ids)

    async def get(self, event_id: int) -> Event:
        async with self.db.session() as session:
            event = await session.get(Event, event_id)
        if event is None:
            raise EventError(Problem.NOT_FOUND)
        return event

    async def by_channel(self, channel_id: int | None) -> Event | None:
        """The event whose channel (or discussion thread) this is."""
        if channel_id is None:
            return None
        async with self.db.session() as session:
            return (await session.execute(select(Event).where(
                (Event.channel_id == channel_id) | (Event.thread_id == channel_id)))).scalar_one_or_none()

    async def upcoming(self, days: int) -> list[Event]:
        now = self.clock()
        async with self.db.session() as session:
            return list((await session.execute(select(Event).where(
                Event.status == "published", Event.start_at > now,
                Event.assembly_at <= now + timedelta(days=days)).order_by(Event.assembly_at))).scalars())

    async def organisers(self, event_id: int) -> set[int]:
        async with self.db.session() as session:
            return set((await session.execute(select(EventOrganiser.member_id).where(
                EventOrganiser.event_id == event_id))).scalars())

    async def guest_groups(self, event_id: int) -> list[OtherGroup]:
        async with self.db.session() as session:
            return list((await session.execute(select(OtherGroup).join(
                EventGuestGroup, EventGuestGroup.group_id == OtherGroup.id).where(
                EventGuestGroup.event_id == event_id).order_by(OtherGroup.acronym))).scalars())

    async def can_organise(self, event: Event, member: Member) -> bool:
        """The creator, a co-organiser, or *manage any event*."""
        return (member.id == event.created_by or self.access(member).has(Perm.MANAGE_ANY_EVENT)
                or member.id in await self.organisers(event.id))

    async def _organiser(self, event_id: int, member: Member) -> Event:
        event = await self.get(event_id)
        if not await self.can_organise(event, member):
            raise EventError(Problem.NOT_ALLOWED)
        return event

    async def channel_name(self, event: Event) -> str:
        """``dd-mm-xxx``: the event date and the acronym of the group whose server it is."""
        acronym = self.store.current.group.acronym
        if event.kind == "guest" and event.host_group_id is not None:
            async with self.db.session() as session:
                host = await session.get(OtherGroup, event.host_group_id)
            acronym = host.acronym if host is not None else acronym
        return f"{event.assembly_at.astimezone(self.store.current.tz):%d-%m}-{acronym}".lower()

    async def checklist(self, event: Event) -> list[str]:
        """What publishing still needs."""
        missing = []
        if not (event.briefing_situation or event.briefing_mission or event.briefing_notes).strip():
            missing.append("briefing")
        if event.kind != "zeus":
            async with self.db.session() as session:
                if (await session.execute(select(Slot.id).where(Slot.event_id == event.id).limit(1))).first() is None:
                    missing.append("orbat")
        if event.kind == "guest" and not event.how_to_join.strip():
            missing.append("how_to_join")
        return missing

    # ---------------------------------------------------------------- creating

    async def create(self, draft: Draft, member: Member) -> Event:
        access = self.access(member)
        if not access.may_create_events:
            raise EventError(Problem.NOT_ALLOWED)
        if draft.kind not in KINDS:
            raise EventError(Problem.NOT_FOUND)
        if draft.kind == "guest" and not access.publishes_without_approval:
            raise EventError(Problem.KIND_NOT_ALLOWED)
        if draft.kind == "guest" and draft.host_group_id is None:
            raise EventError(Problem.NEEDS_HOST)
        if draft.mission_type not in self.store.current.events.mission_types:
            raise EventError(Problem.UNKNOWN_TYPE)
        climates = self.ids().climates
        if draft.climate_role_id is not None and draft.climate_role_id not in climates:
            raise EventError(Problem.UNKNOWN_CLIMATE)
        times = draft.times
        if times.assembly_at <= self.clock():
            raise EventError(Problem.STARTED)
        event = Event(kind=draft.kind, title=draft.title.strip()[:100], status="draft", mission_type=draft.mission_type,
                      climate=climates.get(draft.climate_role_id, ""), climate_role_id=draft.climate_role_id,
                      assembly_at=times.assembly_at, start_at=times.start_at, duration_minutes=times.duration_minutes,
                      created_by=member.id, author_id=member.id,
                      host_group_id=draft.host_group_id if draft.kind == "guest" else None)
        async with self.db.session() as session, session.begin():
            session.add(event)
            await session.flush()
            if draft.kind == "hosted":
                session.add_all(EventGuestGroup(event_id=event.id, group_id=g) for g in set(draft.guest_group_ids))
        if draft.kind in RESERVED and access.publishes_without_approval:
            await self._book(event)
        await self._schedule_stale(event)
        return event

    # ---------------------------------------------------------------- editing

    async def update(self, event_id: int, member: Member, *, title: str | None = None, mission_type: str | None = None,
                     climate_role_id: int | None | object = UNSET, kind: str | None = None,
                     host_group_id: int | None | object = UNSET, guest_group_ids: list[int] | None = None,
                     times: Times | None = None) -> ApprovalRequest | None:
        """Change the details (``UNSET``: leave as it is; None for the climate or host clears it). Returns the
        request when new times went to the moderators instead."""
        event = await self._organiser(event_id, member)
        if event.status == "pending":
            raise EventError(Problem.PENDING)
        if event.status not in ("draft", "published"):
            raise EventError(Problem.NOT_DRAFT)
        if kind is not None and kind != event.kind:
            if event.status != "draft":
                raise EventError(Problem.NOT_DRAFT)
            if kind not in KINDS or (kind == "guest" and not self.access(member).publishes_without_approval):
                raise EventError(Problem.KIND_NOT_ALLOWED)
        if mission_type is not None and mission_type not in self.store.current.events.mission_types:
            raise EventError(Problem.UNKNOWN_TYPE)
        climates = self.ids().climates
        if climate_role_id is not UNSET and climate_role_id is not None and climate_role_id not in climates:
            raise EventError(Problem.UNKNOWN_CLIMATE)
        new_times = times is not None and (times.assembly_at, times.start_at, times.duration_minutes) != \
            (event.assembly_at, event.start_at, event.duration_minutes)
        if new_times:
            await self._check_times(event, times)
            if event.status == "published" and not self.access(member).publishes_without_approval and \
                    (event.kind != "zeus" or await self.reservations.get(event.id) is not None):
                return await self._request(event, "times", member, proposed={
                    "assembly_at": times.assembly_at.isoformat(), "start_at": times.start_at.isoformat(),
                    "duration_minutes": times.duration_minutes})
        old_kind = event.kind
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event_id)
            if title is not None:
                row.title = title.strip()[:100]
            if mission_type is not None:
                row.mission_type = mission_type
            if climate_role_id is not UNSET:
                row.climate_role_id, row.climate = climate_role_id, climates.get(climate_role_id, "")
            if kind is not None:
                row.kind = kind
            if host_group_id is not UNSET:
                row.host_group_id = host_group_id
            if row.kind == "guest" and row.host_group_id is None:
                raise EventError(Problem.NEEDS_HOST)
            if guest_group_ids is not None:
                await session.execute(delete(EventGuestGroup).where(EventGuestGroup.event_id == event_id))
                session.add_all(EventGuestGroup(event_id=event_id, group_id=g) for g in set(guest_group_ids))
            if new_times:
                row.assembly_at, row.start_at, row.duration_minutes = times.assembly_at, times.start_at, times.duration_minutes
            event = row
        if kind is not None and kind != old_kind:
            await self._kind_changed(event, member)
        elif new_times or title is not None:
            await self._rebook(event)
        if new_times:
            await self._reschedule(event)
        await self.notifier.changed(event_id)
        return None

    async def _check_times(self, event: Event, times: Times) -> None:
        now = self.clock()
        if now >= event.start_at:
            raise EventError(Problem.STARTED)
        if times.assembly_at <= now:
            raise EventError(Problem.STARTED)
        booked = await self.reservations.get(event.id)
        if booked is not None and booked.state == "booked" and self.reservations.frozen(booked, now):
            raise EventError(Problem.TIMES_LOCKED)

    async def _kind_changed(self, event: Event, member: Member) -> None:
        """A draft's new kind: as if it had been made with it. Someone who may publish keeps (or gets) the
        reservation of an event on our server; otherwise there is none until the request (or none at all)."""
        if event.kind in RESERVED and self.access(member).publishes_without_approval:
            await self._book(event)
        else:
            await self.reservations.cancel(event.id)

    async def set_briefing(self, event_id: int, member: Member, situation: str, mission: str, notes: str) -> None:
        if len(situation) + len(mission) + len(notes) > MAX_BRIEFING:
            raise EventError(Problem.TOO_LONG)
        await self._set(event_id, member, briefing_situation=situation.strip(), briefing_mission=mission.strip(),
                        briefing_notes=notes.strip())

    async def set_preset(self, event_id: int, member: Member, content: bytes) -> Preset:
        """`/event preset`: parse an Arma 3 Launcher preset, keep the file, and take its Creator DLC as the DLC the
        event needs. Raises ValueError for a file with no mods and no DLC."""
        await self._organiser(event_id, member)
        preset = parse_preset_html(content.decode("utf-8", errors="replace"))
        path = self._file("presets", f"{event_id}.html")
        _write(path, content)
        await self._set(event_id, member, preset_name=preset.name, required_dlc=list(preset.cdlc),
                        preset_mods=[[m.workshop_id, m.name] for m in preset.mods], preset_file=str(path))
        return preset

    async def set_image(self, event_id: int, member: Member, content: bytes, suffix: str) -> None:
        """`/event image`: the briefing's banner or map, kept by the bot (so it outlives the organiser's message)."""
        event = await self._organiser(event_id, member)
        if suffix.lower() not in IMAGE_SUFFIXES:
            raise EventError(Problem.NOT_AN_IMAGE)
        path = self._file("images", f"{event_id}{suffix.lower()}")
        _write(path, content)
        if event.briefing_image and event.briefing_image != str(path):
            Path(event.briefing_image).unlink(missing_ok=True)
        await self._set(event_id, member, briefing_image=str(path))

    def _file(self, folder: str, name: str) -> Path:
        path = self.store.current.data_dir / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    async def set_how_to_join(self, event_id: int, member: Member, text: str) -> None:
        if len(text) > 1024:  # one embed field
            raise EventError(Problem.TOO_LONG)
        await self._set(event_id, member, how_to_join=text.strip())

    async def set_signup_start(self, event_id: int, member: Member, opens_at: datetime | None) -> None:
        event = await self._set(event_id, member, signup_opens_at=opens_at)
        if event.status == "published":
            await self._schedule_signup_open(event)

    async def set_limit(self, event_id: int, member: Member, limit: int | None) -> None:
        event = await self._organiser(event_id, member)
        if event.kind != "zeus":
            raise EventError(Problem.NOT_ZEUS)
        await self._set(event_id, member, signup_limit=limit if limit and limit > 0 else None)

    async def set_dont_end_automatically(self, event_id: int, member: Member, value: bool) -> None:
        await self._set(event_id, member, dont_end_automatically=value)

    async def add_organiser(self, event_id: int, member: Member, organiser_id: int) -> None:
        await self._organiser(event_id, member)
        async with self.db.session() as session, session.begin():
            if await session.get(EventOrganiser, (event_id, organiser_id)) is None:
                session.add(EventOrganiser(event_id=event_id, member_id=organiser_id))
        await self.notifier.changed(event_id)

    async def remove_organiser(self, event_id: int, member: Member, organiser_id: int) -> None:
        await self._organiser(event_id, member)
        async with self.db.session() as session, session.begin():
            await session.execute(delete(EventOrganiser).where(EventOrganiser.event_id == event_id,
                                                               EventOrganiser.member_id == organiser_id))
        await self.notifier.changed(event_id)

    async def _set(self, event_id: int, member: Member, **values) -> Event:
        event = await self._organiser(event_id, member)
        if event.status == "pending":
            raise EventError(Problem.PENDING)
        if event.status not in ("draft", "published"):
            raise EventError(Problem.NOT_DRAFT)
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event_id)
            for name, value in values.items():
                setattr(row, name, value)
        await self.notifier.changed(event_id)
        return row

    # ---------------------------------------------------------------- publishing and approval

    async def publish(self, event_id: int, member: Member) -> str:
        """*Publish* (or *Send for approval*): returns the new status, ``published`` or ``pending``."""
        event = await self._organiser(event_id, member)
        if event.status != "draft":
            raise EventError(Problem.NOT_DRAFT)
        if self.clock() >= event.start_at:
            raise EventError(Problem.STARTED)
        missing = await self.checklist(event)
        if missing:
            raise EventError(Problem.INCOMPLETE, missing)
        access = self.access(member)
        if access.publishes_without_approval or event.kind == "zeus":
            if event.kind in RESERVED and access.publishes_without_approval and await self.reservations.get(event.id) is None:
                await self._book(event)
            await self._publish(event)
            return "published"
        await self._book(event)  # a member's internal or hosted event: the clash shows in the request
        async with self.db.session() as session, session.begin():
            (await session.get(Event, event_id)).status = "pending"
        await self._request(event, "publish", member)
        return "pending"

    async def request_server(self, event_id: int, member: Member) -> ApprovalRequest:
        """A member's Zeus event asks for the host: the reservation is made now (so a clash shows) and waits for
        a moderator."""
        event = await self._organiser(event_id, member)
        if event.kind != "zeus":
            raise EventError(Problem.NOT_ZEUS)
        if event.status not in ("draft", "published"):
            raise EventError(Problem.NOT_DRAFT)
        async with self.db.session() as session:
            waiting = (await session.execute(select(ApprovalRequest).where(
                ApprovalRequest.event_id == event_id, ApprovalRequest.kind == "server",
                ApprovalRequest.state == "pending"))).scalar_one_or_none()
        if waiting is not None:
            return waiting
        await self._book(event)
        return await self._request(event, "server", member)

    async def withdraw(self, event_id: int, member: Member) -> None:
        """*Withdraw request*: a pending event goes back to draft, without its reservation."""
        event = await self._organiser(event_id, member)
        if event.status != "pending":
            raise EventError(Problem.NOT_PENDING)
        async with self.db.session() as session, session.begin():
            (await session.get(Event, event_id)).status = "draft"
            await session.execute(update(ApprovalRequest).where(
                ApprovalRequest.event_id == event_id, ApprovalRequest.state == "pending",
                ApprovalRequest.kind == "publish").values(state="withdrawn"))
        await self.reservations.cancel(event_id)
        await self.notifier.changed(event_id)

    async def _request(self, event: Event, kind: str, member: Member, proposed: dict | None = None) -> ApprovalRequest:
        async with self.db.session() as session, session.begin():
            await session.execute(update(ApprovalRequest).where(  # one open request of a kind at a time
                ApprovalRequest.event_id == event.id, ApprovalRequest.kind == kind,
                ApprovalRequest.state == "pending").values(state="withdrawn"))
            request = ApprovalRequest(event_id=event.id, kind=kind, requested_by=member.id, proposed=proposed or {})
            session.add(request)
        await self.notifier.approval_requested(request.id)
        return request

    async def approval_blocked(self, request: ApprovalRequest) -> bool:
        """*Approve* is off while the request's reservation failed (publishing and server requests)."""
        if request.kind == "times":
            return False
        booked = await self.reservations.get(request.event_id)
        event = await self.get(request.event_id)
        return event.kind in RESERVED and (booked is None or booked.state != "booked")

    async def approve(self, request_id: int, moderator: Member) -> ApprovalRequest:
        request = await self._pending(request_id, moderator)
        event = await self.get(request.event_id)
        if await self.approval_blocked(request):
            raise EventError(Problem.NO_RESERVATION)
        if request.kind == "times":
            p = request.proposed
            times = Times(datetime.fromisoformat(p["assembly_at"]), datetime.fromisoformat(p["start_at"]),
                          int(p["duration_minutes"]))
            await self._check_times(event, times)
            await self._move(event, times)
        await self._decide(request_id, moderator, "approved")
        if request.kind == "publish":
            await self._publish(await self.get(request.event_id))
        await self.notifier.approval_decided(request_id)
        return await self._request_row(request_id)

    async def reject(self, request_id: int, moderator: Member, reason: str) -> ApprovalRequest:
        request = await self._pending(request_id, moderator)
        await self._decide(request_id, moderator, "rejected", reason)
        if request.kind == "publish":
            async with self.db.session() as session, session.begin():
                (await session.get(Event, request.event_id)).status = "draft"
            await self.reservations.cancel(request.event_id)
            await self.notifier.changed(request.event_id)
        elif request.kind == "server":
            await self.reservations.cancel(request.event_id)
        await self.notifier.approval_decided(request_id)
        return await self._request_row(request_id)

    async def _pending(self, request_id: int, moderator: Member) -> ApprovalRequest:
        if not self.access(moderator).has(Perm.APPROVE_EVENTS):
            raise EventError(Problem.NOT_ALLOWED)
        request = await self._request_row(request_id)
        if request.state != "pending":
            raise EventError(Problem.NOT_PENDING)
        return request

    async def _request_row(self, request_id: int) -> ApprovalRequest:
        async with self.db.session() as session:
            request = await session.get(ApprovalRequest, request_id)
        if request is None:
            raise EventError(Problem.NOT_FOUND)
        return request

    async def _decide(self, request_id: int, moderator: Member, state: str, reason: str | None = None) -> None:
        async with self.db.session() as session, session.begin():
            request = await session.get(ApprovalRequest, request_id)
            request.state, request.decided_by, request.reason, request.decided_at = state, moderator.id, reason, self.clock()

    async def _publish(self, event: Event) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event.id)
            row.status = "published"
        for kind in (self.STALE_PING, self.STALE_CANCEL):
            await self.scheduler.cancel(f"{kind}:{event.id}")
        await self._schedule_signup_open(row)
        await self._schedule_reminders(row)
        if row.kind == "guest":
            await self.scheduler.schedule(self.GUEST_END, row.start_at + timedelta(
                minutes=row.duration_minutes + self.store.current.times.buffer_minutes),
                key=f"{self.GUEST_END}:{row.id}", event_id=row.id)
        await self.notifier.published(event.id)

    # ---------------------------------------------------------------- reservations

    async def _window(self, event: Event) -> Window:
        times = self.store.current.times
        return Window(f"{await self.channel_name(event)} {event.title}",
                      event.assembly_at - timedelta(minutes=times.lead_minutes),
                      event.start_at + timedelta(minutes=event.duration_minutes + times.buffer_minutes),
                      link=self.link(event))

    async def _book(self, event: Event) -> None:
        try:
            await self.reservations.book(event.id, await self._window(event))
        except Frozen as exc:
            raise EventError(Problem.TIMES_LOCKED) from exc

    async def refresh_reservation(self, event_id: int) -> None:
        """Send the reservation again (its link, once the channel exists)."""
        await self._rebook(await self.get(event_id))

    async def _rebook(self, event: Event) -> None:
        """Keep an existing reservation in step (name, times); events without one stay without."""
        booked = await self.reservations.get(event.id)
        if booked is not None and booked.state not in ("cancelled", "cancelling", "ended"):
            await self._book(event)

    async def _move(self, event: Event, times: Times) -> None:
        """Approved new times: move the reservation first; if the supervisor refuses, nothing changes."""
        moved = Event(**{c: getattr(event, c) for c in ("id", "kind", "title", "host_group_id")},
                      assembly_at=times.assembly_at, start_at=times.start_at, duration_minutes=times.duration_minutes)
        booked = await self.reservations.get(event.id)
        if booked is not None and booked.state == "booked":
            row = await self.reservations.book(event.id, await self._window(moved))
            if row.state != "booked":
                await self.reservations.book(event.id, await self._window(event))  # back to the old times
                raise EventError(Problem.NO_RESERVATION)
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event.id)
            row.assembly_at, row.start_at, row.duration_minutes = times.assembly_at, times.start_at, times.duration_minutes
        await self._reschedule(row)
        await self.notifier.changed(event.id)

    # ---------------------------------------------------------------- cancelling and ending

    async def cancel(self, event_id: int, member: Member | None, reason: str) -> None:
        """Before the start. A draft (or pending event) is deleted; a published one becomes CANCELLED.
        ``member`` None: the bot itself (a stale draft)."""
        event = await self.get(event_id) if member is None else await self._organiser(event_id, member)
        if event.status in ("cancelled", "ended"):
            raise EventError(Problem.NOT_DRAFT)
        if self.clock() >= event.start_at:
            raise EventError(Problem.STARTED)
        was = event.status
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event_id)
            row.status, row.cancel_reason = "cancelled", reason
            await session.execute(update(ApprovalRequest).where(
                ApprovalRequest.event_id == event_id, ApprovalRequest.state == "pending").values(state="withdrawn"))
        await self.reservations.cancel(event_id)
        for kind in (self.SIGNUP_OPEN, self.STALE_PING, self.STALE_CANCEL, self.GUEST_END):
            await self.scheduler.cancel(f"{kind}:{event_id}")
        for minutes in self.store.current.times.reminder_minutes:
            await self.scheduler.cancel(f"{self.REMINDER}:{event_id}:{minutes}")
        if was in ("draft", "pending"):
            await self.notifier.deleted(event_id)
        else:
            await self.notifier.cancelled(event_id)

    async def end(self, event_id: int, member: Member) -> None:
        """*End event*, after the start: the reservation ends, and the event is over (attendance: phase 6)."""
        event = await self._organiser(event_id, member)
        if event.status != "published":
            raise EventError(Problem.NOT_DRAFT)
        if self.clock() < event.start_at:
            raise EventError(Problem.NOT_STARTED)
        await self.reservations.end(event_id)
        async with self.db.session() as session, session.begin():
            (await session.get(Event, event_id)).status = "ended"
        await self.notifier.changed(event_id)

    # ---------------------------------------------------------------- timed actions

    async def _schedule_stale(self, event: Event) -> None:
        """Only what is still ahead: a draft made inside the window is neither pinged nor cancelled at once."""
        times, now = self.store.current.times, self.clock()
        for kind, hours in ((self.STALE_PING, times.stale_ping_hours), (self.STALE_CANCEL, times.stale_cancel_hours)):
            due = event.assembly_at - timedelta(hours=hours)
            if due > now:
                await self.scheduler.schedule(kind, due, key=f"{kind}:{event.id}", event_id=event.id)
            else:
                await self.scheduler.cancel(f"{kind}:{event.id}")

    async def _schedule_signup_open(self, event: Event) -> None:
        """At publishing, sign-up opens with it (the announcement says so); a later start gets its own action."""
        if event.signup_opens_at is not None and event.signup_opens_at > self.clock():
            await self.scheduler.schedule(self.SIGNUP_OPEN, event.signup_opens_at,
                                          key=f"{self.SIGNUP_OPEN}:{event.id}", event_id=event.id)
        else:
            await self.scheduler.cancel(f"{self.SIGNUP_OPEN}:{event.id}")

    async def _schedule_reminders(self, event: Event) -> None:
        now = self.clock()
        for minutes in self.store.current.times.reminder_minutes:
            due = event.assembly_at - timedelta(minutes=minutes)
            key = f"{self.REMINDER}:{event.id}:{minutes}"
            if due > now:
                await self.scheduler.schedule(self.REMINDER, due, key=key, event_id=event.id,
                                              payload={"minutes": minutes})
            else:
                await self.scheduler.cancel(key)

    async def _reschedule(self, event: Event) -> None:
        if event.status in ("draft", "pending"):
            await self._schedule_stale(event)
        elif event.status == "published":
            await self._schedule_signup_open(event)
            await self._schedule_reminders(event)

    async def _live(self, action: Action, *statuses: str) -> Event | None:
        async with self.db.session() as session:
            event = await session.get(Event, action.event_id)
        return event if event is not None and event.status in statuses else None

    async def _signup_open(self, action: Action, now: datetime) -> None:
        if (event := await self._live(action, "published")) is not None and now < event.start_at:
            await self.notifier.signup_opened(event.id, ping=action.late(now) <= LATE_PING)

    async def _reminder(self, action: Action, now: datetime) -> None:
        event = await self._live(action, "published")
        if event is None or now >= event.assembly_at:
            return
        later = [m for m in self.store.current.times.reminder_minutes if m < action.payload["minutes"]]
        if any(now >= event.assembly_at - timedelta(minutes=m) for m in later):
            return  # a later reminder is due already: that one speaks
        await self.notifier.reminder(event.id)

    async def _stale_ping(self, action: Action, now: datetime) -> None:
        if (event := await self._live(action, "draft", "pending")) is not None:
            await self.notifier.stale(event.id)

    async def _stale_cancel(self, action: Action, now: datetime) -> None:
        if (event := await self._live(action, "draft", "pending")) is not None and now < event.start_at:
            await self.cancel(event.id, None, "stale")

    async def _guest_end(self, action: Action, now: datetime) -> None:
        if (event := await self._live(action, "published")) is not None:
            async with self.db.session() as session, session.begin():
                (await session.get(Event, event.id)).status = "ended"
            await self.notifier.changed(event.id)


def _write(path: Path, content: bytes) -> None:
    """All or nothing: a half-written file never replaces a good one."""
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(content)
    os.replace(temporary, path)
