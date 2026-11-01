"""The event channel (docs/PLAN.md, *Event channel*, *Draft and publish*, *Cancelling*), on the Discord fakes."""

import asyncio
from datetime import timedelta

import discord
import pytest
from conftest import ADMIN, ANNOUNCE, ANNOUNCEMENT, DRAFTS, EVENTS, MEMBER, MISSION_MAKER, MODERATOR, settle
from fakes import FakeChannel

from app.db.models import Event, Squad, Slot
from app.eventchannel import render, sync
from app.eventchannel.service import EventChannels, Redrawer, mention_chunks
from app.events.service import Draft, EventService
from app.events.times import Times
from app.orbat.parser import parse
from app.signup.service import Board, Member, SignupService

MAKER = Member(20, frozenset({MEMBER, MISSION_MAKER}))


# ------------------------------------------------------------------ keeping the order

def ops(stored, wanted):
    return [(op.action, op.key) for op in sync.plan(dict.fromkeys(stored, 1), wanted)]


def test_new_continuations_go_at_the_end_and_old_ones_go_from_it():
    assert ops([], ["info", "briefing", "orbat:0"]) == [("send", "info"), ("send", "briefing"), ("send", "orbat:0")]
    assert ops(["info", "briefing", "orbat:0"], ["info", "briefing", "orbat:0", "orbat:1"]) == [
        ("edit", "info"), ("edit", "briefing"), ("edit", "orbat:0"), ("send", "orbat:1")]
    assert ops(["info", "briefing", "orbat:0", "orbat:1"], ["info", "briefing", "orbat:0"]) == [
        ("delete", "orbat:1"), ("edit", "info"), ("edit", "briefing"), ("edit", "orbat:0")]


def test_a_late_preset_reposts_the_orbat_after_it():
    assert ops(["info", "briefing", "orbat:0", "orbat:1"], ["info", "briefing", "preset", "orbat:0", "orbat:1"]) == [
        ("delete", "orbat:0"), ("delete", "orbat:1"), ("edit", "info"), ("edit", "briefing"),
        ("send", "preset"), ("send", "orbat:0"), ("send", "orbat:1")]


async def test_apply_keeps_the_order_even_when_a_message_was_deleted_by_hand():
    from fakes import FakeGuild
    channel = FakeChannel(1, "18-11-ibc")
    FakeGuild(9, channels=[channel])
    pages = {k: render.Message(content=k) for k in ("info", "briefing", "orbat:0", "orbat:1")}
    stored = await sync.apply(channel, {}, pages)
    assert [m.content for m in channel.messages] == ["info", "briefing", "orbat:0", "orbat:1"]
    await channel.get_partial_message(stored["briefing"]).delete()  # a moderator deleted it
    stored = await sync.apply(channel, stored, pages)
    assert [m.content for m in channel.messages] == ["info", "briefing", "orbat:0", "orbat:1"]
    with_preset = {"info": pages["info"], "briefing": pages["briefing"], "preset": render.Message(content="preset"),
                   "orbat:0": pages["orbat:0"], "orbat:1": pages["orbat:1"]}
    await sync.apply(channel, stored, with_preset)
    assert [m.content for m in channel.messages] == ["info", "briefing", "preset", "orbat:0", "orbat:1"]


# ------------------------------------------------------------------ drawing

def test_the_orbat_splits_before_discords_limits():
    squads = [(Squad(id=i, event_id=1, position=i, name=f"Squad {i}"),
               [Slot(id=i * 100 + j, event_id=1, squad_id=i, position=j, name="Rifleman", holder_id=None, locked=False,
                     required_role_id=None) for j in range(8)]) for i in range(14)]
    pages = render.orbat(Board(squads=squads), "en")
    assert len(pages) == 2 and len(pages[0].embeds) == 10
    assert all(len(p.embeds) <= 10 and p.size() <= 6000 for p in pages)
    big = [(Squad(id=1, event_id=1, position=0, name="Big"),
            [Slot(id=j, event_id=1, squad_id=1, position=j, name="R" * 90, holder_id=10 ** 17 + j, locked=False,
                  required_role_id=None) for j in range(40)]) for _ in range(3)]
    assert all(p.size() <= 6000 for p in render.orbat(Board(squads=big), "en"))


def test_mentions_split_into_messages_discord_takes():
    chunks = mention_chunks("Cancelled.", [10 ** 17 + n for n in range(150)])
    assert len(chunks) == 2 and all(len(c) <= 2000 for c in chunks) and chunks[0].startswith("Cancelled.")
    assert sum(c.count("<@") for c in chunks) == 150


async def test_redraws_are_batched():
    drawn = []

    async def draw(event_id):
        drawn.append(event_id)

    redrawer = Redrawer(draw, 0.01)
    for _ in range(50):
        redrawer.request(1)
    redrawer.request(2)
    await redrawer.settle()
    assert sorted(drawn) == [1, 2]


# ------------------------------------------------------------------ the channel's life

@pytest.fixture
async def world(services, fake, guild, clock):
    channels = EventChannels(services.db, services.store, lambda: services.ids, services.scheduler, lambda: guild,
                             interval=0)
    events = EventService(services.db, services.store, lambda: services.ids, services.scheduler,
                          services.reservations, channels, link=channels.link)
    signups = SignupService(services.db, services.store, lambda: services.ids, services.scheduler, channels,
                            lambda _m: frozenset({MEMBER}))
    channels.events, channels.signups = events, signups
    guild.get_channel(EVENTS).overwrites = {guild.default_role: discord.PermissionOverwrite(send_messages=False)}
    yield channels, events, signups
    await settle(channels)


async def make(world, clock, days=5):
    channels, events, signups = world
    start = clock.now + timedelta(days=days)
    event = await events.create(Draft("internal", "Operation X", "Coop", Times(start, start + timedelta(minutes=30), 180)),
                                MAKER)
    channel = await channels.open_draft(event.id)
    return event, channel


async def ready_and_publish(world, event):
    channels, events, signups = world
    await events.set_briefing(event.id, MAKER, "Night raid", "Take the bridge", "")
    await signups.set_orbat(event.id, parse("# Alpha\nLeader\nRifleman x2\n", []))
    await events.publish(event.id, MAKER)
    await channels.redrawer.settle()


async def test_a_draft_gets_a_hidden_channel_with_its_thread_and_messages(world, clock, guild, fake):
    event, channel = await make(world, clock)
    assert channel.category_id == DRAFTS and channel.name == f"{event.assembly_at.astimezone(world[0].store.current.tz):%d-%m}-ibc"
    assert channel.overwrites[guild.default_role].view_channel is False
    assert discord.Object(id=MAKER.id) in channel.overwrites  # the creator
    assert discord.Object(id=ADMIN) in channel.overwrites and discord.Object(id=MODERATOR) in channel.overwrites
    thread = next(iter(guild.threads.values()))
    assert thread.parent_id == channel.id
    titles = [m.embeds[0].title for m in channel.messages]
    assert titles == ["SZKIC · Operation X", "Briefing", "ORBAT"]
    stored = (await world[1].get(event.id)).messages
    assert list(stored) == ["info", "briefing", "orbat:0"]
    (reservation,) = fake.reservations.values()
    assert reservation["link"] == f"https://discord.com/channels/{guild.id}/{channel.id}"


async def test_publishing_moves_the_channel_and_announces(world, clock, guild):
    event, channel = await make(world, clock)
    await ready_and_publish(world, event)
    assert channel.category_id == EVENTS
    assert channel.overwrites[guild.default_role].send_messages is False  # the category's: read-only
    assert discord.Object(id=MAKER.id) in channel.overwrites  # the organisers still post
    (notice,) = guild.get_channel(ANNOUNCE).messages
    assert notice.content == f"<@&{ANNOUNCEMENT}>"
    assert [r.id for r in notice.kwargs["allowed_mentions"].roles] == [ANNOUNCEMENT]
    assert f"<#{channel.id}>" in notice.embeds[0].fields[0].value
    assert await world[0].scheduler.exists(f"archive:{event.id}")


async def test_sign_ups_are_edited_in_place_and_continuations_appended(world, clock):
    channels, events, signups = world
    event, channel = await make(world, clock)
    await ready_and_publish(world, event)
    before = len(channel.messages)
    slot = (await signups.board(event.id)).slots[0]
    await signups.take(event.id, slot.id, Member(30, frozenset({MEMBER})))
    await channels.redrawer.settle()
    assert len(channel.messages) == before
    assert "<@30>" in channel.messages[-1].embeds[0].description
    big = "".join(f"# Squad {i}\nRifleman x3\n" for i in range(12))
    await signups.set_orbat(event.id, parse(big, []))
    await channels.redrawer.settle()
    assert [m.embeds[0].title.split(" (")[0] for m in channel.messages][:3] == ["Operation X", "Briefing", "Squad 0"]
    assert len(channel.messages) == 4  # info, briefing, two ORBAT pages
    await signups.set_orbat(event.id, parse("# Alpha\nLeader\n", []))
    await channels.redrawer.settle()
    assert len(channel.messages) == 3


async def test_a_new_date_renames_the_channel(world, clock):
    channels, events, _ = world
    event, channel = await make(world, clock)
    later = clock.now + timedelta(days=9)
    await events.update(event.id, MAKER, times=Times(later, later + timedelta(minutes=30), 180))
    await channels.redrawer.settle()
    await asyncio.gather(*channels._renames.values())
    assert channel.name.startswith(f"{later.astimezone(channels.store.current.tz):%d-%m}")


async def test_cancelling_pings_the_thread_and_archives_by_quarter(world, clock, guild):
    channels, events, signups = world
    event, channel = await make(world, clock)
    await ready_and_publish(world, event)
    slot = (await signups.board(event.id)).slots[0]
    await signups.take(event.id, slot.id, Member(30, frozenset({MEMBER})))
    await events.cancel(event.id, MAKER, "the server burned")
    thread = guild.threads[(await events.get(event.id)).thread_id]
    assert "Operation X" in thread.sent[-1].content and "<@30>" in thread.sent[-1].content
    assert thread.archived and thread.locked
    local = event.start_at.astimezone(channels.store.current.tz)
    archive = guild.get_channel(channel.category_id)
    assert archive.name == f"Archiwum Q{(local.month - 1) // 3 + 1} {local.year}"
    assert archive.overwrites[guild.default_role].send_messages is False
    assert channel.messages[0].embeds[0].title.startswith("ODWOŁANY")
    notice = guild.get_channel(ANNOUNCE).messages[0]
    assert notice.embeds[0].title.startswith("ODWOŁANY")


async def test_a_cancelled_event_loses_its_buttons(world, clock):
    channels, events, _ = world
    event, channel = await make(world, clock)
    await ready_and_publish(world, event)
    await events.cancel(event.id, MAKER, "x")
    info, orbat = channel.messages[0], channel.messages[-1]
    assert info.edits[-1]["view"] is None and orbat.edits[-1]["view"] is None  # None removes them on Discord


async def test_the_archive_and_the_closing_follow_new_times(world, clock, services):
    channels, events, signups = world
    event, _ = await make(world, clock)
    await ready_and_publish(world, event)
    later = event.assembly_at + timedelta(days=10)
    await events.update(event.id, MAKER, times=Times(later, later + timedelta(minutes=30), 180))
    clock.now = event.start_at + timedelta(minutes=180, hours=48)  # when it would have been archived
    await services.scheduler.run_due()
    assert (await events.get(event.id)).archived_at is None
    clock.now = later + timedelta(minutes=30 + 180) + timedelta(hours=48)
    await services.scheduler.run_due()
    assert (await events.get(event.id)).archived_at is not None


async def test_sign_up_buttons_follow_the_sign_up_state(world, clock, services):
    channels, events, signups = world
    event, channel = await make(world, clock)
    await events.set_signup_start(event.id, MAKER, clock.now + timedelta(days=1))
    await ready_and_publish(world, event)
    orbat = channel.messages[-1]

    def disabled():
        view = orbat.edits[-1]["view"] if orbat.edits else orbat.kwargs["view"]
        return [b.custom_id.split(":")[-1] for b in view.children if b.item.disabled]

    assert disabled() == ["take", "reserve"]  # Maybe, Can't come and Leave work before it opens
    clock.now += timedelta(days=1)
    await services.scheduler.run_due()
    await channels.redrawer.settle()
    assert disabled() == []
    clock.now = (await events.get(event.id)).start_at
    await services.scheduler.run_due()  # sign-up closes at the start: drawn again
    await channels.redrawer.settle()
    assert disabled() == ["take", "reserve", "maybe", "cant", "leave"]


async def test_a_full_zeus_event_turns_coming_off(world, clock):
    channels, events, signups = world
    start = clock.now + timedelta(days=5)
    event = await events.create(Draft("zeus", "Zeus night", "Zeus", Times(start, start + timedelta(minutes=30), 180)),
                                MAKER)
    channel = await channels.open_draft(event.id)
    await events.set_limit(event.id, MAKER, 1)
    await events.set_briefing(event.id, MAKER, "x", "", "")
    await events.publish(event.id, MAKER)
    await signups.coming(event.id, Member(30, frozenset({MEMBER})))
    await channels.redrawer.settle()
    view = channel.messages[-1].edits[-1]["view"]
    assert [b.custom_id.split(":")[-1] for b in view.children if b.item.disabled] == ["coming"]


async def test_a_members_zeus_server_is_confirmed_only_by_a_moderator(world, clock):
    channels, events, _ = world
    member = Member(10, frozenset({MEMBER}))
    start = clock.now + timedelta(days=5)
    event = await events.create(Draft("zeus", "Zeus night", "Zeus", Times(start, start + timedelta(minutes=30), 180)),
                                member)
    channel = await channels.open_draft(event.id)
    await events.set_briefing(event.id, member, "x", "", "")
    await events.publish(event.id, member)

    async def server() -> bool:
        await channels.redrawer.settle()
        return "Jeszcze niepotwierdzony" in [f.value for f in channel.messages[0].embeds[0].fields]

    assert await server()
    request = await events.request_server(event.id, member)
    assert await server()  # the host is held, but nobody approved it yet
    await events.approve(request.id, Member(40, frozenset({MODERATOR})))
    assert not await server()


async def test_a_co_organiser_added_later_sees_the_draft(world, clock):
    channels, events, _ = world
    event, channel = await make(world, clock)
    await events.add_organiser(event.id, MAKER, 77)
    assert channel.overwrites[discord.Object(id=77)].view_channel is True
    await events.remove_organiser(event.id, MAKER, 77)
    assert discord.Object(id=77) not in channel.overwrites


async def test_crowded_categories_warn_staff_and_a_full_one_is_named(world, clock, guild, services):
    channels, events, _ = world
    channels.staff = services.staff
    drafts = guild.get_channel(DRAFTS)
    for n in range(44):
        await guild.create_text_channel(f"old-{n}", category=drafts)
    services.staff.drain()
    await make(world, clock)
    await asyncio.sleep(0)  # (posting is queued for the next loop round)
    assert any("45 z 50" in note for note in services.staff.drain())
    assert channels.full() is None
    for n in range(5):
        await guild.create_text_channel(f"more-{n}", category=drafts)
    assert channels.full() == "Szkice" and channels.full("events_category") is None


async def test_a_cancelled_draft_loses_its_channel(world, clock, guild):
    _, events, _ = world
    event, channel = await make(world, clock)
    await events.cancel(event.id, MAKER, "no")
    assert guild.get_channel(channel.id) is None


async def test_a_full_quarter_overflows_into_a_second_category(world, clock, guild, services):
    channels, events, _ = world
    event, channel = await make(world, clock)
    local = event.start_at.astimezone(channels.store.current.tz)
    full = await guild.create_category(f"Archiwum Q{(local.month - 1) // 3 + 1} {local.year}")
    for n in range(50):
        await guild.create_text_channel(f"old-{n}", category=full)
    await ready_and_publish(world, event)
    clock.now = event.start_at + timedelta(minutes=180) + timedelta(hours=48)
    await services.scheduler.run_due()
    assert guild.get_channel(channel.category_id).name.endswith("(2)")


async def test_after_a_restart_every_open_channel_is_redrawn(world, clock):
    channels, events, _ = world
    event, channel = await make(world, clock)
    async with channels.db.session() as session, session.begin():
        (await session.get(Event, event.id)).title = "Renamed while down"
    await channels.refresh_all()
    await channels.redrawer.settle()
    assert channel.messages[0].embeds[0].title.endswith("Renamed while down")
