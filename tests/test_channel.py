"""The operation channels drawn from the supervisor's change feed (docs/PLAN-front-end.md, *Sync and drawing*), on
the Discord fakes and the fake supervisor: operations change in the app (the fake's ``operation`` / ``change``), the
bot reads the feed and draws."""

from __future__ import annotations

from datetime import timedelta

import discord
import pytest
from conftest import ADMIN, ANNOUNCE, ANNOUNCEMENT, EVENTS, HEADER, MODERATOR, settle
from sqlalchemy import select

from app.channel import render, sync
from app.channel.service import ARCHIVE_KIND, OperationChannels
from app.db.models import OperationChannel, ScheduledAction
from app.operations.feed import CURSOR, Feed
from app.operations.header import Header
from app.db import state

CLIMATE = 401


@pytest.fixture
async def stack(services, guild, clock, fake):
    """The channels and the feed against the fake supervisor; Anna (person 1) is on the roster."""
    services.channels = OperationChannels(services.db, services.store, lambda: services.ids, services.scheduler,
                                          lambda: guild, services.supervisor, interval=0)
    services.channels.header = Header(services.db, services.store, lambda: services.ids, lambda: guild,
                                      services.scheduler.clock, interval=0)
    services.channels.staff = services.staff
    services.feed = Feed(services.db, services.supervisor, services.channels, services.scheduler.clock)
    fake.person(1, "anna")
    fake.staff_roles = {"manage": [str(ADMIN)], "approve": [str(MODERATOR)]}
    yield services
    await settle(services.channels)


async def run(stack) -> None:
    """The feed read, the redraws done, what is due run."""
    await stack.feed.run_once()
    await settle(stack.channels)
    await stack.scheduler.run_due()
    await settle(stack.channels)


async def row(stack, op_id: int) -> OperationChannel:
    return await stack.channels.row(op_id)


def at(clock, fake, when) -> None:
    clock.now = fake.now = when


def texts(channel) -> list[str]:
    return [m.text for m in channel.messages]


async def test_a_draft_made_in_the_app_gets_its_channel_and_its_place_goes_back(stack, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    await run(stack)
    drawn = await row(stack, op["id"])
    channel = guild.get_channel(drawn.channel_id)
    assert channel.name == "05-11-ibc" and channel.category_id == EVENTS
    info = channel.messages[0].embeds[0]
    assert info.title == "SZKIC · Ibis"
    assert texts(channel)[1].endswith("## OPORD\nJeszcze nie napisano.")
    assert fake.operations[op["id"]]["discord"] == {"channel_url": f"https://discord.com/channels/100/{channel.id}"}
    # the organiser and the staff who may manage or approve post there
    posting = {getattr(target, "id", None) for target, rule in channel.overwrites.items() if rule.send_messages}
    assert {1, ADMIN, MODERATOR} <= posting
    sent = len(channel.sent)
    await run(stack)  # the plumbing's echo: drawn again, nothing changes
    assert len(channel.sent) == sent and await state.get(stack.db, CURSOR) == {"after": fake.seq}


async def test_edits_in_the_app_are_drawn_the_opord_a_message_per_paragraph(stack, guild, fake):
    op = fake.operation("Ibis")
    await run(stack)
    long = "\n".join(f"Line {n} " + "x" * 90 for n in range(30))  # over 2000 characters
    fake.change(op["id"], opord={"situation": "Enemy **armour** north.", "execution": long})
    fake.set_file(op["id"], "banner", b"banner-png")
    await run(stack)
    drawn = await row(stack, op["id"])
    channel = guild.get_channel(drawn.channel_id)
    assert list(drawn.messages) == ["info", "situation", "execution", "execution:1"]
    assert texts(channel)[1] == f"{render.SPACER}## Sytuacja\nEnemy **armour** north."
    assert texts(channel)[2].startswith(f"{render.SPACER}## Wykonanie\nLine 0")
    assert all(len(text) <= 2000 for text in texts(channel))
    first = channel.get_partial_message(drawn.messages["situation"])._find()
    assert first.edits[-1]["attachments"][0].filename == "banner.png"  # the banner goes with the first paragraph


async def test_the_preset_is_posted_and_fetched_once_per_content(stack, guild, fake):
    op = fake.operation("Ibis")
    fake.set_file(op["id"], "preset", b"<html>mods</html>", original="ibis.html", preset_name="Ibis mods",
                  mods=[["1", "CBA"], ["2", "ACE"]], cdlc_names=["Global Mobilization"])
    await run(stack)
    drawn = await row(stack, op["id"])
    channel = guild.get_channel(drawn.channel_id)
    preset = channel.get_partial_message(drawn.messages["preset"])._find()
    assert preset.content == "Modset do Arma 3 Launchera: Ibis mods"
    assert preset.kwargs["files"][0].filename == "ibis.html"
    modset = next(f for f in channel.messages[0].embeds[0].fields if f.name == "Modset")
    assert modset.value == "Ibis mods: 2 mody\nCreator DLC: Global Mobilization"
    fetched = [c for c in fake.calls if c[1].endswith("/files/preset.html")]
    fake.change(op["id"], title="Ibis II")
    await run(stack)
    assert [c for c in fake.calls if c[1].endswith("/files/preset.html")] == fetched  # cached on disk


async def test_a_file_that_cant_be_fetched_is_tried_again(stack, guild, fake, clock):
    op = fake.operation("Ibis")
    fake.set_file(op["id"], "map", b"map")
    del fake.files[(op["id"], "map.png")]  # (gone on the supervisor's side for a moment)
    await run(stack)
    async with stack.db.session() as session:
        retry = (await session.execute(select(ScheduledAction).where(ScheduledAction.kind == "draw_retry"))).scalar_one()
    assert retry.operation_id == op["id"] and retry.done_at is None
    fake.files[(op["id"], "map.png")] = b"map"
    at(clock, fake, clock.now + timedelta(minutes=2))
    await run(stack)
    drawn = await row(stack, op["id"])
    assert guild.get_channel(drawn.channel_id).messages[1].kwargs["files"][0].filename == "map.png"


async def test_publishing_makes_the_thread_last_announces_and_pings_at_the_opening(stack, guild, fake, clock):
    opens = clock.now + timedelta(days=1)
    op = fake.operation("Ibis", climate_role_id=str(CLIMATE), signup_opens_at=opens,
                        opord={"situation": "S", "mission": "M"})
    await run(stack)
    fake.change(op["id"], status="published")
    await run(stack)
    drawn = await row(stack, op["id"])
    channel = guild.get_channel(drawn.channel_id)
    assert channel.messages[-1].type == discord.MessageType.thread_created and sync.CARD in drawn.messages
    assert fake.operations[op["id"]]["discord"]["thread_url"].endswith(f"/{drawn.thread_id}")
    (announcement,) = guild.get_channel(ANNOUNCE).messages
    assert announcement.content == f"<@&{ANNOUNCEMENT}> <@&{CLIMATE}>"
    assert announcement.embeds[0].fields[0].value == f"<#{channel.id}>"
    sign_up = next(f for f in channel.messages[0].embeds[0].fields if f.name == "Zapisy")
    assert sign_up.value.startswith("Otwierają się <t:")
    at(clock, fake, opens + timedelta(minutes=5))
    await run(stack)
    (ping,) = guild.threads[drawn.thread_id].sent
    assert ping.content == f"<@&{ANNOUNCEMENT}> <@&{CLIMATE}> Zapisy na Ibis są otwarte!"


async def test_a_sign_up_ping_more_than_an_hour_late_is_skipped(stack, guild, fake, clock):
    opens = clock.now + timedelta(days=1)
    op = fake.operation("Ibis", status="published", signup_opens_at=opens)
    await run(stack)
    at(clock, fake, opens + timedelta(hours=2))  # the bot was down
    await run(stack)
    drawn = await row(stack, op["id"])
    assert guild.threads[drawn.thread_id].sent == []


async def test_a_cancelled_operation_is_archived_at_once_and_its_announcement_edited(stack, guild, fake):
    op = fake.operation("Ibis", status="published")
    await run(stack)
    fake.change(op["id"], status="cancelled", cancel_reason="no Zeus")
    await run(stack)
    drawn = await row(stack, op["id"])
    channel = guild.get_channel(drawn.channel_id)
    assert drawn.archived_at is not None and channel.category.name == "Archiwum Q4 2026"
    assert channel.messages[0].embeds[0].description == "Odwołana: no Zeus"
    assert guild.threads[drawn.thread_id].locked
    (announcement,) = guild.get_channel(ANNOUNCE).messages
    assert announcement.embeds[0].title == "ODWOŁANA · Ibis"


async def test_a_deleted_draft_loses_its_channel(stack, guild, fake):
    op = fake.operation("Ibis")
    await run(stack)
    channel_id = (await row(stack, op["id"])).channel_id
    fake.delete_operation(op["id"])
    await run(stack)
    assert guild.get_channel(channel_id) is None and (await row(stack, op["id"])).gone_at is not None


async def test_an_operation_is_archived_after_its_end(stack, guild, fake, clock):
    op = fake.operation("Ibis", status="published")
    await run(stack)
    drawn = await row(stack, op["id"])
    async with stack.db.session() as session:
        due = (await session.execute(select(ScheduledAction.due_at).where(
            ScheduledAction.key == f"{ARCHIVE_KIND}:{op['id']}"))).scalar_one()
    assert due == drawn.start_at + timedelta(minutes=180) + timedelta(hours=48)
    at(clock, fake, due + timedelta(minutes=1))
    await run(stack)
    assert (await row(stack, op["id"])).archived_at is not None


async def test_a_reset_redraws_and_what_is_missing_is_gone(stack, guild, fake):
    published = fake.operation("Published", status="published")
    draft = fake.operation("Draft")
    kept = fake.operation("Kept")
    await run(stack)
    draft_channel = (await row(stack, draft["id"])).channel_id
    # while the bot is away: two are deleted, and the supervisor prunes their tombstones
    fake.operations.pop(published["id"])  # (a published one never leaves a tombstone: a reset's list misses it)
    fake.delete_operation(draft["id"])
    fake.change(kept["id"], title="Kept II")
    fake.prune(fake.seq)
    await state.put(stack.db, CURSOR, {"after": 1})  # older than what was pruned
    await run(stack)
    assert (await row(stack, published["id"])).archived_at is not None  # it was published: archived
    assert guild.get_channel(draft_channel) is None  # a draft: deleted
    assert (await row(stack, kept["id"])).title == "Kept II"


async def test_the_first_read_leaves_old_operations_alone(stack, guild, fake, clock):
    fake.operation("Long ago", assembly=clock.now - timedelta(days=30), status="ended")
    old = fake.operation("Over", assembly=clock.now - timedelta(days=5), status="published")
    await run(stack)
    assert await row(stack, old["id"]) is None and guild.get_channel(EVENTS).channels == []


async def test_the_header_lists_the_published_operations(stack, guild, fake, clock):
    fake.operation("Ibis", status="published")
    fake.operation("Draft")
    fake.operation("Far", status="published", assembly=clock.now + timedelta(days=30))
    await run(stack)
    (header,) = guild.get_channel(HEADER).messages
    embed = (header.edits[-1] if header.edits else header.kwargs)["embed"]
    calendar = embed.fields[0].value
    assert "**Ibis**" in calendar and "Draft" not in calendar and "Far" not in calendar
    assert embed.title == "Operacje"


async def test_a_restart_redraws_and_keeps_the_overwrites_for_the_meta(stack, guild, fake):
    op = fake.operation("Ibis", author_person_id=1)
    await run(stack)
    channel = guild.get_channel((await row(stack, op["id"])).channel_id)
    stack.channels.meta = {}  # a restart: the feed hasn't read the meta yet
    await stack.channels.refresh_all()
    await settle(stack.channels)
    assert any(getattr(target, "id", None) == ADMIN for target in channel.overwrites)  # not edited without them
    await run(stack)
    assert any(getattr(target, "id", None) == ADMIN for target in channel.overwrites)


async def test_an_operation_live_event_wakes_the_feed(stack, fake):
    from app.live import Live
    from app.supervisor_client import StreamEvent

    live = Live(stack.supervisor, {"operation": lambda _event: stack.feed.wake()})
    assert live.kinds == ("operation",)
    live.handle(StreamEvent("operation", {"id": 1}, "fake:1"))
    assert stack.feed._wake.is_set()
