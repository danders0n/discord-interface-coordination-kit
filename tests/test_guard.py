from types import SimpleNamespace

import discord
import pytest
from conftest import EVENTS, GUILD
from pydantic import ValidationError
from fakes import FakeMember, FakeMessage
from test_channel import row, run, stack  # noqa: F401  (stack: the fixture)

from app.channel.guard import guard
from app.startup_checks import check


def switch(stack, **events):
    settings = stack.settings
    stack.store.current = settings.model_copy(update={"events": settings.events.model_copy(update=events)})


def post(where, author, content="Hej", stickers=(), **extra):
    message = FakeMessage(author, content, channel=where, **extra)
    message.guild, message.webhook_id = SimpleNamespace(id=GUILD, stickers=list(stickers)), None
    where.posts.append(message)
    return message


def member(member_id=500, **extra):
    found = FakeMember(member_id, "Bartek", **extra)
    found.bot = False
    return found


async def published(stack, guild, fake):
    op = fake.operation("Ibis", status="published")
    await run(stack)
    drawn = await row(stack, op["id"])
    return guild.get_channel(drawn.channel_id), guild.threads[drawn.thread_id]


async def test_off_by_default_nothing_is_deleted(stack, guild, fake):
    channel, _ = await published(stack, guild, fake)
    message = post(channel, member())
    assert await guard(message, stack.settings, stack.channels) is False
    assert message in channel.posts


async def test_a_message_in_the_channel_is_deleted_and_its_author_told_every_time(stack, guild, fake):
    switch(stack, delete_messages=True)
    channel, thread = await published(stack, guild, fake)
    bartek = member()
    for text in ("Hej, jestem!", "Jeszcze raz"):
        message = post(channel, bartek, text)
        assert await guard(message, stack.settings, stack.channels) is True
        assert message not in channel.posts
    assert len(bartek.dms) == 2
    first = bartek.dms[0].content
    assert first.startswith(f"Na kanale <#{channel.id}> pisze tylko bot")
    assert f"/{thread.id}" in first and first.endswith("Twoja wiadomość:\n> Hej, jestem!")
    assert bartek.dms[0].kwargs["allowed_mentions"] is not None


async def test_the_thread_bots_system_notes_and_other_channels_stay(stack, guild, fake):
    switch(stack, delete_messages=True)
    channel, thread = await published(stack, guild, fake)
    in_thread = post(thread, member())
    robot = member(501)
    robot.bot = True
    by_bot = post(channel, robot)
    pinned = post(channel, member(), type=discord.MessageType.pins_add)
    elsewhere = post(guild.get_channel(EVENTS), member())  # (not an operation's channel)
    for message in (in_thread, by_bot, pinned, elsewhere):
        assert await guard(message, stack.settings, stack.channels) is False
        assert message in message.channel.posts


async def test_the_configured_notice_fills_in_its_fields_and_closed_dms_still_delete(stack, guild, fake):
    switch(stack, delete_messages=True, delete_notice="Kanał {title} jest tylko dla bota.\\nPisz tu: {thread}")
    channel, thread = await published(stack, guild, fake)
    bartek = member()
    assert await guard(post(channel, bartek, ""), stack.settings, stack.channels) is True
    (dm,) = bartek.dms
    assert dm.content.startswith("Kanał Ibis jest tylko dla bota.\nPisz tu: https://discord.com/channels/")
    assert dm.content.endswith(f"/{thread.id}")
    closed = member(502, dms_closed=True)
    message = post(channel, closed)
    assert await guard(message, stack.settings, stack.channels) is True and message not in channel.posts


async def test_a_long_message_is_cut_to_fit_one_dm(stack, guild, fake):
    switch(stack, delete_messages=True)
    channel, _ = await published(stack, guild, fake)
    bartek = member()
    await guard(post(channel, bartek, "x" * 3000), stack.settings, stack.channels)
    assert len(bartek.dms[0].content) == 2000 and bartek.dms[0].content.endswith("…")


async def test_a_sticker_by_name_or_id_goes_with_the_dm(stack, guild, fake):
    channel, _ = await published(stack, guild, fake)
    salute = SimpleNamespace(id=777, name="Salut")
    switch(stack, delete_messages=True, delete_sticker="salut")
    bartek = member()
    await guard(post(channel, bartek, stickers=[salute]), stack.settings, stack.channels)
    assert bartek.dms[0].kwargs["stickers"] == [salute]
    switch(stack, delete_messages=True, delete_sticker=888)  # (.env's digits are read as an id)
    await guard(post(channel, bartek), stack.settings, stack.channels)
    assert [s.id for s in bartek.dms[1].kwargs["stickers"]] == [888]
    switch(stack, delete_messages=True, delete_sticker="gone")  # (not on the server: the text alone)
    await guard(post(channel, bartek, stickers=[salute]), stack.settings, stack.channels)
    assert "stickers" not in bartek.dms[2].kwargs and bartek.dms[2].content


async def test_a_sticker_discord_refuses_leaves_the_text_alone(stack, guild, fake):
    channel, _ = await published(stack, guild, fake)
    switch(stack, delete_messages=True, delete_sticker=888)
    bartek = member()
    plain_send = bartek.send

    async def refusing(content=None, **kwargs):
        if "stickers" in kwargs:
            raise discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), "Invalid sticker")
        return await plain_send(content, **kwargs)

    bartek.send = refusing
    assert await guard(post(channel, bartek), stack.settings, stack.channels) is True
    (dm,) = bartek.dms
    assert "stickers" not in dm.kwargs and "embed" not in dm.kwargs and dm.content.startswith("Na kanale")
    image = "https://media.discordapp.net/stickers/1492127676494774312.webp?size=160&quality=lossless"
    switch(stack, delete_messages=True, delete_sticker=888, delete_sticker_image=image)
    await guard(post(channel, bartek), stack.settings, stack.channels)
    assert bartek.dms[1].kwargs["embed"].image.url == image and "stickers" not in bartek.dms[1].kwargs


async def test_the_image_alone_when_no_sticker_is_set_and_never_with_one_sent(stack, guild, fake):
    channel, _ = await published(stack, guild, fake)
    image = "https://example.com/salut.png"
    switch(stack, delete_messages=True, delete_sticker_image=image)
    bartek = member()
    await guard(post(channel, bartek), stack.settings, stack.channels)
    assert bartek.dms[0].kwargs["embed"].image.url == image
    switch(stack, delete_messages=True, delete_sticker=888, delete_sticker_image=image)
    await guard(post(channel, bartek), stack.settings, stack.channels)
    assert "embed" not in bartek.dms[1].kwargs and bartek.dms[1].kwargs["stickers"]


def test_the_image_must_be_a_web_address(settings):
    from app.settings import Events

    assert Events(delete_sticker_image="https://example.com/a.png").delete_sticker_image
    with pytest.raises(ValidationError):
        Events(delete_sticker_image="salut.png")


async def test_a_sticker_name_missing_on_the_server_is_a_start_up_problem(guild, settings):
    guild.stickers = [SimpleNamespace(id=777, name="Salut")]
    named = lambda name: settings.model_copy(update={  # noqa: E731
        "language": "en", "events": settings.events.model_copy(update={"delete_sticker": name})})
    assert check(guild, named("SALUT"))[1] == [] and check(guild, named(888))[1] == []
    assert check(guild, named("gone"))[1] == ["BOT_EVENTS__DELETE_STICKER: gone not found on this server"]


async def test_switched_on_the_start_up_check_wants_manage_messages(guild, settings):
    allowed = discord.Permissions.all()
    allowed.update(manage_messages=False)
    guild.get_channel(EVENTS).permissions = allowed
    assert check(guild, settings)[1] == []
    on = settings.model_copy(update={"language": "en",
                                     "events": settings.events.model_copy(update={"delete_messages": True})})
    (problem,) = check(guild, on)[1]
    assert "manage_messages" in problem
