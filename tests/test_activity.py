"""Activity entries in the staff log thread (docs/PLAN.md, *Notifications*): who did what, one line each."""

import re

import pytest
from conftest import MEMBER
from test_events import MAKER, MEMBER_A, MOD, draft, events, ready  # noqa: F401 - the events fixture
from test_signup import MEDIC, make_event, member, offers, roles, signups, slots  # noqa: F401 - fixtures

from app.activity import Activity


@pytest.fixture
def lines(services):
    services.store.current = services.store.current.model_copy(update={"language": "en"})
    return []


def attach(service, services, lines):
    service.activity = Activity(services.db, services.store, lines.append, services.scheduler.clock)


def plain(lines):
    """Without the leading time, with the offers' deadlines as <t>."""
    out = []
    for line in lines:
        stamp, text = line.split(" ", 1)
        assert re.fullmatch(r"<t:\d+:T>", stamp)
        out.append(re.sub(r"<t:\d+:[a-zA-Z]>", "<t>", text))
    return out


async def test_sign_ups_offers_and_organisers(services, clock, signups, roles, lines):
    event = await make_event(services, clock, signups)
    attach(signups, services, lines)
    leader, medic, rifle1, rifle2 = await slots(services, event.id)
    for n in (1, 2, 3, 5):
        roles[n] = frozenset({MEMBER})
    roles[4] = frozenset({MEMBER, MEDIC})
    await signups.take(event.id, rifle1.id, member(1))
    await signups.take(event.id, rifle2.id, member(1))
    await signups.take(event.id, leader.id, member(2))
    await signups.take(event.id, rifle1.id, member(3))
    await signups.take(event.id, medic.id, member(4, MEDIC))
    await signups.reserve(event.id, member(5))
    await signups.answer(event.id, member(3), "maybe")  # rifle1 is offered to 5
    (offer,) = await offers(services, event.id)
    await signups.decline(offer.id, member(5))
    await signups.assign(rifle1.id, 6, by=99)
    await signups.lock(rifle1.id, False, by=99)
    await signups.remove(rifle1.id, by=99)  # 5 was asked about it already: no offer
    await signups.leave(event.id, member(1))  # rifle2 is offered to 5
    assert plain(lines) == [
        "<@1> took Alpha / Rifleman in **Op**.",
        "<@1> moved from Alpha / Rifleman to Alpha / Rifleman in **Op**.",
        "<@2> took Alpha / Squad Leader in **Op**.",
        "<@3> took Alpha / Rifleman in **Op**.",
        "<@4> took Alpha / Medic in **Op**.",
        "<@5> joined the reserve list of **Op**.",
        "<@3> answered Maybe for **Op**. (gave up Alpha / Rifleman)",
        "Alpha / Rifleman in **Op** offered to <@5> until <t>.",
        "<@5> declined Alpha / Rifleman in **Op**.",
        "<@99> put <@6> in Alpha / Rifleman in **Op**.",
        "<@99> unlocked Alpha / Rifleman in **Op**.",
        "<@99> took <@6> out of Alpha / Rifleman in **Op**.",
        "<@1> left Alpha / Rifleman in **Op**.",
        "Alpha / Rifleman in **Op** offered to <@5> until <t>.",
    ]


async def test_coming_and_not_coming(services, clock, signups, lines):
    event = await make_event(services, clock, signups, kind="zeus")
    attach(signups, services, lines)
    await signups.coming(event.id, member(1))
    await signups.answer(event.id, member(1), "cant")
    assert plain(lines) == ["<@1> answered Coming for **Op**.", "<@1> answered Not coming for **Op**."]


async def test_event_edits_show_old_and_new(events, clock, services, lines):
    attach(events, services, lines)
    event = await events.create(draft(clock), MAKER)
    await events.update(event.id, MAKER, title="Operation Y")
    await events.set_briefing(event.id, MAKER, "Situation", "Mission", "")
    await events.set_briefing(event.id, MAKER, "Situation", "Mission", "")  # the same: nothing to log
    await events.update(event.id, MAKER, times=draft(clock, days=6).times)
    await events.add_organiser(event.id, MAKER, 50)
    await events.remove_organiser(event.id, MAKER, 50)
    await events.cancel(event.id, MAKER, "Weather")
    got = plain(lines)
    assert got[:4] == [
        "<@20> created **Operation X** (Internal, assembly <t>).",
        "<@20> changed the title of **Operation Y**: Operation X → Operation Y.",
        "<@20> changed the briefing texts of **Operation Y**.",
        "<@20> changed the times of **Operation Y**: <t> / <t>, 3 h → <t> / <t>, 3 h.",
    ]
    assert got[4:] == [
        "<@20> made <@50> a co-organiser of **Operation Y**.",
        "<@20> removed <@50> from the co-organisers of **Operation Y**.",
        "<@20> deleted the draft **Operation Y**. Reason: Weather",
    ]


async def test_a_proposal_moves_nothing_so_logs_no_change(events, clock, services, lines):
    event = await events.create(draft(clock), MEMBER_A)
    await ready(events, event, MEMBER_A)
    await events.publish(event.id, MEMBER_A)
    await events.approve(events.recorder.calls[-1][1], MOD)
    attach(events, services, lines)
    assert await events.update(event.id, MEMBER_A, times=draft(clock, days=6).times) is not None
    assert lines == []  # the approval request is the staff note


async def test_an_entry_never_breaks_the_change(services, clock, signups, lines):
    event = await make_event(services, clock, signups)

    def broken(_text):
        raise RuntimeError("queue gone")

    signups.activity = Activity(services.db, services.store, broken, services.scheduler.clock)
    _, _, rifle, _ = await slots(services, event.id)
    assert (await signups.take(event.id, rifle.id, member(1))).ok
    assert (await slots(services, event.id))[2].holder_id == 1


async def test_off_reads_and_posts_nothing():
    off = Activity.off()
    assert await off.snapshot(1) == {}
    await off.took(1, 2, 3)  # no database: it would fail if it looked
