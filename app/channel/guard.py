"""Messages written in an operation's channel (``BOT_EVENTS__DELETE_MESSAGES``, off by default): the channel holds
the bot's messages only, so whatever someone else writes there (an organiser too) is deleted, every time, and its
author is told in a DM where to write instead (``BOT_EVENTS__DELETE_NOTICE``), with their text quoted so it isn't
lost. The discussion thread is left alone, and so are bots, webhooks and Discord's own notes (a pin, a thread)."""

from __future__ import annotations

import logging
from typing import Any

import discord

from app.channel.service import OperationChannels
from app.discord_util import NO_MENTIONS
from app.i18n import t
from app.settings import Settings

log = logging.getLogger(__name__)

WRITTEN = (discord.MessageType.default, discord.MessageType.reply)


async def guard(message: Any, settings: Settings, channels: OperationChannels | None) -> bool:
    """Delete ``message`` when it was written in an operation's channel and tell its author; True when deleted."""
    if not settings.events.delete_messages or channels is None:
        return False
    if message.guild is None or message.guild.id != settings.guild_id:
        return False
    if message.author.bot or message.webhook_id is not None or message.type not in WRITTEN:
        return False
    row = await channels.by_channel(message.channel.id)
    if row is None or row.channel_id != message.channel.id:  # (its thread is for talking)
        return False
    try:
        await message.delete()
    except discord.NotFound:
        pass  # deleted meanwhile: still worth the note
    except discord.HTTPException as exc:
        log.warning("Could not delete a message in %s (Manage Messages?): %s", message.channel.id, exc)
        return False
    text = notice(settings, message, channels.link(row.thread_id), row.title or message.channel.name)
    await tell(message.author, text, sticker(settings, message.guild), settings.events.delete_sticker_image)
    return True


def sticker(settings: Settings, guild: Any) -> Any | None:
    """``BOT_EVENTS__DELETE_STICKER``: an id as it is (the server's or Discord's own), a name among the server's."""
    wanted = settings.events.delete_sticker
    if wanted is None:
        return None
    if isinstance(wanted, int):
        return discord.Object(id=wanted)
    found = next((s for s in getattr(guild, "stickers", ()) if s.name.casefold() == wanted.casefold()), None)
    if found is None:
        log.warning("BOT_EVENTS__DELETE_STICKER: no sticker called %s on the server", wanted)
    return found


async def tell(author: Any, text: str, with_sticker: Any | None, image: str | None = None) -> None:
    """The DM, with the sticker when there is one. Without it (none set, not on the server, or refused by Discord
    there) the image (``BOT_EVENTS__DELETE_STICKER_IMAGE``) goes in an embed instead, or the text alone."""
    try:
        if with_sticker is not None:
            try:
                await author.send(text, stickers=[with_sticker], allowed_mentions=NO_MENTIONS)
                return
            except discord.Forbidden:
                raise
            except discord.HTTPException as exc:
                log.warning("The sticker %s could not be sent in a DM: %s", with_sticker.id, exc)
        extra = {"embed": discord.Embed().set_image(url=image)} if image else {}
        await author.send(text, allowed_mentions=NO_MENTIONS, **extra)
    except discord.HTTPException:
        log.info("Could not tell %s why their message was deleted (closed DMs)", author.id)


def notice(settings: Settings, message: Any, thread: str | None, title: str) -> str:
    """The DM: the configured text (or the default one) and the deleted text quoted, within Discord's 2000."""
    language, channel = settings.language, message.channel.mention
    custom = settings.events.delete_notice
    if custom:
        text = custom.replace("\\n", "\n")
        for name, value in (("{channel}", channel), ("{thread}", thread or channel), ("{title}", title)):
            text = text.replace(name, value)
    elif thread:
        text = t("Only the bot writes in {channel}, so your message was removed. Talk about the operation in its "
                 "thread: {thread}", language, channel=channel, thread=thread)
    else:
        text = t("Only the bot writes in {channel}, so your message was removed.", language, channel=channel)
    content = (message.content or "").strip()
    if not content:
        return text[:2000]
    head = f"{text}\n\n{t('Your message:', language)}\n"
    room = 2000 - len(head)
    quoted = "\n".join(f"> {line}" for line in content.splitlines())
    if len(quoted) > room:
        quoted = quoted[:max(room - 1, 0)] + "…"
    return (head + quoted)[:2000] if room > 0 else text[:2000]
