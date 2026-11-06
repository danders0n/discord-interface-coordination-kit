"""Sign-up (docs/PLAN.md, *ORBAT and slots*, *Zeus sign-up*), as a service with no Discord."""

import asyncio
from datetime import timedelta

import pytest
from conftest import KADET, MEMBER
from sqlalchemy import select

from app.db.models import Event, Offer, Signup, Slot
from app.orbat.parser import parse
from app.signup.service import COMING, Member, Refusal, SignupService

MEDIC = 3
ROLES = [(MEDIC, "Medic")]
ORBAT = "# Alpha\nSquad Leader\nMedic [Medic]\nRifleman x2\n"


class Recorder:
    """The Notifier, recording what the Discord layer would be told."""

    def __init__(self) -> None:
        self.redrawn: list[int] = []
        self.offers: list[tuple[int | None, list[int]]] = []
        self.lost: list[list[int]] = []

    async def changed(self, event_id):
        self.redrawn.append(event_id)

    async def offered(self, offer, member_ids):
        self.offers.append((offer.member_id, member_ids))

    async def lost_slot(self, event_id, member_ids):
        self.lost.append(member_ids)

    async def released(self, member_id, event_ids, left):
        self.released_from = (member_id, event_ids, left)


@pytest.fixture
def roles():
    return {}


@pytest.fixture
def signups(services, roles):
    notifier = Recorder()
    service = SignupService(services.db, services.store, lambda: services.ids, services.scheduler, notifier,
                            lambda member_id: roles.get(member_id))
    service.recorder = notifier
    return service


def member(n: int, *extra: int) -> Member:
    return Member(n, frozenset({MEMBER, *extra}))


async def make_event(services, clock, signups, *, kind="internal", days=3, status="published", orbat=ORBAT, **fields):
    start = clock.now + timedelta(days=days)
    event = Event(kind=kind, title="Op", status=status, assembly_at=start - timedelta(minutes=30), start_at=start,
                  duration_minutes=180, created_by=1, **fields)
    async with services.db.session() as session, session.begin():
        session.add(event)
    if orbat and kind not in COMING:
        await signups.set_orbat(event.id, parse(orbat, ROLES))
    return event


async def slots(services, event_id) -> list[Slot]:
    async with services.db.session() as session:
        return list((await session.execute(select(Slot).where(Slot.event_id == event_id).order_by(Slot.id))).scalars())


async def entries(services, event_id) -> dict[int, tuple[str, int | None]]:
    async with services.db.session() as session:
        rows = (await session.execute(select(Signup).where(Signup.event_id == event_id))).scalars()
        return {r.member_id: (r.status, r.reserve_position) for r in rows}


async def offers(services, event_id) -> list[Offer]:
    async with services.db.session() as session:
        return list((await session.execute(select(Offer).where(Offer.event_id == event_id).order_by(Offer.id))).scalars())


# ------------------------------------------------------------------ taking slots

async def test_take_move_and_leave(services, clock, signups):
    event = await make_event(services, clock, signups)
    leader, medic, rifle1, _ = await slots(services, event.id)
    assert (await signups.take(event.id, leader.id, member(10))).ok
    assert (await signups.take(event.id, rifle1.id, member(10))).ok  # moving
    holders = [s.holder_id for s in await slots(services, event.id)]
    assert holders == [None, None, 10, None]
    assert (await signups.take(event.id, rifle1.id, member(10))).refusal == Refusal.ALREADY
    assert (await signups.take(event.id, rifle1.id, member(11))).refusal == Refusal.TAKEN
    assert (await signups.take(event.id, medic.id, member(11))).refusal == Refusal.ROLE
    assert (await signups.take(event.id, medic.id, member(11, MEDIC))).ok
    assert (await signups.leave(event.id, member(10))).ok
    assert (await signups.leave(event.id, member(10))).refusal == Refusal.NOT_SIGNED_UP


async def test_two_members_at_once_never_share_a_slot(services, clock, signups):
    event = await make_event(services, clock, signups)
    leader = (await slots(services, event.id))[0]
    results = await asyncio.gather(*(signups.take(event.id, leader.id, member(n)) for n in range(20, 30)))
    assert sum(r.ok for r in results) == 1
    assert {r.refusal for r in results if not r.ok} == {Refusal.TAKEN}


async def test_when_sign_up_is_open(services, clock, signups):
    later = await make_event(services, clock, signups, signup_opens_at=clock.now + timedelta(days=1))
    draft = await make_event(services, clock, signups, days=10, status="draft")
    leader = (await slots(services, later.id))[0]
    assert (await signups.take(later.id, leader.id, member(10))).refusal == Refusal.NOT_OPEN
    assert (await signups.answer(later.id, member(10), "maybe")).ok  # Maybe works before it opens
    assert (await signups.take(draft.id, (await slots(services, draft.id))[0].id, member(10))).refusal == Refusal.NOT_PUBLISHED
    assert (await signups.take(later.id, leader.id, Member(10, frozenset({999})))).refusal == Refusal.NOT_ALLOWED
    assert (await signups.take(later.id, leader.id, Member(10, frozenset({KADET})))).refusal == Refusal.NOT_OPEN
    clock.now = later.start_at
    assert (await signups.take(later.id, leader.id, member(10))).refusal == Refusal.CLOSED


async def test_overlapping_events_are_refused_with_the_other_event(services, clock, signups):
    first = await make_event(services, clock, signups)
    second = await make_event(services, clock, signups, days=3)  # same evening
    elsewhere = await make_event(services, clock, signups, days=5)
    await signups.take(first.id, (await slots(services, first.id))[0].id, member(10))
    outcome = await signups.take(second.id, (await slots(services, second.id))[0].id, member(10))
    assert outcome.refusal == Refusal.OVERLAP and outcome.other_event == first.id
    assert (await signups.take(elsewhere.id, (await slots(services, elsewhere.id))[0].id, member(10))).ok


async def test_maybe_and_cant_free_the_slot(services, clock, signups):
    event = await make_event(services, clock, signups)
    leader = (await slots(services, event.id))[0]
    await signups.take(event.id, leader.id, member(10))
    assert (await signups.answer(event.id, member(10), "cant")).ok
    assert (await slots(services, event.id))[0].holder_id is None
    assert await entries(services, event.id) == {10: ("cant", None)}
    assert (await signups.answer(event.id, member(10), "cant")).refusal == Refusal.ALREADY
    assert (await signups.take(event.id, leader.id, member(10))).ok  # taking a slot clears the answer
    assert await entries(services, event.id) == {}


async def test_the_dlc_warning(services, clock, signups, settings):
    services.ids = type(services.ids)(**{**services.ids.__dict__, "dlc": {"vn": 501, "gm": 502}})
    event = await make_event(services, clock, signups, required_dlc=["vn", "gm", "spe"])
    outcome = await signups.take(event.id, (await slots(services, event.id))[0].id, member(10, 501))
    assert outcome.ok and outcome.missing_dlc == ["gm"]  # spe has no role configured: not judged


# ------------------------------------------------------------------ reserve list and offers

async def full_event(services, clock, signups, **fields):
    event = await make_event(services, clock, signups, orbat="# A\nRifleman x2\n", **fields)
    one, two = await slots(services, event.id)
    await signups.take(event.id, one.id, member(10))
    await signups.take(event.id, two.id, member(11))
    return event, one, two


async def test_reserve_only_when_full_and_in_order(services, clock, signups, roles):
    event = await make_event(services, clock, signups, orbat="# A\nRifleman\n")
    assert (await signups.reserve(event.id, member(20))).refusal == Refusal.NOT_FULL
    await signups.take(event.id, (await slots(services, event.id))[0].id, member(10))
    assert (await signups.reserve(event.id, member(20))).ok
    assert (await signups.reserve(event.id, member(21))).ok
    assert (await signups.reserve(event.id, member(20))).refusal == Refusal.ALREADY
    assert await entries(services, event.id) == {20: ("reserve", 1), 21: ("reserve", 2)}


async def test_a_freed_slot_is_offered_in_turn(services, clock, signups, roles):
    event, one, _ = await full_event(services, clock, signups)
    roles.update({20: frozenset({MEMBER}), 21: frozenset({MEMBER})})
    await signups.reserve(event.id, member(20))
    await signups.reserve(event.id, member(21))
    await signups.leave(event.id, member(10))
    assert signups.recorder.offers == [(20, [20])]
    assert (await signups.take(event.id, one.id, member(30))).refusal == Refusal.OFFERED  # held for the offer
    (offer,) = await offers(services, event.id)
    assert offer.expires_at == clock.now + timedelta(minutes=30)
    assert (await signups.accept(offer.id, member(21))).refusal == Refusal.OFFER_GONE  # not theirs
    clock.now = offer.expires_at
    await services.scheduler.run_due()  # nobody answered: the next one
    assert signups.recorder.offers[-1] == (21, [21])
    second = (await offers(services, event.id))[-1]
    assert (await signups.accept(second.id, member(21))).ok
    assert (await slots(services, event.id))[0].holder_id == 21
    assert await entries(services, event.id) == {20: ("reserve", 1)}


async def test_declining_moves_the_offer_on_and_roles_are_respected(services, clock, signups, roles):
    event = await make_event(services, clock, signups, orbat="# A\nMedic [Medic]\n")
    medic = (await slots(services, event.id))[0]
    await signups.take(event.id, medic.id, member(10, MEDIC))
    roles.update({20: frozenset({MEMBER}), 21: frozenset({MEMBER, MEDIC})})
    await signups.reserve(event.id, member(20))
    await signups.reserve(event.id, member(21, MEDIC))
    await signups.leave(event.id, member(10))
    assert signups.recorder.offers == [(21, [21])]  # 20 is no medic
    (offer,) = await offers(services, event.id)
    assert (await signups.decline(offer.id, member(21))).ok
    assert len(await offers(services, event.id)) == 1  # nobody else may take it: no new offer


async def test_close_to_the_assembly_the_whole_list_is_asked(services, clock, signups, roles):
    event, one, _ = await full_event(services, clock, signups, days=1)
    roles.update({20: frozenset({MEMBER}), 21: frozenset({MEMBER})})
    await signups.reserve(event.id, member(20))
    await signups.reserve(event.id, member(21))
    clock.now = event.assembly_at - timedelta(minutes=30)
    await signups.leave(event.id, member(10))
    assert signups.recorder.offers == [(None, [20, 21])]
    (offer,) = await offers(services, event.id)
    assert offer.expires_at == event.start_at
    assert (await signups.take(event.id, one.id, member(21))).ok  # anyone on the list, even via Sign up
    assert (await signups.accept(offer.id, member(20))).refusal == Refusal.OFFER_GONE


async def test_no_offers_after_the_start(services, clock, signups, roles):
    event, _, _ = await full_event(services, clock, signups)
    roles[20] = frozenset({MEMBER})
    await signups.reserve(event.id, member(20))
    await signups.remove((await slots(services, event.id))[0].id)
    assert len(signups.recorder.offers) == 1
    clock.now = event.start_at + timedelta(minutes=1)
    await signups.remove((await slots(services, event.id))[1].id)
    assert len(signups.recorder.offers) == 1


# ------------------------------------------------------------------ organisers

async def test_locks_and_assignments(services, clock, signups, roles):
    event = await make_event(services, clock, signups)
    leader, medic, rifle, _ = await slots(services, event.id)
    await signups.lock(leader.id, True)
    assert (await signups.take(event.id, leader.id, member(10))).refusal == Refusal.LOCKED
    await signups.take(event.id, rifle.id, member(10))
    roles[10] = frozenset({MEMBER})
    await signups.assign(medic.id, 10)  # moved from Rifleman, locked, flagged: no Medic role
    medic_now = (await slots(services, event.id))[1]
    assert medic_now.holder_id == 10 and medic_now.locked and medic_now.flagged
    assert (await slots(services, event.id))[2].holder_id is None
    assert (await signups.leave(event.id, member(10))).refusal == Refusal.LOCKED
    assert (await signups.answer(event.id, member(10), "cant")).refusal == Refusal.LOCKED
    await signups.assign(medic.id, 11)  # someone else: 10 goes to the top of the reserve list
    assert signups.recorder.lost == [[10]] and (await entries(services, event.id))[10][0] == "reserve"


async def test_an_orbat_edited_after_sign_ups(services, clock, signups, roles):
    event = await make_event(services, clock, signups, orbat="# Alpha\nSquad Leader\nRifleman x3\n# Bravo\nRifleman\n")
    alpha_lead, r1, r2, r3, bravo = await slots(services, event.id)
    for n, slot in zip((10, 11, 12, 13, 14), (alpha_lead, r1, r2, r3, bravo)):
        await signups.take(event.id, slot.id, member(n))
    await signups.lock(r1.id, True)  # 11 is assigned
    roles[20] = frozenset({MEMBER})
    await signups.reserve(event.id, member(20))
    displaced = await signups.set_orbat(event.id, parse("# alpha\nSquad Leader\nRifleman x2\n!Zeus\n", ROLES))
    assert sorted(displaced) == [13, 14]  # the third Rifleman and Bravo are gone
    now = await slots(services, event.id)
    assert [(s.name, s.holder_id, s.locked) for s in now] == [("Squad Leader", 10, False), ("Rifleman", 11, True),
                                                              ("Rifleman", 12, False), ("Zeus", None, True)]
    reserve = await entries(services, event.id)
    assert reserve[13][1] < reserve[20][1] and reserve[14][1] < reserve[20][1]  # ahead of those already waiting
    assert signups.recorder.lost == [[13, 14]]


async def test_closing_a_held_slot_with_a_bang(services, clock, signups):
    event = await make_event(services, clock, signups, orbat="# A\nZeus\n")
    await signups.take(event.id, (await slots(services, event.id))[0].id, member(10))
    assert await signups.set_orbat(event.id, parse("# A\n!Zeus\n", ROLES)) == [10]


# ------------------------------------------------------------------ Zeus

@pytest.mark.parametrize("kind", COMING)
async def test_zeus_coming_with_a_limit_even_in_a_rush(services, clock, signups, kind):
    event = await make_event(services, clock, signups, kind=kind, signup_limit=3)
    results = await asyncio.gather(*(signups.coming(event.id, member(n)) for n in range(10, 18)))
    assert sum(r.ok for r in results) == 3 and {r.refusal for r in results if not r.ok} == {Refusal.FULL}
    coming = [m for m, (status, _) in (await entries(services, event.id)).items() if status == "coming"]
    assert len(coming) == 3
    assert (await signups.coming(event.id, member(coming[0]))).refusal == Refusal.ALREADY
    assert (await signups.leave(event.id, member(coming[0]))).ok
    assert (await signups.coming(event.id, member(99))).ok  # room again
    assert (await signups.answer(event.id, member(98), "cant")).ok  # Not coming
    assert (await signups.answer(event.id, member(98), "maybe")).refusal == Refusal.WRONG_KIND
    assert (await signups.reserve(event.id, member(97))).refusal == Refusal.WRONG_KIND


async def test_zeus_coming_counts_for_the_overlap(services, clock, signups):
    zeus = await make_event(services, clock, signups, kind="zeus")
    other = await make_event(services, clock, signups)
    await signups.coming(zeus.id, member(10))
    outcome = await signups.take(other.id, (await slots(services, other.id))[0].id, member(10))
    assert outcome.refusal == Refusal.OVERLAP and outcome.other_event == zeus.id


# ------------------------------------------------------------------ members' roles

async def test_losing_the_sign_up_role_frees_places_but_not_assignments(services, clock, signups, roles):
    event = await make_event(services, clock, signups)
    leader, _, rifle, _ = await slots(services, event.id)
    other = await make_event(services, clock, signups, days=6)
    await signups.take(event.id, rifle.id, member(10))
    await signups.take(other.id, (await slots(services, other.id))[0].id, member(10))
    await signups.lock((await slots(services, other.id))[0].id, True)
    await signups.member_changed(Member(10, frozenset({999})), previous=frozenset({MEMBER}))
    assert (await slots(services, event.id))[2].holder_id is None
    assert (await slots(services, other.id))[0].holder_id == 10  # the organiser's assignment stays
    assert signups.recorder.released_from == (10, [event.id], False)  # for the staff log


async def test_only_losing_the_role_frees_places(services, clock, signups):
    import dataclasses

    event = await make_event(services, clock, signups)
    rifle = (await slots(services, event.id))[2]
    await signups.assign(rifle.id, 10)  # e.g. a guest Zeus, without a sign-up role
    await signups.lock(rifle.id, False)
    await signups.member_changed(Member(10, frozenset({999})), previous=frozenset({998}))  # never had one
    assert (await slots(services, event.id))[2].holder_id == 10
    resolved = services.ids
    services.ids = dataclasses.replace(resolved, signup=frozenset())  # not resolved yet, or a mistyped name
    await signups.member_changed(Member(10, frozenset({999})))
    assert (await slots(services, event.id))[2].holder_id == 10
    services.ids = resolved


async def test_losing_a_slots_role_flags_it_and_leaving_the_server_frees_everything(services, clock, signups):
    event = await make_event(services, clock, signups)
    medic = (await slots(services, event.id))[1]
    await signups.take(event.id, medic.id, member(10, MEDIC))
    await signups.lock(medic.id, True)
    await signups.member_changed(member(10))
    assert (await slots(services, event.id))[1].flagged
    await signups.member_changed(member(10, MEDIC))
    assert not (await slots(services, event.id))[1].flagged
    await signups.member_left(10)
    assert (await slots(services, event.id))[1].holder_id is None
