"""The event channel (docs/PLAN.md, *Event channel*, *Draft and publish*, *Cancelling*), on the Discord fakes."""

import asyncio
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

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


async def test_discords_thread_card_stands_for_the_link_until_something_goes_after_it():
    from fakes import FakeGuild
    channel = FakeChannel(1, "18-11-ibc")
    FakeGuild(9, channels=[channel])
    pages = {k: render.Message(content=k) for k in ("info", "orbat:0")}
    stored = await sync.apply(channel, {}, pages)
    await channel.create_thread(name="Dyskusja", type=discord.ChannelType.public_thread, auto_archive_duration=10080)
    stored[sync.CARD] = channel.messages[-1].id
    with_link = pages | {"discussion": render.Message(content="link")}
    assert await sync.apply(channel, stored, with_link) == stored  # edited in place, the card kept
    assert [m.content for m in channel.messages] == ["info", "orbat:0", None]
    stored = await sync.apply(channel, stored, with_link | {"orbat:1": render.Message(content="orbat:1")})
    assert [m.content for m in channel.messages] == ["info", "orbat:0", "orbat:1", "link"]  # the card is gone
    assert sync.CARD not in stored


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


async def test_a_failure_part_way_keeps_what_was_sent_so_nothing_is_sent_twice():
    """Discord refusing the third message used to lose the ids of the first two: the next redraw sent them again."""
    from fakes import FakeGuild
    channel = FakeChannel(1, "18-11-ibc")
    FakeGuild(9, channels=[channel])
    pages = {k: render.Message(content=k) for k in ("info", "briefing", "orbat:0")}
    send, calls = channel.send, []

    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise discord.HTTPException(SimpleNamespace(status=429, reason="Too Many Requests"), "rate limited")
        return await send(*args, **kwargs)

    channel.send = flaky
    with pytest.raises(sync.Partial) as caught:
        await sync.apply(channel, {}, pages)
    assert set(caught.value.stored) == {"info", "briefing"}
    assert isinstance(caught.value.__cause__, discord.HTTPException)
    await sync.apply(channel, caught.value.stored, pages)
    assert [m.content for m in channel.messages] == ["info", "briefing", "orbat:0"]  # no second info or briefing


# ------------------------------------------------------------------ drawing

EVENT = SimpleNamespace(title="Op", status="published", signup_opens_at=None,
                        start_at=datetime(2026, 11, 20, 18, 30, tzinfo=UTC))
CTX = render.Context("en", datetime(2026, 11, 10, tzinfo=UTC))


def test_the_orbat_is_text_split_between_squads_before_discords_limit():
    squads = [(Squad(id=i, event_id=1, position=i, name=f"Squad {i}"),
               [Slot(id=i * 100 + j, event_id=1, squad_id=i, position=j, name="Rifleman", holder_id=None, locked=False,
                     required_role_id=None) for j in range(8)]) for i in range(30)]
    pages = render.orbat(EVENT, Board(squads=squads), CTX)
    assert len(pages) > 1 and all(len(p.content) <= 2000 and not p.embeds for p in pages)
    assert pages[0].content.startswith(f"{render.SPACER}## Sign-up: Op\nStart: ")  # an empty line above
    assert "\n\n**Squad 0**\n`1.` Rifleman — *free*\n`2.` Rifleman" in pages[0].content
    assert all(p.content.startswith("**Squad") for p in pages[1:])  # a squad that fits isn't cut
    assert sum(p.content.count("**Squad") for p in pages) == 30
    assert pages[-1].content.endswith("Taking another slot moves your sign-up.")  # the hint, by the slot lists
    big = [(Squad(id=1, event_id=1, position=0, name="Big"),
            [Slot(id=j, event_id=1, squad_id=1, position=j, name="R" * 90, holder_id=10 ** 17 + j, locked=False,
                  required_role_id=None) for j in range(40)]) for _ in range(3)]
    assert all(len(p.content) <= 2000 for p in render.orbat(EVENT, Board(squads=big), CTX))


def test_slots_show_their_state_in_words_and_the_zeus_list_one_member_per_line():
    from app.db.models import Signup

    squad = Squad(id=1, event_id=1, position=0, name="1st *Squad*")
    slots = [Slot(id=1, event_id=1, squad_id=1, position=0, name="SL", holder_id=5, locked=True, required_role_id=7),
             Slot(id=2, event_id=1, squad_id=1, position=1, name="Zeus", holder_id=None, locked=True, required_role_id=None),
             Slot(id=3, event_id=1, squad_id=1, position=2, name="Medic", holder_id=None, locked=False, required_role_id=None)]
    board = Board(squads=[(squad, slots)], offered={3}, signups=[Signup(event_id=1, member_id=9, status="maybe")])
    polish = render.Context("pl", CTX.now)
    (page,) = render.orbat(EVENT, board, polish)
    head, squad_block, maybe, hint = page.content.removeprefix(render.SPACER).split("\n\n")
    assert hint.startswith("-# Wybierz drużynę z listy poniżej")
    assert head.split("\n")[1].endswith("• Zajęte sloty: **1/3** • \U0001F512 1")
    assert head.split("\n")[2].startswith("\U0001F7E2 **Zapisy otwarte** – zamknięcie <t:")
    assert squad_block.split("\n") == ["**1st \\*Squad\\***", "`1.` SL (<@&7>) — <@5> \U0001F512",
                                        "`2.` Zeus — \U0001F512 *zamknięty*", "`3.` Medic — *zaoferowany*"]
    assert maybe.startswith("**Może** (1)\n<@9>")
    zeus = Board(signups=[Signup(event_id=1, member_id=n, status="coming") for n in (1, 2)])
    (listed,) = render.zeus_list(SimpleNamespace(**vars(EVENT), signup_limit=20), zeus, polish)
    assert listed.content.removeprefix(render.SPACER).split("\n\n")[1:] == ["**Będę** (2/20)\n<@1>\n<@2>", "**Nie będę** (0)\n—"]
    assert "• Będę: **2/20**" in listed.content


def test_a_long_briefing_continues_in_further_messages():
    event = SimpleNamespace(briefing_situation="S" * 1500, briefing_mission="M" * 1500, briefing_notes="x\n" * 400,
                            briefing_image=None)
    pages = render.briefing(event, "en")
    assert len(pages) == 3 and all(len(p.content) <= 2000 for p in pages)
    assert pages[0].content.startswith(f"{render.SPACER}## Briefing\n\n**Situation**") and pages[1].content.startswith("**Mission**")
    assert render.texts(["y" * 4500]) == ["y" * 2000, "y" * 2000, "y" * 500]  # one line too long: cut


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
    event = await events.create(Draft("internal", "Operation X", Times(start, start + timedelta(minutes=30), 180)),
                                MAKER)
    channel = await channels.open_draft(event.id)
    return event, channel


async def ready_and_publish(world, event):
    channels, events, signups = world
    await events.set_briefing(event.id, MAKER, "Night raid", "Take the bridge", "")
    await signups.set_orbat(event.id, parse("# Alpha\nLeader\nRifleman x2\n", []))
    await events.publish(event.id, MAKER)
    await channels.redrawer.settle()


async def test_a_draft_gets_its_channel_in_the_events_category_marked_draft(world, clock, guild, fake):
    event, channel = await make(world, clock)
    assert channel.category_id == EVENTS and channel.name == f"{event.assembly_at.astimezone(world[0].store.current.tz):%d-%m}-ibc"
    assert channel.overwrites[guild.default_role].send_messages is False  # the category's: seen, read-only
    assert channel.overwrites[guild.default_role].view_channel is not False
    assert discord.Object(id=MAKER.id) in channel.overwrites  # the creator
    assert discord.Object(id=ADMIN) in channel.overwrites and discord.Object(id=MODERATOR) in channel.overwrites
    assert not guild.threads  # the thread comes with publishing
    info, briefing, orbat = channel.messages
    assert info.embeds[0].title == "SZKIC · Operation X"
    assert briefing.text.startswith(f"{render.SPACER}## Briefing")
    assert orbat.text.startswith(f"{render.SPACER}## Zapisy: Operation X")
    stored = (await world[1].get(event.id)).messages
    assert list(stored) == ["info", "briefing", "orbat:0"]
    (reservation,) = fake.reservations.values()
    assert reservation["link"] == f"https://discord.com/channels/{guild.id}/{channel.id}"


async def test_a_drafts_pings_go_by_dm_as_it_has_no_thread_yet(world, clock, guild):
    from fakes import FakeMember

    maker = guild.members[MAKER.id] = FakeMember(MAKER.id)
    event, _channel = await make(world, clock)
    await world[0].stale(event.id)
    assert "Operation X" in maker.dms[-1].content


async def test_the_thread_and_the_kinds_are_named_as_configured(world, clock, guild):
    from app.settings import KindNames

    store = world[0].store
    store.current = store.current.model_copy(update={"events": store.current.events.model_copy(update={
        "discussion": "Pytania i ustalenia", "kind_names": KindNames(internal="Misja klanowa")})})
    event, channel = await make(world, clock)
    await ready_and_publish(world, event)
    assert guild.threads[(await world[1].get(event.id)).thread_id].name == "Pytania i ustalenia"
    kind = next(f.value for f in channel.messages[0].embeds[0].fields if f.name == "Rodzaj")
    assert kind == "Misja klanowa" and "Misja klanowa" in channel.topic
    assert render.kind_label("zeus", "pl", store.current.events.kind_names) == "Zeus"  # not set: the bot's own


async def test_publishing_opens_the_thread_and_announces(world, clock, guild):
    event, channel = await make(world, clock)
    await ready_and_publish(world, event)
    assert channel.category_id == EVENTS
    assert channel.overwrites[guild.default_role].send_messages is False  # the category's: read-only
    assert discord.Object(id=MAKER.id) in channel.overwrites  # the organisers still post
    thread = next(iter(guild.threads.values()))
    assert thread.parent_id == channel.id and thread.name == "Dyskusja"
    *_, orbat, card = channel.messages
    assert "Leader" in orbat.text
    assert card.type == discord.MessageType.thread_created and card.kwargs["thread"] == thread.id  # Discord's, last
    assert list((await world[1].get(event.id)).messages) == ["info", "briefing", "orbat:0", "card"]
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
    assert "<@30>" in channel.messages[-2].text
    big = "".join(f"# Squad {i}\nRifleman x5\n" for i in range(25))
    await signups.set_orbat(event.id, parse(big, []))
    await channels.redrawer.settle()
    texts = [m.text for m in channel.messages]
    assert len(texts) == 5 and texts[2].startswith(f"{render.SPACER}## Zapisy") and texts[3].startswith("**Squad")
    assert texts[4].startswith("💬")  # the card was last: now the link is, after the new page
    assert all(m.type == discord.MessageType.default for m in channel.messages)
    await signups.set_orbat(event.id, parse("# Alpha\nLeader\n", []))
    await channels.redrawer.settle()
    assert len(channel.messages) == 4


def test_the_order_puts_the_top_channels_first_and_the_events_by_start():
    from app.eventchannel.order import wanted

    c = {name: SimpleNamespace(id=n, name=name) for n, name in enumerate(["late", "chat", "header", "early", "rules"])}
    day = datetime(2026, 11, 20, tzinfo=UTC)
    starts = {c["late"].id: (day + timedelta(days=2), 1), c["early"].id: (day, 2)}
    order = wanted(list(c.values()), [c["rules"].id, c["header"].id, 99], starts)
    assert [ch.name for ch in order] == ["rules", "header", "chat", "early", "late"]
    same_start = {c["late"].id: (day, 3), c["early"].id: (day, 2)}  # then the older event first
    assert [ch.name for ch in wanted(list(c.values()), [], same_start)] == ["chat", "header", "rules", "early", "late"]


async def test_the_categories_are_kept_in_date_order(world, clock, guild, services):
    import dataclasses
    from conftest import HEADER

    channels, events, _ = world
    rules, chat = FakeChannel(901, "instrukcja-strefy", category_id=EVENTS, position=4), \
        FakeChannel(902, "pogadanki", category_id=EVENTS, position=0)
    guild.add(rules), guild.add(chat)
    guild.get_channel(HEADER).category_id, guild.get_channel(HEADER).position = EVENTS, 2
    services.ids = dataclasses.replace(services.ids, top=(rules.id, HEADER))

    made = {}
    for days in (9, 3, 6):  # created, and published, out of order
        event, channel = await make(world, clock, days=days)
        made[days] = (event, channel)
        await channels.sorter.settle()
        assert [ch.id for ch in guild.get_channel(EVENTS).channels][3:] == \
            [made[d][1].id for d in sorted(made)]  # the drafts by start too, after the top and the rest
    for days in (9, 3, 6):
        await ready_and_publish(world, made[days][0])
        await channels.sorter.settle()
    names = lambda category: [ch.id for ch in guild.get_channel(category).channels]  # noqa: E731
    assert names(EVENTS) == [rules.id, HEADER, chat.id, made[3][1].id, made[6][1].id, made[9][1].id]

    requests = len(guild.reorders)
    await channels.refresh_all()  # already in order: nothing is sent
    await channels.redrawer.settle(), await channels.sorter.settle()
    assert len(guild.reorders) == requests

    later = clock.now + timedelta(days=12)  # the earliest event moves to the end
    await events.update(made[3][0].id, MAKER, times=Times(later, later + timedelta(minutes=30), 180))
    await channels.redrawer.settle(), await channels.sorter.settle()
    assert names(EVENTS)[3:] == [made[6][1].id, made[9][1].id, made[3][1].id]

    made[6][1].position = 40  # a moved channel keeps its number, wherever that puts it
    for days in (9, 6):  # the archive is by date all the same
        await channels.archive(made[days][0].id)
    await channels.sorter.settle()
    archive = guild.get_channel(made[9][1].id).category
    assert [ch.id for ch in archive.channels] == [made[6][1].id, made[9][1].id]


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
    info, orbat = channel.messages[0], channel.messages[-2]
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
    orbat = channel.messages[-2]

    def disabled():
        view = orbat.edits[-1]["view"] if orbat.edits else orbat.kwargs["view"]
        return [b.custom_id.split(":")[-1] for b in view.children if b.item.disabled]

    assert disabled() == ["0", "reserve"]  # the slot list off; Maybe, Can't come and Leave work before it opens
    clock.now += timedelta(days=1)
    await services.scheduler.run_due()
    await channels.redrawer.settle()
    assert disabled() == []
    clock.now = (await events.get(event.id)).start_at
    await services.scheduler.run_due()  # sign-up closes at the start: drawn again
    await channels.redrawer.settle()
    assert disabled() == ["0", "reserve", "maybe", "cant", "leave"]


async def test_a_full_zeus_event_turns_coming_off(world, clock):
    channels, events, signups = world
    start = clock.now + timedelta(days=5)
    event = await events.create(Draft("zeus", "Zeus night", Times(start, start + timedelta(minutes=30), 180)),
                                MAKER)
    channel = await channels.open_draft(event.id)
    await events.set_limit(event.id, MAKER, 1)
    await events.set_briefing(event.id, MAKER, "x", "", "")
    await events.publish(event.id, MAKER)
    await signups.coming(event.id, Member(30, frozenset({MEMBER})))
    await channels.redrawer.settle()
    view = channel.messages[-2].edits[-1]["view"]
    assert [b.custom_id.split(":")[-1] for b in view.children if b.item.disabled] == ["coming"]


async def test_a_members_zeus_server_is_confirmed_only_by_a_moderator(world, clock):
    channels, events, _ = world
    member = Member(10, frozenset({MEMBER}))
    start = clock.now + timedelta(days=5)
    event = await events.create(Draft("zeus", "Zeus night", Times(start, start + timedelta(minutes=30), 180)),
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
    events_category = guild.get_channel(EVENTS)
    for n in range(44):
        await guild.create_text_channel(f"old-{n}", category=events_category)
    services.staff.drain()
    await make(world, clock)
    await asyncio.sleep(0)  # (posting is queued for the next loop round)
    assert any("45 z 50" in note for note in services.staff.drain())
    assert channels.full() is None
    for n in range(5):
        await guild.create_text_channel(f"more-{n}", category=events_category)
    assert channels.full() == "Eventy"


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


async def test_without_the_guild_the_archive_waits_rather_than_passing_for_done(world, clock, services, guild):
    """A scheduled action run while the bot isn't connected must be tried again, not marked done."""
    channels, events, _ = world
    event, channel = await make(world, clock)
    await ready_and_publish(world, event)
    channels.guild = lambda: None
    clock.now = event.start_at + timedelta(minutes=180, hours=48)
    await services.scheduler.run_due()
    assert await services.scheduler.exists(f"archive:{event.id}")
    assert (await events.get(event.id)).archived_at is None
    channels.guild = lambda: guild
    clock.now += timedelta(minutes=5)  # (after the scheduler's first backoff)
    await services.scheduler.run_due()
    assert (await events.get(event.id)).archived_at is not None


async def test_without_the_guild_a_cancelled_draft_keeps_its_channel_id(world, clock, guild):
    channels, events, _ = world
    event, channel = await make(world, clock)
    channels.guild = lambda: None
    with pytest.raises(RuntimeError):
        await channels.delete(event.id)
    assert (await events.get(event.id)).channel_id == channel.id


async def test_a_lost_archive_action_is_scheduled_again_after_a_restart(world, clock, services, guild):
    """Before the fix the archive could be marked done without archiving: the start-up redraw repairs it."""
    channels, events, _ = world
    event, channel = await make(world, clock)
    await ready_and_publish(world, event)
    await services.scheduler.cancel(f"archive:{event.id}")
    clock.now = event.start_at + timedelta(minutes=180, hours=48, seconds=1)
    await channels.refresh_all()
    await channels.redrawer.settle()
    assert await services.scheduler.exists(f"archive:{event.id}")
    await services.scheduler.run_due()
    assert (await events.get(event.id)).archived_at is not None


async def test_an_old_hidden_draft_moves_to_the_events_category_at_start_up(world, clock, guild):
    """Drafts made before 2026-11-11 live in the old hidden drafts category; after the update they are seen."""
    channels, events, _ = world
    event, channel = await make(world, clock)
    channel.category_id = DRAFTS  # as the old version made it
    channel.overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
    await channels.refresh_all()
    await channels.redrawer.settle()
    assert channel.category_id == EVENTS
    assert channel.overwrites[guild.default_role].view_channel is not False
    assert discord.Object(id=MAKER.id) in channel.overwrites


async def test_publishing_that_fails_part_way_is_finished_later_without_doubles(world, clock, guild, services):
    """Discord refusing the announcement used to leave a published event half done for good."""
    channels, events, _ = world
    event, channel = await make(world, clock)
    announcements = guild.get_channel(ANNOUNCE)
    send, calls = announcements.send, []

    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise discord.HTTPException(SimpleNamespace(status=503, reason="Unavailable"), "try later")
        return await send(*args, **kwargs)

    announcements.send = flaky
    await ready_and_publish(world, event)
    assert (await events.get(event.id)).status == "published" and announcements.messages == []
    assert await services.scheduler.exists(f"publish_finish:{event.id}")
    clock.now += timedelta(minutes=2)
    await services.scheduler.run_due()
    await channels.redrawer.settle()
    assert len(announcements.messages) == 1 and len(guild.threads) == 1
    assert (await events.get(event.id)).announcement_id == announcements.messages[0].id
    assert await services.scheduler.exists(f"signup_close:{event.id}")  # (the channel's own actions too)
