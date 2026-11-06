"""Sign-up (docs/PLAN.md, *ORBAT and slots*, *Zeus sign-up*): slots, Maybe / Can't come, the reserve list and its
offers, Zeus's Coming, organisers' locks and assignments, an ORBAT edited after sign-ups, and members whose roles
change. No discord.py: the Discord layer passes :class:`Member` (id and role ids), words the :class:`Refusal`, and
learns what to send through :class:`Notifier`.

The rules that matter most:

- **A claim is one conditional update** ("take it if still free"), and one slot per member per event is a unique
  index, so a rush when sign-up opens can't hand a slot to two members. Zeus's limit is one statement too.
- Sign-up opens at ``signup_opens_at`` (or at publishing) and closes at the start; Maybe and Can't come work before
  it opens. A member may not hold a place in two events whose windows (assembly -> start + duration) overlap.
- A freed slot is offered to the first member on the reserve list who meets its role rule (each gets one chance per
  freed slot), for ``offer_timeout`` but never past the start; within ``offer_to_all`` of the assembly it is offered
  to the whole list at once, first to confirm takes it. While an offer is open, nobody else takes that slot."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from sqlalchemy import delete, func, insert, literal, select, update

from app.db import Database
from app.db.models import Event, Offer, Signup, Slot, Squad, UtcDateTime
from app.orbat.parser import ParsedSquad
from app.resolve import Ids
from app.scheduler import Action, Scheduler
from app.settings import SettingsStore

OFFER_KIND = "offer_expiry"
COMING = ("zeus", "training")  # event kinds without an ORBAT: Coming / Not coming, a limit, no approval to publish
ACTIVE = ("draft", "pending", "published")  # events whose sign-ups still change
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Member:
    id: int
    role_ids: frozenset[int] = frozenset()


class Refusal(StrEnum):
    NOT_PUBLISHED = "not_published"
    NOT_OPEN = "not_open"  # sign-up opens later
    CLOSED = "closed"  # the event has started
    NOT_ALLOWED = "not_allowed"  # no sign-up role
    WRONG_KIND = "wrong_kind"  # a slot in a Zeus event, or Coming in an ORBAT event
    NO_SLOT = "no_slot"
    TAKEN = "taken"
    OFFERED = "offered"  # the slot is offered to someone on the reserve list
    LOCKED = "locked"
    ROLE = "role"  # the slot needs a role the member lacks
    OVERLAP = "overlap"
    FULL = "full"  # Zeus: the limit is reached
    NOT_FULL = "not_full"  # reserve while a slot is free
    ALREADY = "already"
    NOT_SIGNED_UP = "not_signed_up"
    OFFER_GONE = "offer_gone"


@dataclass
class Outcome:
    refusal: Refusal | None = None
    other_event: int | None = None  # OVERLAP: the event the member is in already
    missing_dlc: list[str] = field(default_factory=list)  # DLC keys the preset needs and the member has no role for

    @property
    def ok(self) -> bool:
        return self.refusal is None


@dataclass
class Board:
    """Everything the ORBAT or sign-up list message shows."""

    squads: list[tuple[Squad, list[Slot]]] = field(default_factory=list)
    signups: list[Signup] = field(default_factory=list)  # reserve in queue order, the others by time
    offered: set[int] = field(default_factory=set)  # slots held for an open offer

    def status(self, status: str) -> list[int]:
        return [s.member_id for s in self.signups if s.status == status]

    @property
    def slots(self) -> list[Slot]:
        return [slot for _, slots in self.squads for slot in slots]


class Notifier(Protocol):
    """What the Discord layer does when sign-up changes."""

    async def changed(self, event_id: int) -> None: ...  # redraw the ORBAT / sign-up list
    async def offered(self, offer: Offer, member_ids: list[int]) -> None: ...  # DM them (or ping in the thread)
    async def lost_slot(self, event_id: int, member_ids: list[int]) -> None: ...  # now at the top of the reserve list
    async def released(self, member_id: int, event_ids: list[int], left: bool) -> None: ...  # tell staff


class _Refused(Exception):
    def __init__(self, refusal: Refusal, other_event: int | None = None) -> None:
        super().__init__(refusal.value)
        self.outcome = Outcome(refusal, other_event)


def _window(event: Event) -> tuple[datetime, datetime]:
    return event.assembly_at, event.start_at + timedelta(minutes=event.duration_minutes)


def _free(slot: Slot) -> int:
    slot.holder_id, slot.locked, slot.flagged = None, False, False
    return slot.id


class SignupService:
    def __init__(self, db: Database, store: SettingsStore, ids: Callable[[], Ids], scheduler: Scheduler,
                 notifier: Notifier, roles_of: Callable[[int], frozenset[int] | None],
                 clock: Callable[[], datetime] | None = None) -> None:
        """``roles_of(member_id)``: the member's role ids now, or None when they are no longer on the server."""
        self.db = db
        self.store = store
        self.ids = ids
        self.scheduler = scheduler
        self.notifier = notifier
        self.roles_of = roles_of
        self.clock = clock or scheduler.clock
        scheduler.register(OFFER_KIND, self._offer_expired)

    # ---------------------------------------------------------------- reading

    async def board(self, event_id: int) -> Board:
        async with self.db.session() as session:
            squads = (await session.execute(select(Squad).where(Squad.event_id == event_id)
                                            .order_by(Squad.position))).scalars().all()
            slots = (await session.execute(select(Slot).where(Slot.event_id == event_id)
                                           .order_by(Slot.position))).scalars().all()
            signups = (await session.execute(select(Signup).where(Signup.event_id == event_id).order_by(
                Signup.reserve_position.is_(None), Signup.reserve_position, Signup.updated_at, Signup.id))).scalars().all()
            offered = set((await session.execute(select(Offer.slot_id).where(Offer.event_id == event_id,
                                                                             Offer.state == "open"))).scalars())
        by_squad: dict[int, list[Slot]] = {}
        for slot in slots:
            by_squad.setdefault(slot.squad_id, []).append(slot)
        return Board([(squad, by_squad.get(squad.id, [])) for squad in squads], list(signups), offered)

    # ---------------------------------------------------------------- rules

    def _check_signup(self, event: Event, member: Member, now: datetime, *, answer: bool = False) -> None:
        """May ``member`` sign up now? ``answer``: Maybe / Can't come, which work before sign-up opens."""
        if event.status != "published":
            raise _Refused(Refusal.NOT_PUBLISHED)
        if now >= event.start_at:
            raise _Refused(Refusal.CLOSED)
        if member.role_ids.isdisjoint(self.ids().signup):
            raise _Refused(Refusal.NOT_ALLOWED)
        if not answer and event.signup_opens_at is not None and now < event.signup_opens_at:
            raise _Refused(Refusal.NOT_OPEN)

    async def _check_overlap(self, session, event: Event, member_id: int) -> None:
        start, end = _window(event)
        held = select(Slot.event_id).where(Slot.holder_id == member_id)
        coming = select(Signup.event_id).where(Signup.member_id == member_id, Signup.status == "coming")
        others = (await session.execute(select(Event).where(
            Event.id != event.id, Event.status.in_(ACTIVE), Event.id.in_(held.union(coming))))).scalars()
        for other in others:
            other_start, other_end = _window(other)
            if other_start < end and start < other_end:
                raise _Refused(Refusal.OVERLAP, other.id)

    def missing_dlc(self, event: Event, role_ids: Iterable[int]) -> list[str]:
        """The preset's Creator DLC whose role the member lacks (DLC without a configured role are not judged)."""
        roles, dlc_roles = frozenset(role_ids), self.ids().dlc
        return [key for key in event.required_dlc or () if key in dlc_roles and dlc_roles[key] not in roles]

    @staticmethod
    async def _event(session, event_id: int) -> Event:
        event = await session.get(Event, event_id)
        if event is None:
            raise _Refused(Refusal.NO_SLOT)
        return event

    @staticmethod
    async def _held(session, event_id: int, member_id: int) -> Slot | None:
        return (await session.execute(select(Slot).where(Slot.event_id == event_id,
                                                         Slot.holder_id == member_id))).scalar_one_or_none()

    @staticmethod
    async def _open_offer(session, slot_id: int) -> Offer | None:
        return (await session.execute(select(Offer).where(Offer.slot_id == slot_id,
                                                          Offer.state == "open"))).scalar_one_or_none()

    # ---------------------------------------------------------------- slots

    async def take(self, event_id: int, slot_id: int, member: Member) -> Outcome:
        """Take a slot, moving from the member's other slot in the event. A slot under offer goes only to the
        member offered it (or, offered to all, to anyone on the reserve list)."""
        now = self.clock()
        freed: int | None = None
        try:
            async with self.db.session() as session, session.begin():
                event = await self._event(session, event_id)
                if event.kind in COMING:
                    raise _Refused(Refusal.WRONG_KIND)
                self._check_signup(event, member, now)
                slot = await session.get(Slot, slot_id)
                if slot is None or slot.event_id != event_id:
                    raise _Refused(Refusal.NO_SLOT)
                if slot.holder_id == member.id:
                    raise _Refused(Refusal.ALREADY)
                if slot.locked:
                    raise _Refused(Refusal.LOCKED)
                if slot.required_role_id is not None and slot.required_role_id not in member.role_ids:
                    raise _Refused(Refusal.ROLE)
                offer = await self._open_offer(session, slot_id)
                if offer is not None:
                    entry = await self._signup(session, event_id, member.id)
                    on_reserve = entry is not None and entry.status == "reserve"
                    if offer.member_id != member.id and not (offer.member_id is None and on_reserve):
                        raise _Refused(Refusal.OFFERED)
                await self._check_overlap(session, event, member.id)
                own = await self._held(session, event_id, member.id)
                if own is not None:
                    if own.locked:
                        raise _Refused(Refusal.LOCKED)
                    freed = _free(own)
                    await session.flush()
                claimed = await session.execute(update(Slot).where(Slot.id == slot_id, Slot.holder_id.is_(None),
                                                                   Slot.locked.is_(False)).values(holder_id=member.id))
                if claimed.rowcount != 1:
                    raise _Refused(Refusal.TAKEN)  # someone was faster: everything here rolls back
                if offer is not None:
                    offer.state = "taken"
                    offer_id = offer.id
                await session.execute(delete(Signup).where(Signup.event_id == event_id, Signup.member_id == member.id))
                missing = self.missing_dlc(event, member.role_ids)
        except _Refused as refused:
            return refused.outcome
        if offer is not None:
            await self.scheduler.cancel(f"{OFFER_KIND}:{offer_id}")
        if freed is not None:
            await self._offer(freed)
        await self.notifier.changed(event_id)
        return Outcome(missing_dlc=missing)

    async def leave(self, event_id: int, member: Member) -> Outcome:
        """*Leave*: the slot, or the event's lists (Maybe, Can't come, reserve, Coming)."""
        freed = None
        try:
            async with self.db.session() as session, session.begin():
                event = await self._event(session, event_id)
                if self.clock() >= event.start_at:
                    raise _Refused(Refusal.CLOSED)
                own = await self._held(session, event_id, member.id)
                if own is not None:
                    if own.locked:
                        raise _Refused(Refusal.LOCKED)
                    freed = _free(own)
                gone = await session.execute(delete(Signup).where(Signup.event_id == event_id,
                                                                  Signup.member_id == member.id))
                if own is None and gone.rowcount == 0:
                    raise _Refused(Refusal.NOT_SIGNED_UP)
        except _Refused as refused:
            return refused.outcome
        if freed is not None:
            await self._offer(freed)
        await self.notifier.changed(event_id)
        return Outcome()

    async def answer(self, event_id: int, member: Member, status: str) -> Outcome:
        """*Maybe* or *Can't come* (a Zeus event's *Not coming* is ``cant``); the member's slot is freed."""
        assert status in ("maybe", "cant")
        freed = None
        try:
            async with self.db.session() as session, session.begin():
                event = await self._event(session, event_id)
                if status == "maybe" and event.kind in COMING:
                    raise _Refused(Refusal.WRONG_KIND)
                self._check_signup(event, member, self.clock(), answer=True)
                entry = await self._signup(session, event_id, member.id)
                if entry is not None and entry.status == status:
                    raise _Refused(Refusal.ALREADY)
                own = await self._held(session, event_id, member.id)
                if own is not None:
                    if own.locked:
                        raise _Refused(Refusal.LOCKED)
                    freed = _free(own)
                await self._set(session, event_id, member.id, status)
        except _Refused as refused:
            return refused.outcome
        if freed is not None:
            await self._offer(freed)
        await self.notifier.changed(event_id)
        return Outcome()

    async def reserve(self, event_id: int, member: Member) -> Outcome:
        """*Reserve*: the end of the queue, only when no slot is free for this member."""
        try:
            async with self.db.session() as session, session.begin():
                event = await self._event(session, event_id)
                if event.kind in COMING:
                    raise _Refused(Refusal.WRONG_KIND)
                self._check_signup(event, member, self.clock())
                entry = await self._signup(session, event_id, member.id)
                if await self._held(session, event_id, member.id) or (entry is not None and entry.status == "reserve"):
                    raise _Refused(Refusal.ALREADY)
                if await self._free_slot_for(session, event_id, member.role_ids):
                    raise _Refused(Refusal.NOT_FULL)
                await self._check_overlap(session, event, member.id)
                last = (await session.execute(select(func.max(Signup.reserve_position)).where(
                    Signup.event_id == event_id, Signup.status == "reserve"))).scalar()
                await self._set(session, event_id, member.id, "reserve", (last or 0) + 1)
        except _Refused as refused:
            return refused.outcome
        await self.notifier.changed(event_id)
        return Outcome()

    @staticmethod
    async def _free_slot_for(session, event_id: int, role_ids: frozenset[int]) -> bool:
        free = (await session.execute(select(Slot).where(Slot.event_id == event_id, Slot.holder_id.is_(None),
                                                         Slot.locked.is_(False)))).scalars()
        offered = set((await session.execute(select(Offer.slot_id).where(Offer.event_id == event_id,
                                                                         Offer.state == "open"))).scalars())
        return any(s.id not in offered and (s.required_role_id is None or s.required_role_id in role_ids) for s in free)

    # ---------------------------------------------------------------- Zeus

    async def coming(self, event_id: int, member: Member) -> Outcome:
        """*Coming* in a Zeus event, checked against the limit in the same statement that records it."""
        now = self.clock()
        try:
            async with self.db.session() as session, session.begin():
                event = await self._event(session, event_id)
                if event.kind not in COMING:
                    raise _Refused(Refusal.WRONG_KIND)
                self._check_signup(event, member, now)
                entry = await self._signup(session, event_id, member.id)
                if entry is not None and entry.status == "coming":
                    raise _Refused(Refusal.ALREADY)
                await self._check_overlap(session, event, member.id)
                room = literal(True)
                if event.signup_limit is not None:
                    count = select(func.count()).select_from(Signup).where(
                        Signup.event_id == event_id, Signup.status == "coming").scalar_subquery()
                    room = count < event.signup_limit
                if entry is None:
                    done = await session.execute(insert(Signup).from_select(
                        ["event_id", "member_id", "status", "updated_at"],
                        select(literal(event_id), literal(member.id), literal("coming"),
                               literal(now, UtcDateTime())).where(room)))
                else:
                    done = await session.execute(update(Signup).where(Signup.id == entry.id, room)
                                                 .values(status="coming", reserve_position=None, updated_at=now))
                if done.rowcount != 1:
                    raise _Refused(Refusal.FULL)
                missing = self.missing_dlc(event, member.role_ids)
        except _Refused as refused:
            return refused.outcome
        await self.notifier.changed(event_id)
        return Outcome(missing_dlc=missing)

    # ---------------------------------------------------------------- offers

    async def _offer(self, slot_id: int) -> None:
        """Offer a freed slot to the reserve list, if the event still takes offers."""
        now, times = self.clock(), self.store.current.times
        async with self.db.session() as session, session.begin():
            slot = await session.get(Slot, slot_id)
            if slot is None or slot.holder_id is not None or slot.locked or await self._open_offer(session, slot_id):
                return
            event = await session.get(Event, slot.event_id)
            if event is None or event.status != "published" or now >= event.start_at:
                return
            asked = set((await session.execute(select(Offer.member_id).where(Offer.slot_id == slot_id))).scalars())
            queue = (await session.execute(select(Signup).where(Signup.event_id == event.id, Signup.status == "reserve")
                                           .order_by(Signup.reserve_position, Signup.id))).scalars()
            eligible = []
            for entry in queue:
                roles = self.roles_of(entry.member_id)
                if roles is not None and entry.member_id not in asked and \
                        (slot.required_role_id is None or slot.required_role_id in roles):
                    eligible.append(entry.member_id)
            if not eligible:
                return
            to_all = now >= event.assembly_at - timedelta(minutes=times.offer_to_all_minutes)
            expires = event.start_at if to_all else min(now + timedelta(minutes=times.offer_timeout_minutes),
                                                        event.start_at)
            offer = Offer(event_id=event.id, slot_id=slot_id, member_id=None if to_all else eligible[0],
                          expires_at=expires)
            session.add(offer)
            await session.flush()
        await self.scheduler.schedule(OFFER_KIND, expires, key=f"{OFFER_KIND}:{offer.id}", event_id=event.id,
                                      payload={"offer": offer.id})
        await self.notifier.offered(offer, eligible if to_all else eligible[:1])
        await self.notifier.changed(event.id)

    async def accept(self, offer_id: int, member: Member) -> Outcome:
        """*Take it* on an offer."""
        async with self.db.session() as session:
            offer = await session.get(Offer, offer_id)
        if offer is None or offer.state != "open" or self.clock() >= offer.expires_at:
            return Outcome(Refusal.OFFER_GONE)
        if offer.member_id is not None and offer.member_id != member.id:
            return Outcome(Refusal.OFFER_GONE)
        outcome = await self.take(offer.event_id, offer.slot_id, member)
        return Outcome(Refusal.OFFER_GONE) if outcome.refusal == Refusal.OFFERED else outcome

    async def decline(self, offer_id: int, member: Member) -> Outcome:
        async with self.db.session() as session, session.begin():
            offer = await session.get(Offer, offer_id)
            if offer is None or offer.state != "open" or offer.member_id != member.id:
                return Outcome(Refusal.OFFER_GONE)
            offer.state = "declined"
            slot_id = offer.slot_id
        await self.scheduler.cancel(f"{OFFER_KIND}:{offer_id}")
        await self._offer(slot_id)
        return Outcome()

    async def _offer_expired(self, action: Action, now: datetime) -> None:
        async with self.db.session() as session, session.begin():
            offer = await session.get(Offer, action.payload["offer"])
            if offer is None or offer.state != "open":
                return None
            offer.state = "expired"
            slot_id = offer.slot_id
        await self._offer(slot_id)
        return None

    # ---------------------------------------------------------------- organisers

    async def lock(self, slot_id: int, locked: bool) -> None:
        """Lock a free slot closed, lock a held one as an assignment, or unlock it."""
        async with self.db.session() as session, session.begin():
            slot = await session.get(Slot, slot_id)
            if slot is None:
                return
            slot.locked = locked
            event_id, free = slot.event_id, slot.holder_id is None
            if locked and free:
                await session.execute(update(Offer).where(Offer.slot_id == slot_id, Offer.state == "open")
                                      .values(state="withdrawn"))
        if not locked and free:
            await self._offer(slot_id)
        await self.notifier.changed(event_id)

    async def assign(self, slot_id: int, member_id: int) -> None:
        """Put a member in a slot, locked: from their other slot in the event and off its lists. A previous holder
        goes to the top of the reserve list."""
        freed: int | None = None
        displaced: int | None = None
        async with self.db.session() as session, session.begin():
            slot = await session.get(Slot, slot_id)
            if slot is None:
                return
            event_id = slot.event_id
            if slot.holder_id == member_id:
                slot.locked = True
            else:
                displaced = slot.holder_id
                own = await self._held(session, event_id, member_id)
                if own is not None:
                    freed = _free(own)
                    await session.flush()
                roles = self.roles_of(member_id)
                slot.holder_id, slot.locked = member_id, True
                slot.flagged = slot.required_role_id is not None and (roles is None or slot.required_role_id not in roles)
                await session.execute(delete(Signup).where(Signup.event_id == event_id, Signup.member_id == member_id))
                await session.execute(update(Offer).where(Offer.slot_id == slot_id, Offer.state == "open")
                                      .values(state="withdrawn"))
                if displaced is not None:
                    await self._to_top(session, event_id, [displaced])
        if displaced is not None:
            await self.notifier.lost_slot(event_id, [displaced])
        if freed is not None:
            await self._offer(freed)
        await self.notifier.changed(event_id)

    async def remove(self, slot_id: int) -> None:
        """Take the holder out of a slot (organisers); the slot is offered on."""
        async with self.db.session() as session, session.begin():
            slot = await session.get(Slot, slot_id)
            if slot is None:
                return
            _free(slot)
            event_id = slot.event_id
        await self._offer(slot_id)
        await self.notifier.changed(event_id)

    async def set_orbat(self, event_id: int, squads: list[ParsedSquad]) -> list[int]:
        """Replace the ORBAT. A holder keeps the slot that matches by squad name, slot name and position among
        same-named slots (and an organiser's assignment stays locked); a slot now written with ``!`` is closed and
        loses its holder. Members whose slot is gone go to the top of the reserve list; they are returned."""
        async with self.db.session() as session, session.begin():
            event = await session.get(Event, event_id)
            if event is None or event.kind in COMING:
                raise ValueError("A Zeus event has no ORBAT")
            old: dict[tuple[str, str, int], tuple[int | None, bool]] = {}  # -> (holder, locked)
            count: dict[tuple[str, str], int] = {}
            rows = (await session.execute(select(Slot, Squad.name).join(Squad, Slot.squad_id == Squad.id)
                                          .where(Slot.event_id == event_id)
                                          .order_by(Squad.position, Slot.position))).all()
            for slot, squad_name in rows:
                key = (squad_name.casefold(), slot.name.casefold())
                count[key] = count.get(key, -1) + 1
                old[(*key, count[key])] = (slot.holder_id, slot.locked)
                session.expunge(slot)  # (the new slots may get the same ids)
            holders = [holder for holder, _ in old.values() if holder is not None]
            await session.execute(delete(Squad).where(Squad.event_id == event_id))  # slots and offers follow
            await session.flush()
            kept: set[int] = set()
            count = {}
            for squad_position, parsed in enumerate(squads):
                squad = Squad(event_id=event_id, position=squad_position, name=parsed.name)
                session.add(squad)
                await session.flush()
                for position, spec in enumerate(parsed.slots):
                    key = (parsed.name.casefold(), spec.name.casefold())
                    count[key] = count.get(key, -1) + 1
                    before = old.get((*key, count[key]))
                    if before is None or spec.locked:
                        holder, locked = None, spec.locked
                    else:
                        holder, locked = before[0], before[1] and before[0] is not None
                    roles = self.roles_of(holder) if holder is not None else None
                    flagged = holder is not None and spec.role_id is not None and (roles is None or spec.role_id not in roles)
                    session.add(Slot(event_id=event_id, squad_id=squad.id, position=position, name=spec.name,
                                     required_role_id=spec.role_id, holder_id=holder, locked=locked, flagged=flagged))
                    if holder is not None:
                        kept.add(holder)
            displaced = [member for member in holders if member not in kept]
            if displaced:
                await self._to_top(session, event_id, displaced)
        if displaced:
            await self.notifier.lost_slot(event_id, displaced)
        await self.notifier.changed(event_id)
        return displaced

    async def _to_top(self, session, event_id: int, members: list[int]) -> None:
        first = (await session.execute(select(func.min(Signup.reserve_position)).where(
            Signup.event_id == event_id, Signup.status == "reserve"))).scalar()
        start = (first if first is not None else 1) - len(members)
        for offset, member_id in enumerate(members):
            await self._set(session, event_id, member_id, "reserve", start + offset)

    # ---------------------------------------------------------------- members' roles

    async def member_changed(self, member: Member, previous: frozenset[int] | None = None) -> None:
        """Roles changed (``previous``: the roles before, when known). A member who lost their last sign-up role has
        their places in events not yet started freed (an organiser's assignment stays). A slot whose required role
        they lost is flagged ⚠, and unflagged again. While the sign-up roles aren't resolved (before the bot has
        seen the server, or a mistyped name) nobody counts as having lost one."""
        signup = self.ids().signup
        if signup and member.role_ids.isdisjoint(signup) and (previous is None or not previous.isdisjoint(signup)):
            await self._release(member.id, keep_assigned=True)
        touched: set[int] = set()
        async with self.db.session() as session, session.begin():
            for slot in (await session.execute(select(Slot).where(Slot.holder_id == member.id,
                                                                  Slot.required_role_id.is_not(None)))).scalars():
                flagged = slot.required_role_id not in member.role_ids
                if flagged != slot.flagged:
                    slot.flagged = flagged
                    touched.add(slot.event_id)
        for event_id in touched:
            await self.notifier.changed(event_id)

    async def member_left(self, member_id: int) -> None:
        """The member left the server: every place of theirs in events not yet started is freed."""
        await self._release(member_id, keep_assigned=False)

    async def _release(self, member_id: int, *, keep_assigned: bool) -> None:
        freed: list[int] = []
        touched: set[int] = set()
        async with self.db.session() as session, session.begin():
            upcoming = select(Event.id).where(Event.status.in_(ACTIVE), Event.start_at > self.clock())
            for slot in (await session.execute(select(Slot).where(Slot.holder_id == member_id,
                                                                  Slot.event_id.in_(upcoming)))).scalars():
                if not (keep_assigned and slot.locked):
                    touched.add(slot.event_id)
                    freed.append(_free(slot))
            places = (Signup.member_id == member_id, Signup.status.in_(("coming", "reserve")), Signup.event_id.in_(upcoming))
            touched |= set((await session.execute(select(Signup.event_id).where(*places))).scalars())
            await session.execute(delete(Signup).where(*places))
        for slot_id in freed:
            await self._offer(slot_id)
        for event_id in touched:
            await self.notifier.changed(event_id)
        if touched:
            await self.notifier.released(member_id, sorted(touched), left=not keep_assigned)

    # ---------------------------------------------------------------- storage

    @staticmethod
    async def _signup(session, event_id: int, member_id: int) -> Signup | None:
        return (await session.execute(select(Signup).where(Signup.event_id == event_id,
                                                           Signup.member_id == member_id))).scalar_one_or_none()

    async def _set(self, session, event_id: int, member_id: int, status: str, position: int | None = None) -> None:
        entry = await self._signup(session, event_id, member_id)
        if entry is None:
            session.add(Signup(event_id=event_id, member_id=member_id, status=status, reserve_position=position))
        else:
            entry.status, entry.reserve_position = status, position
        await session.flush()
