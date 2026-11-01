"""Phase 6a: the attendance trigger, reading the plays, the report and its corrections (docs/PLAN.md, *Supervisor:
attendance*), on the Discord fakes and the fake supervisor."""

import asyncio
from datetime import timedelta

import discord
import pytest
from conftest import MEMBER, MISSION_MAKER, settle
from fakes import FakeInteraction, FakeMember, FakeRole

from app.attendance.service import READ, AttendanceService, compute, finished_at
from app.attendance.stats import Play, Player, StatsReader
from app.attendance.ui import Editor, ReportAction
from app.eventchannel.service import ARCHIVE_KIND, EventChannels
from app.events.groups import Groups
from app.events.header import Header
from app.events.service import Draft, EventService
from app.events.times import Times
from app.events.ui import PanelAction
from app.links.service import LinkService
from app.orbat.parser import parse
from app.signup.service import Member, SignupService
from app.squadxml.service import SquadXml
from app.staff_threads import StaffThreads

ENGLISH = discord.Locale.british_english
MAKER = Member(20, frozenset({MEMBER, MISSION_MAKER}))
ANNA, BOB, MATCH, STRANGER, SHORT = ("76561198000000001", "76561198000000002", "76561198000000003",
                                     "76561198000000004", "76561198000000005")


@pytest.fixture
async def stack(services, fake, guild, clock):
    channels = EventChannels(services.db, services.store, lambda: services.ids, services.scheduler, lambda: guild,
                             interval=0)
    events = EventService(services.db, services.store, lambda: services.ids, services.scheduler,
                          services.reservations, channels, link=channels.link)
    signups = SignupService(services.db, services.store, lambda: services.ids, services.scheduler, channels,
                            lambda member_id: frozenset({MEMBER}))
    channels.events, channels.signups = events, signups
    channels.header = Header(services.db, services.store, lambda: services.ids, lambda: guild, clock, interval=0)
    channels.staff, channels.services = services.staff, services
    channels.threads = services.threads = StaffThreads(services.db, services.store)
    links = LinkService(services.db, clock)
    names = lambda member_id: [guild.members[member_id].name] if member_id in guild.members else []
    attendance = AttendanceService(services.db, services.store, services.scheduler, services.reservations,
                                   StatsReader(services.supervisor), links, events, channels, names)
    events.attendance = attendance
    services.events, services.signups, services.channels, services.attendance = events, signups, channels, attendance
    services.links = links
    services.squadxml = SquadXml(links, services.store, lambda: services.ids, lambda: None, delay=0)
    yield services
    await settle(channels)
    await services.squadxml.settle()


def person(guild, member_id: int, *roles: int) -> FakeMember:
    member = FakeMember(member_id, f"m{member_id}", roles=[FakeRole(r) for r in roles])
    guild.members[member_id] = member
    return member


def asking(stack, guild, who) -> FakeInteraction:
    return FakeInteraction(stack, who, ENGLISH, guild=guild)


async def published(stack, clock, kind="internal", orbat="# Alpha\nLeader\nMedic\nRifleman\n", host=None):
    start = clock.now + timedelta(days=2)
    event = await stack.events.create(Draft(kind, "Operation X", "Coop", Times(start, start + timedelta(minutes=30), 180),
                                            host_group_id=host), MAKER)
    await stack.channels.open_draft(event.id)
    await stack.events.set_briefing(event.id, MAKER, "Night", "Bridge", "")
    if kind != "zeus":
        await stack.signups.set_orbat(event.id, parse(orbat, []))
    if kind == "guest":
        await stack.events.set_how_to_join(event.id, MAKER, "ts.abc.example")
    await stack.events.publish(event.id, MAKER)
    await stack.channels.redrawer.settle()
    return await stack.events.get(event.id)


async def hold(stack, event, *member_ids):
    board = await stack.signups.board(event.id)
    for slot, member_id in zip(board.slots, member_ids):
        await stack.signups.assign(slot.id, member_id)


def at(clock, fake, when):
    clock.now = fake.now = when


def play(id_, start, minutes, end="finished", players=()):
    return {"id": id_, "server_id": 2, "started_at": start, "ended_at": start + timedelta(minutes=minutes) if end else None,
            "end": end, "players": [{"uid": uid, "name": name, "seconds": seconds} for uid, name, seconds in players]}


async def run(stack):
    for _ in range(3):  # (an action may schedule the next one, due at once)
        await stack.scheduler.run_due()
    await settle(stack.channels)


async def notes(stack) -> list[str]:
    await asyncio.sleep(0)
    return stack.staff.drain()


# ---------------------------------------------------------------------------- the rules

def test_a_finished_play_ends_the_event_only_after_the_share_and_the_settle_delay(clock):
    start = clock.now
    early = Play(1, start, start + timedelta(minutes=60), "finished")
    earliest, settle_ = start + timedelta(minutes=108), timedelta(minutes=10)
    assert finished_at([early], start + timedelta(minutes=70), settle_, earliest) is None  # remembered ...
    assert finished_at([early], earliest, settle_, earliest) == earliest  # ... and counts once the point passes
    later = Play(2, start + timedelta(minutes=65), None, None)  # a new play since: forgotten
    assert finished_at([early, later], earliest, settle_, earliest) is None
    crashed = Play(3, start + timedelta(minutes=65), start + timedelta(minutes=90), "crashed")
    assert finished_at([early, crashed], earliest, settle_, earliest) is None  # a crash after it doesn't end it
    last = Play(4, start + timedelta(minutes=100), start + timedelta(minutes=120), "finished")
    assert finished_at([early, crashed, last], start + timedelta(minutes=125), settle_, earliest) is None
    assert finished_at([early, crashed, last], start + timedelta(minutes=130), settle_, earliest) is not None


def test_the_report_sorts_holders_walk_ins_strangers_and_probable_links():
    players = [Player(ANNA, "Anna", 3600), Player(MATCH, "Kowal", 2400), Player(BOB, "Bob", 2000),
               Player(STRANGER, "Stranger", 1900), Player(SHORT, "Short", 60), Player(None, "OcapOnly", 2000)]
    names = {2: ["Kowal", "kowal_dc"], 4: ["Nobody"]}
    lines, guests = compute([1, 2, 4, 5], {1: ANNA, 3: BOB, 5: "76561198000000099"}, players, 1800, False,
                            lambda m: names.get(m, []))
    by = {(line.member_id, line.name): line for line in lines}
    assert by[(1, "Anna")].status == "attended" and by[(1, "Anna")].holder
    assert by[(2, "")].status == "not_linked" and by[(2, "")].probable_steam_id == MATCH
    assert by[(4, "")].probable_steam_id is None
    assert by[(5, "")].status == "no_show"
    assert by[(3, "Bob")].status == "walk_in" and not by[(3, "Bob")].holder
    assert by[(None, "Stranger")].status == "walk_in" and by[(None, "OcapOnly")].status == "walk_in"
    assert not any(line.name == "Short" for line in lines) and guests == 0
    lines, guests = compute([1], {1: ANNA, 3: BOB}, players, 1800, True, lambda m: [])
    assert guests == 3 and {line.status for line in lines} == {"attended", "walk_in"}  # our walk-in by name


async def test_the_stats_come_from_the_window_or_the_plays(services, fake, clock):
    start = clock.now
    fake.plays = [play(1, start - timedelta(hours=5), 60, players=[(ANNA, "Anna", 999)]),  # before the window
                  play(2, start + timedelta(minutes=10), 60, players=[(ANNA, "Anna", 1000), (BOB, "Bob", 500)]),
                  play(3, start + timedelta(minutes=80), 30, "crashed", players=[(ANNA, "Anna", 200)])]
    reader = StatsReader(services.supervisor)
    window = await reader.read("event", start, start + timedelta(hours=3))
    assert [p.id for p in window.plays] == [2, 3] and {p.uid: p.seconds for p in window.players} == {ANNA: 1200, BOB: 500}
    fake.capabilities.remove("attendance_window")
    await services.supervisor.refresh_info()
    fallback = await reader.read("event", start, start + timedelta(hours=3))
    assert [p.id for p in fallback.plays] == [2, 3] and {p.uid: p.seconds for p in fallback.players} == {ANNA: 1200, BOB: 500}
    assert ("GET", "/api/stats/missions/2") in fake.calls


# ---------------------------------------------------------------------------- the whole way

async def test_a_finished_mission_ends_the_event_and_the_report_follows(stack, guild, clock, fake):
    for member_id in (1, 2, 3):
        person(guild, member_id, MEMBER)
    event = await published(stack, clock)
    await hold(stack, event, 1, 2)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    await stack.links.link(3, BOB, "Bob", 3, has_rank=True)
    start = event.start_at
    fake.plays = [play(1, start + timedelta(minutes=5), 100, players=[
        (ANNA, "Anna", 5000), (MATCH, "m2", 4000), (BOB, "Bob", 3000), (STRANGER, "Stranger", 2000),
        (SHORT, "Short", 100)])]
    at(clock, fake, start + timedelta(minutes=100))  # the play is still going (for the bot: not finished yet)
    fake.plays[0]["ended_at"], fake.plays[0]["end"] = None, None
    await run(stack)
    assert (await stack.events.get(event.id)).status == "published"
    fake.plays[0]["ended_at"], fake.plays[0]["end"] = start + timedelta(minutes=105), "finished"
    at(clock, fake, start + timedelta(minutes=110))  # after the share (108 min), but inside the settle delay
    await run(stack)
    assert (await stack.events.get(event.id)).status == "published"
    at(clock, fake, start + timedelta(minutes=116))
    await run(stack)
    ended = await stack.events.get(event.id)
    assert (ended.status, ended.attendance_trigger, ended.attendance_state) == ("ended", "finished", "reported")
    assert all(r["ended"] for r in fake.reservations.values())  # the reservation was ended early
    entries = {(e.member_id, e.name): e.status for e in await stack.attendance.entries(event.id)}
    assert entries == {(1, "Anna"): "attended", (2, ""): "not_linked", (3, "Bob"): "walk_in",
                       (None, "Stranger"): "walk_in"}
    channel = guild.get_channel(event.channel_id)
    report = channel.messages[-1]
    fields = {f.name: f.value for f in report.kwargs["embeds"][0].fields}
    assert fields["Obecni (1)"] == "<@1>" and "prawdopodobnie m2" in fields["Bez linku Steam (1)"]
    assert report.kwargs["view"].children[0].custom_id == f"attendance:{event.id}:edit"
    assert any("obecni 1, nieobecni 0, bez linku 1, bez slotu 2" in n for n in await notes(stack))
    assert await stack.scheduler.exists(f"{ARCHIVE_KIND}:{event.id}")


async def test_a_zeus_event_ends_with_its_reservation_and_counts_those_coming(stack, guild, clock, fake):
    event = await published(stack, clock, kind="zeus")
    for member_id in (1, 2):
        person(guild, member_id, MEMBER)
        await stack.signups.coming(event.id, Member(member_id, frozenset({MEMBER})))
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    await stack.links.link(2, BOB, "Bob", 2, has_rank=True)
    start = event.start_at
    fake.plays = [play(1, start + timedelta(minutes=5), 30, players=[(ANNA, "Anna", 1800)])]  # finished: ignored
    at(clock, fake, start + timedelta(minutes=120))
    await run(stack)
    assert (await stack.events.get(event.id)).status == "published"
    booked = await stack.reservations.get(event.id)
    at(clock, fake, booked.ends_at + timedelta(minutes=1))
    await run(stack)
    ended = await stack.events.get(event.id)
    assert (ended.status, ended.attendance_trigger, ended.attendance_state) == ("ended", "reservation", "waiting")
    at(clock, fake, booked.ends_at + timedelta(minutes=10))
    await run(stack)
    entries = {e.member_id: e.status for e in await stack.attendance.entries(event.id)}
    assert entries == {1: "attended", 2: "no_show"}


async def test_unreadable_stats_are_retried_until_the_deadline(stack, guild, clock, fake):
    event = await published(stack, clock)
    booked = await stack.reservations.get(event.id)
    at(clock, fake, booked.ends_at + timedelta(minutes=11))
    stack.supervisor.url = "http://127.0.0.1:1"  # gone
    await run(stack)
    ended = await stack.events.get(event.id)
    assert ended.attendance_state == "waiting" and await stack.scheduler.exists(f"{READ}:{event.id}")
    at(clock, fake, booked.ends_at + timedelta(hours=25))
    await run(stack)
    assert (await stack.events.get(event.id)).attendance_state == "unavailable"
    report = guild.get_channel(event.channel_id).messages[-1]
    assert "Nie udało się odczytać statystyk" in report.kwargs["embeds"][0].description


async def test_end_event_in_the_panel_reads_the_stats_after_the_settle_delay(stack, guild, clock, fake):
    event = await published(stack, clock)
    at(clock, fake, event.start_at + timedelta(minutes=30))
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    pressed = asking(stack, guild, maker)
    await PanelAction(event.id, "end").callback(pressed)
    assert pressed.replies[0].content == "The event has ended."
    ended = await stack.events.get(event.id)
    assert (ended.status, ended.attendance_trigger) == ("ended", "manual")
    assert all(r["ended"] for r in fake.reservations.values())
    await run(stack)
    assert (await stack.events.get(event.id)).attendance_state == "waiting"
    at(clock, fake, clock.now + timedelta(minutes=10))
    await run(stack)
    assert (await stack.events.get(event.id)).attendance_state == "reported"


async def test_a_guest_event_is_confirmed_by_hand_or_after_n_days(stack, guild, clock, fake):
    abc = await Groups(stack.db).add("ABC", "Alpha Bravo", 20)
    event = await published(stack, clock, kind="guest", host=abc.id)
    await hold(stack, event, 1, 2)
    end = event.start_at + timedelta(minutes=180 + 30)
    at(clock, fake, end)
    await run(stack)
    assert {e.member_id: e.status for e in await stack.attendance.entries(event.id)} == {1: "to_confirm", 2: "to_confirm"}
    assert not fake.calls or ("GET", "/api/stats/attendance") not in fake.calls  # never the supervisor
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    await stack.attendance.correct(event.id, Member(20, frozenset({MEMBER, MISSION_MAKER})), [1], "attended")
    at(clock, fake, end + timedelta(hours=25))
    await run(stack)
    thread = guild.get_thread(event.thread_id)
    assert "czeka na potwierdzenie" in thread.sent[-1].content and "<@20>" in thread.sent[-1].content
    at(clock, fake, end + timedelta(days=3, minutes=1))
    await run(stack)
    assert {e.member_id: e.status for e in await stack.attendance.entries(event.id)} == {1: "attended", 2: "attended"}
    assert any("1 osoba do potwierdzenia liczy się teraz jako obecna" in n for n in await notes(stack))
    assert maker  # (the organiser who confirmed)


# ---------------------------------------------------------------------------- corrections

async def test_organisers_correct_the_report_and_confirm_a_probable_link(stack, guild, clock, fake):
    for member_id in (1, 2):
        person(guild, member_id, MEMBER)
    event = await published(stack, clock)
    await hold(stack, event, 1, 2)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    start = event.start_at
    fake.plays = [play(1, start + timedelta(minutes=5), 120, players=[(ANNA, "Anna", 600), (MATCH, "m2", 4000)])]
    at(clock, fake, start + timedelta(minutes=140))
    await run(stack)
    stranger = asking(stack, guild, person(guild, 7, MEMBER))
    await ReportAction(event.id, "edit").callback(stranger)
    assert stranger.replies[0].content == "Only the event's organisers edit the attendance."
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    opened = asking(stack, guild, maker)
    await ReportAction(event.id, "edit").callback(opened)
    editor: Editor = opened.replies[0].kwargs["view"]
    early = asking(stack, guild, maker)
    await editor._members(early, [1])
    assert early.replies[0].content == "Pick the status first."
    await editor._status(asking(stack, guild, maker), "excused")
    done = asking(stack, guild, maker)
    await editor._members(done, [1, 9])  # 9 wasn't in the report: added
    assert done.replies[0].content == "2 members are now Excused."
    entries = {e.member_id: e for e in await stack.attendance.entries(event.id)}
    assert entries[1].status == "excused" and entries[1].corrected_by == 20 and entries[9].status == "excused"
    (probable,) = await stack.attendance.probable(event.id)
    confirm = asking(stack, guild, maker)
    await editor._confirm(confirm, [probable.id])
    assert confirm.replies[0].content == "Linked: <@2> is m2."
    assert (await stack.links.get(2)).steam_id == MATCH
    assert {e.member_id: e.status for e in await stack.attendance.entries(event.id)}[2] == "attended"
