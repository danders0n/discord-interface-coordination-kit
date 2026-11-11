"""Events as a service (docs/PLAN.md, *Event kinds* to *Scheduled actions and downtime*), on the fake supervisor."""

import dataclasses
from datetime import timedelta

import pytest
from conftest import ADMIN, MEMBER, MISSION_MAKER, MODERATOR

from app.events.groups import Groups
from app.events.service import Draft, EventError, EventService, Problem
from app.events.times import Times
from app.orbat.parser import parse
from app.signup.service import COMING, Member, SignupService

CLIMATE = 401


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        async def record(*args, **kwargs):
            self.calls.append((name, *args, *kwargs.values()))
        return record

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def events(services, fake, guild):
    from fakes import FakeRole

    guild.add(FakeRole(CLIMATE, "Cold War"))
    services.store.current = services.settings.model_copy(update={"events": services.settings.events.model_copy(
        update={"climate_roles": [CLIMATE]})})
    from app.resolve import resolve
    services.ids, _ = resolve(guild, services.settings)
    notifier = Recorder()
    service = EventService(services.db, services.store, lambda: services.ids, services.scheduler,
                           services.reservations, notifier, link=lambda e: f"https://discord.com/channels/1/{e.id}")
    service.recorder = notifier
    service.signups = SignupService(services.db, services.store, lambda: services.ids, services.scheduler, Recorder(),
                                    lambda _m: frozenset())
    return service


MEMBER_A = Member(10, frozenset({MEMBER}))
MAKER = Member(20, frozenset({MEMBER, MISSION_MAKER}))
MOD = Member(30, frozenset({MEMBER, MODERATOR}))
ADMIN_ = Member(40, frozenset({ADMIN}))


def draft(clock, kind="internal", days=5, hours=0, **fields) -> Draft:
    assembly = clock.now + timedelta(days=days, hours=hours)
    return Draft(kind=kind, title="Operation X", climate_role_id=CLIMATE,
                 times=Times(assembly, assembly + timedelta(minutes=30), 180), **fields)


async def ready(events, event, member):
    """Give the event what publishing needs."""
    await events.set_briefing(event.id, member, "Situation", "Mission", "")
    if event.kind not in COMING:
        await events.signups.set_orbat(event.id, parse("# A\nRifleman x2\n", []))
    if event.kind == "guest":
        await events.set_how_to_join(event.id, member, "127.0.0.1:2302, password x")


def at(clock, fake, when):
    clock.now = fake.now = when


# ------------------------------------------------------------------ creating

async def test_a_mission_maker_gets_the_reservation_at_once(events, fake, clock):
    event = await events.create(draft(clock), MAKER)
    assert event.status == "draft" and event.climate == "Cold War" and event.author_id == MAKER.id
    (r,) = fake.reservations.values()
    assert r["starts_at"] == event.assembly_at - timedelta(minutes=45)
    assert r["ends_at"] == event.start_at + timedelta(minutes=180 + 30)
    assert r["name"] == f"{await events.channel_name(event)} Operation X" and r["link"].endswith(f"/{event.id}")


async def test_a_members_draft_has_no_reservation_yet(events, fake, clock):
    event = await events.create(draft(clock), MEMBER_A)
    assert fake.reservations == {} and await events.reservations.get(event.id) is None


async def test_who_may_create_what(events, clock, services):
    with pytest.raises(EventError) as caught:
        await events.create(draft(clock), Member(1, frozenset()))
    assert caught.value.problem == Problem.NOT_ALLOWED
    with pytest.raises(EventError) as caught:
        await events.create(draft(clock, "guest"), MEMBER_A)
    assert caught.value.problem == Problem.KIND_NOT_ALLOWED
    with pytest.raises(EventError) as caught:
        await events.create(draft(clock, "guest"), MAKER)
    assert caught.value.problem == Problem.NEEDS_HOST
    bad = draft(clock)
    bad.climate_role_id = 999
    with pytest.raises(EventError) as caught:
        await events.create(bad, MAKER)
    assert caught.value.problem == Problem.UNKNOWN_CLIMATE


async def test_a_guest_event_is_named_after_its_host_and_never_reserves(events, fake, clock, services):
    abc = await Groups(services.db).add("abc", "Alpha Bravo", MAKER.id)
    event = await events.create(draft(clock, "guest", host_group_id=abc.id), MAKER)
    assert (await events.channel_name(event)).endswith("-abc") and fake.reservations == {}
    ours = await events.create(draft(clock, days=6), MAKER)
    assert (await events.channel_name(ours)).endswith("-ibc")


async def test_stale_actions_only_when_still_ahead(events, clock, services):
    far = await events.create(draft(clock, days=5), MAKER)
    near = await events.create(draft(clock, days=0, hours=20), MAKER)  # inside both windows already
    assert await services.scheduler.exists(f"stale_ping:{far.id}") and await services.scheduler.exists(f"stale_cancel:{far.id}")
    assert not await services.scheduler.exists(f"stale_ping:{near.id}")
    assert await services.scheduler.exists(f"stale_cancel:{near.id}")  # not at once: at its start


async def test_an_unpublished_event_never_outlives_its_start(events, clock, services):
    """A draft made inside the cancel window used to get no cancel action and stay forever (decided 2026-11-11:
    cancelled at the start at the latest)."""
    near = await events.create(draft(clock, days=0, hours=20), MAKER)
    clock.now = near.start_at + timedelta(minutes=1)
    await services.scheduler.run_due()
    assert (await events.get(near.id)).status == "cancelled"
    assert "deleted" in events.recorder.names()


async def test_a_draft_can_be_cancelled_by_hand_after_its_start(events, clock):
    event = await events.create(draft(clock, days=0, hours=20), MAKER)
    clock.now = event.start_at + timedelta(hours=1)
    await events.cancel(event.id, MAKER, "never happened")
    assert (await events.get(event.id)).status == "cancelled"


async def test_resume_gives_an_old_draft_its_cancel_action(events, clock, services):
    event = await events.create(draft(clock, days=0, hours=20), MAKER)
    await services.scheduler.cancel(f"stale_cancel:{event.id}")  # as a draft made before the fix
    await events.resume()
    assert await services.scheduler.exists(f"stale_cancel:{event.id}")


# ------------------------------------------------------------------ publishing and approval

async def test_publishing_needs_the_checklist(events, clock, services):
    abc = await Groups(services.db).add("ABC", "Alpha Bravo", MAKER.id)
    event = await events.create(draft(clock, "guest", host_group_id=abc.id), MAKER)
    with pytest.raises(EventError) as caught:
        await events.publish(event.id, MAKER)
    assert caught.value.problem == Problem.INCOMPLETE and caught.value.missing == ["briefing", "orbat", "how_to_join"]
    zeus = await events.create(draft(clock, "zeus", days=8), MAKER)
    with pytest.raises(EventError) as caught:
        await events.publish(zeus.id, MAKER)
    assert caught.value.missing == ["briefing"]


async def test_a_mission_maker_publishes_and_the_actions_are_set(events, clock, services):
    event = await events.create(draft(clock), MAKER)
    await ready(events, event, MAKER)
    assert await events.publish(event.id, MAKER) == "published"
    assert "published" in events.recorder.names()
    assert not await services.scheduler.exists(f"stale_ping:{event.id}")
    assert await services.scheduler.exists(f"reminder:{event.id}:1440") and await services.scheduler.exists(f"reminder:{event.id}:60")


async def test_a_members_event_goes_through_approval(events, fake, clock):
    event = await events.create(draft(clock), MEMBER_A)
    await ready(events, event, MEMBER_A)
    assert await events.publish(event.id, MEMBER_A) == "pending"
    assert (await events.get(event.id)).status == "pending" and len(fake.reservations) == 1  # the clash would show
    with pytest.raises(EventError) as caught:
        await events.set_briefing(event.id, MEMBER_A, "changed", "", "")
    assert caught.value.problem == Problem.PENDING  # locked while waiting
    request_id = events.recorder.calls[-1][1]
    with pytest.raises(EventError):
        await events.approve(request_id, MEMBER_A)  # members don't approve
    await events.approve(request_id, MOD)
    assert (await events.get(event.id)).status == "published"
    assert events.recorder.names()[-2:] == ["published", "approval_decided"]


async def test_rejecting_and_withdrawing_drop_the_reservation(events, fake, clock):
    event = await events.create(draft(clock), MEMBER_A)
    await ready(events, event, MEMBER_A)
    await events.publish(event.id, MEMBER_A)
    request_id = events.recorder.calls[-1][1]
    rejected = await events.reject(request_id, MOD, "Not on a Monday")
    assert rejected.reason == "Not on a Monday" and (await events.get(event.id)).status == "draft"
    assert fake.reservations == {}
    await events.publish(event.id, MEMBER_A)
    await events.withdraw(event.id, MEMBER_A)
    assert (await events.get(event.id)).status == "draft" and fake.reservations == {}


async def test_approve_is_blocked_while_the_reservation_failed(events, fake, clock):
    other = await events.create(draft(clock), MAKER)  # holds the host that evening
    event = await events.create(draft(clock, hours=1), MEMBER_A)
    await ready(events, event, MEMBER_A)
    await events.publish(event.id, MEMBER_A)
    request_id = events.recorder.calls[-1][1]
    assert (await events.reservations.get(event.id)).state == "failed"
    with pytest.raises(EventError) as caught:
        await events.approve(request_id, MOD)
    assert caught.value.problem == Problem.NO_RESERVATION
    await events.cancel(other.id, MAKER, "moved")
    await events.reservations.retry(event.id)  # *Retry reservation*
    await events.approve(request_id, MOD)
    assert (await events.get(event.id)).status == "published"


async def test_the_presser_decides_not_the_creator(events, clock):
    event = await events.create(draft(clock), MEMBER_A)
    await events.add_organiser(event.id, MEMBER_A, MAKER.id)
    await ready(events, event, MEMBER_A)
    assert await events.publish(event.id, MAKER) == "published"  # a mission maker co-organiser publishes
    own = await events.create(draft(clock, days=7), MOD)
    await ready(events, own, MOD)
    assert await events.publish(own.id, MOD) == "published"  # a moderator just publishes


@pytest.mark.parametrize("kind", COMING)
async def test_a_members_zeus_or_training_event_publishes_but_asks_for_the_server(events, fake, clock, kind):
    event = await events.create(draft(clock, kind), MEMBER_A)
    await ready(events, event, MEMBER_A)
    assert await events.publish(event.id, MEMBER_A) == "published" and fake.reservations == {}
    request = await events.request_server(event.id, MEMBER_A)
    assert request.kind == "server" and len(fake.reservations) == 1
    assert await events.server_pending(event.id) and events.recorder.calls[-1] == ("changed", event.id)
    assert (await events.request_server(event.id, MEMBER_A)).id == request.id  # asking twice: the same request
    await events.reject(request.id, MOD, "Not this week")
    assert fake.reservations == {}


# ------------------------------------------------------------------ times and kinds

async def test_a_drafts_times_move_its_reservation(events, fake, clock):
    event = await events.create(draft(clock), MAKER)
    later = draft(clock, days=6).times
    assert await events.update(event.id, MAKER, times=later) is None
    (r,) = fake.reservations.values()
    assert r["starts_at"] == later.assembly_at - timedelta(minutes=45)


async def test_a_members_new_times_are_a_proposal(events, fake, clock):
    event = await events.create(draft(clock), MEMBER_A)
    await ready(events, event, MEMBER_A)
    await events.publish(event.id, MEMBER_A)
    await events.approve(events.recorder.calls[-1][1], MOD)
    before = (await events.get(event.id)).assembly_at
    proposal = await events.update(event.id, MEMBER_A, times=draft(clock, days=6).times)
    assert proposal is not None and proposal.kind == "times"
    assert (await events.get(event.id)).assembly_at == before  # nothing moves yet
    await events.reject(proposal.id, MOD, "No")
    assert (await events.get(event.id)).assembly_at == before
    proposal = await events.update(event.id, MEMBER_A, times=draft(clock, days=6).times)
    await events.approve(proposal.id, MOD)
    moved = await events.get(event.id)
    assert moved.assembly_at == draft(clock, days=6).times.assembly_at
    (r,) = fake.reservations.values()
    assert r["starts_at"] == moved.assembly_at - timedelta(minutes=45)


async def test_a_proposal_keeps_the_other_changes_made_with_it(events, fake, clock):
    event = await events.create(draft(clock), MEMBER_A)
    await ready(events, event, MEMBER_A)
    await events.publish(event.id, MEMBER_A)
    await events.approve(events.recorder.calls[-1][1], MOD)
    proposal = await events.update(event.id, MEMBER_A, title="Operation Y", times=draft(clock, days=6).times)
    assert proposal is not None
    saved = await events.get(event.id)
    assert saved.title == "Operation Y" and saved.assembly_at == draft(clock).times.assembly_at


async def test_an_approved_move_asks_again_for_a_failed_reservation_at_the_new_times(events, fake, clock):
    from app.db.models import HostReservation

    event = await events.create(draft(clock), MEMBER_A)
    await ready(events, event, MEMBER_A)
    await events.publish(event.id, MEMBER_A)
    await events.approve(events.recorder.calls[-1][1], MOD)
    proposal = await events.update(event.id, MEMBER_A, times=draft(clock, days=6).times)
    async with events.db.session() as session, session.begin():  # the reservation failed meanwhile
        (await session.get(HostReservation, event.id)).state = "failed"
    await events.approve(proposal.id, MOD)
    asked = await events.reservations.get(event.id)  # what a later *Retry reservation* sends
    assert asked.starts_at == draft(clock, days=6).times.assembly_at - timedelta(minutes=45)


async def test_reminders_follow_the_offsets_in_force(events, clock, services):
    event = await events.create(draft(clock), MAKER)
    await ready(events, event, MAKER)
    await events.publish(event.id, MAKER)
    times = services.settings.times.model_copy(update={"reminder_minutes": [120]})
    services.store.current = services.settings.model_copy(update={"times": times})  # a /reload
    await events.update(event.id, MAKER, times=draft(clock, days=6).times)
    assert not await services.scheduler.exists(f"reminder:{event.id}:60")
    assert await services.scheduler.exists(f"reminder:{event.id}:120")
    await events.cancel(event.id, MAKER, "x")
    assert not await services.scheduler.exists(f"reminder:{event.id}:120")


async def test_a_refused_preset_or_image_leaves_the_files_alone(events, clock, services):
    event = await events.create(draft(clock), MEMBER_A)
    await ready(events, event, MEMBER_A)
    await events.publish(event.id, MEMBER_A)  # pending: locked
    with pytest.raises(EventError):
        await events.set_image(event.id, MEMBER_A, b"\x89PNG", ".png")
    with pytest.raises(EventError):
        await events.set_preset(event.id, MEMBER_A, b"<a href='filedetails/?id=1'>x</a>")
    assert not (services.settings.data_dir / "images").exists() and not (services.settings.data_dir / "presets").exists()


async def test_events_using_an_item_removed_from_the_settings(events, clock, services):
    event = await events.create(draft(clock), MAKER)
    assert await events.affected_by_config() == []
    services.ids = dataclasses.replace(services.ids, climates={})  # the climate role left the settings
    assert [e.id for e in await events.affected_by_config()] == [event.id]


async def test_an_approved_move_into_a_clash_changes_nothing(events, fake, clock):
    blocker = await events.create(draft(clock, days=6), MAKER)
    event = await events.create(draft(clock), MEMBER_A)
    await ready(events, event, MEMBER_A)
    await events.publish(event.id, MEMBER_A)
    await events.approve(events.recorder.calls[-1][1], MOD)
    proposal = await events.update(event.id, MEMBER_A, times=draft(clock, days=6).times)
    with pytest.raises(EventError) as caught:
        await events.approve(proposal.id, MOD)
    assert caught.value.problem == Problem.NO_RESERVATION
    assert (await events.get(event.id)).assembly_at == draft(clock).times.assembly_at
    assert (await events.reservations.get(event.id)).state == "booked" and blocker


async def test_times_are_locked_from_the_quiet_period(events, fake, clock):
    event = await events.create(draft(clock), MAKER)
    at(clock, fake, event.assembly_at - timedelta(minutes=60))  # the reservation starts 45 min before: quiet now
    with pytest.raises(EventError) as caught:
        await events.update(event.id, MAKER, times=draft(clock, days=2).times)
    assert caught.value.problem == Problem.TIMES_LOCKED
    await events.update(event.id, MAKER, title="Operation Y")  # other details still change
    assert (await events.get(event.id)).title == "Operation Y"


async def test_the_kind_changes_only_in_a_draft(events, fake, clock, services):
    abc = await Groups(services.db).add("ABC", "Alpha Bravo", MAKER.id)
    event = await events.create(draft(clock), MAKER)
    await events.update(event.id, MAKER, kind="guest", host_group_id=abc.id)
    assert fake.reservations == {}  # a guest event plays elsewhere
    await events.update(event.id, MAKER, kind="internal", host_group_id=None)
    assert len(fake.reservations) == 1
    await ready(events, await events.get(event.id), MAKER)
    await events.publish(event.id, MAKER)
    with pytest.raises(EventError) as caught:
        await events.update(event.id, MAKER, kind="zeus")
    assert caught.value.problem == Problem.NOT_DRAFT
    mine = await events.create(draft(clock, days=9), MEMBER_A)
    with pytest.raises(EventError) as caught:
        await events.update(mine.id, MEMBER_A, kind="guest")
    assert caught.value.problem == Problem.KIND_NOT_ALLOWED


async def test_only_organisers_edit(events, clock):
    event = await events.create(draft(clock), MEMBER_A)
    with pytest.raises(EventError) as caught:
        await events.update(event.id, Member(11, frozenset({MEMBER})), title="mine now")
    assert caught.value.problem == Problem.NOT_ALLOWED
    await events.add_organiser(event.id, MEMBER_A, 11)
    await events.update(event.id, Member(11, frozenset({MEMBER})), title="ours")
    await events.update(event.id, ADMIN_, title="any event")  # manage any event
    assert (await events.get(event.id)).title == "any event"


# ------------------------------------------------------------------ cancelling and ending

async def test_cancelling_a_draft_deletes_it_and_a_published_event_is_cancelled(events, fake, clock):
    draft_event = await events.create(draft(clock), MAKER)
    await events.cancel(draft_event.id, MAKER, "no Zeus")
    assert events.recorder.calls[-1] == ("deleted", draft_event.id) and fake.reservations == {}
    event = await events.create(draft(clock, days=6), MAKER)
    await ready(events, event, MAKER)
    await events.publish(event.id, MAKER)
    await events.cancel(event.id, MAKER, "weather")
    assert events.recorder.calls[-1] == ("cancelled", event.id)
    assert (await events.get(event.id)).cancel_reason == "weather"


async def test_after_the_start_end_not_cancel(events, fake, clock):
    event = await events.create(draft(clock), MAKER)
    await ready(events, event, MAKER)
    await events.publish(event.id, MAKER)
    at(clock, fake, event.start_at + timedelta(minutes=10))
    with pytest.raises(EventError) as caught:
        await events.cancel(event.id, MAKER, "late")
    assert caught.value.problem == Problem.STARTED
    await events.end(event.id, MAKER)
    assert (await events.get(event.id)).status == "ended" and next(iter(fake.reservations.values()))["ended"]


# ------------------------------------------------------------------ timed actions

async def test_sign_up_opening_and_its_ping_after_downtime(events, clock, services):
    event = await events.create(draft(clock), MAKER)
    await events.set_signup_start(event.id, MAKER, clock.now + timedelta(days=1))
    await ready(events, event, MAKER)
    await events.publish(event.id, MAKER)
    clock.now += timedelta(days=1, hours=2)  # the bot was down for two hours
    await services.scheduler.run_due()
    assert ("signup_opened", event.id, False) in events.recorder.calls  # opened, but too late to ping


async def test_a_reminder_gives_way_to_a_later_one(events, clock, services):
    event = await events.create(draft(clock, days=2), MAKER)
    await ready(events, event, MAKER)
    await events.publish(event.id, MAKER)
    clock.now = event.assembly_at - timedelta(minutes=30)  # down through both reminder times
    await services.scheduler.run_due()
    assert events.recorder.names().count("reminder") == 1  # only the 1 h one speaks


async def test_a_stale_draft_is_pinged_then_cancelled(events, fake, clock, services):
    event = await events.create(draft(clock, days=3), MAKER)
    at(clock, fake, event.assembly_at - timedelta(hours=47))
    await services.scheduler.run_due()
    assert ("stale", event.id) in events.recorder.calls
    at(clock, fake, event.assembly_at - timedelta(hours=23))
    await services.scheduler.run_due()
    assert (await events.get(event.id)).status == "cancelled" and ("deleted", event.id) in events.recorder.calls
    assert fake.reservations == {}


async def test_a_guest_event_ends_by_the_clock(events, clock, services):
    abc = await Groups(services.db).add("ABC", "Alpha Bravo", MAKER.id)
    event = await events.create(draft(clock, "guest", host_group_id=abc.id), MAKER)
    await ready(events, event, MAKER)
    await events.publish(event.id, MAKER)
    clock.now = event.start_at + timedelta(minutes=180 + 30)
    await services.scheduler.run_due()
    assert (await events.get(event.id)).status == "ended"


async def test_other_groups_are_added_once_and_corrected(services):
    groups = Groups(services.db)
    first = await groups.add(" abc ", "Alpha  Bravo", 1)
    again = await groups.add("ABC", "Something else", 2)
    assert again.id == first.id and first.name == "Alpha Bravo"
    await groups.add("XYZ", "X-ray", 1)
    fixed = await groups.correct(first.id, "AB", "Alpha Bravo Company")
    assert fixed.acronym == "AB" and [g.acronym for g in await groups.search("x")] == ["XYZ"]
