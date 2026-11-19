"""Keeping the channel's messages in order (docs/PLAN.md, *Event channel*, *Order kept*).

The bot stores every message id by key and edits in place. A continuation follows its first message
(``situation:1`` after ``situation``, ``orbat:1`` after ``orbat:0``); the report and the discussion link come last. A
message that must go *before* messages already posted (a preset added after the ORBAT, an OPORD paragraph written
later, a new ORBAT continuation before the discussion link) is the one case that re-posts: everything after it is
deleted and posted again. A message someone deleted by hand is treated the same way: it and everything after it are
posted again, so the order holds.

The thread is made after the other messages, so Discord's own "started a thread" card is last; stored as ``card``,
it stands in for the discussion link (decided 2026-11-02). Discord can't post that card again, so once a message
must go after it (a late preset, an ORBAT continuation, the report), the card is deleted and the link is posted.

A message is edited only when it changed: ``drawn`` holds each one's :func:`fingerprint` as last sent. Discord allows
only so many edits to messages older than an hour (error 30046), and a redraw that edited every message every time
(a restart, the feed's echo, a scheduled redraw) used it up. As a skipped edit no longer finds a message deleted by
hand, the service drops a deleted message's fingerprint (``on_raw_message_delete``, and a look at the channel after a
restart), so the next edit finds it gone and posts it again.

Even a changed message waits its turn: Discord takes about one edit every five seconds of a channel's messages older
than an hour (more is refused with 429, and in the end 30046), so :class:`Pacer` spaces such edits per channel and
the bot waits instead of being refused. A redraw waiting there holds its operation's lock, so the changes that come
meanwhile (a sign-up rush) fold into one next redraw."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import discord

from app.channel.render import Message

# The OPORD's paragraphs at 10, 20 ... 50, each with room for its continuations; key:N is the first message's place + N.
OPORD = {"situation": 10, "mission": 20, "execution": 30, "sustainment": 40, "command": 50}
FIXED = {"info": 0, **OPORD, "preset": 100}
PAGED = {**OPORD, "orbat": 1000}
LAST = {"report": 100_000, "discussion": 200_000, "card": 200_000}  # the report comes once the ORBAT is final
CARD, DISCUSSION = "card", "discussion"
log = logging.getLogger(__name__)


def order(key: str) -> int:
    if key in FIXED:
        return FIXED[key]
    if key in LAST:
        return LAST[key]
    kind, _, number = key.partition(":")
    return PAGED[kind] + int(number)


def _view(message: Message) -> dict[str, Any]:
    """For sending: a view only when there is one. Editing passes ``view`` always, as leaving it out keeps the
    buttons the message had (a cancelled event's sign-up buttons), while None removes them."""
    return {"view": message.view} if message.view is not None else {}


OLD = timedelta(hours=1)  # Discord's "old message": edits past it are limited per channel
SPACING = 6.0  # seconds between edits of a channel's old messages (Discord's 429s asked for 5.1 to 5.6)


class Pacer:
    """Edits of messages older than an hour spaced per channel (:data:`SPACING`), so Discord never refuses one."""

    def __init__(self, spacing: float = SPACING, *, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.spacing, self.sleep, self.monotonic, self.now = spacing, sleep, monotonic, now
        self._last: dict[int, float] = {}  # channel id -> when its last old message was edited

    async def wait(self, channel_id: int, message_id: int) -> None:
        """Before editing ``message_id`` in ``channel_id``: wait for its turn if the message is old."""
        if self.spacing <= 0 or discord.utils.snowflake_time(message_id) > self.now() - OLD:
            return
        last = self._last.get(channel_id)
        if last is not None:
            delay = last + self.spacing - self.monotonic()
            if delay > 0:
                await self.sleep(delay)
        self._last[channel_id] = self.monotonic()


def fingerprint(message: Message) -> str:
    """What the message shows: its text, embeds, buttons and files. Equal fingerprints need no edit."""
    body = {"content": message.content, "embeds": [embed.to_dict() for embed in message.embeds],
            "view": message.view.to_components() if message.view is not None else None,
            "files": message.tag if message.files else ""}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:32]


@dataclass(frozen=True)
class Op:
    action: str  # edit | send | delete
    key: str


def plan(stored: Mapping[str, int], wanted: Sequence[str]) -> list[Op]:
    """What to do so the channel holds exactly ``wanted``, in order, given what is posted (``stored``)."""
    wanted = sorted(wanted, key=order)
    kept = [key for key in stored if key in wanted]
    late = [order(key) for key in wanted if key not in stored and any(order(k) > order(key) for k in kept)]
    cut = min(late, default=None)  # from here on, everything is posted again
    ops = [Op("delete", key) for key in sorted(stored, key=order)
           if key not in wanted or (cut is not None and order(key) > cut)]
    for key in wanted:
        moved = cut is not None and order(key) > cut
        ops.append(Op("edit" if key in stored and not moved else "send", key))
    return ops


class Partial(Exception):
    """:func:`apply` failed part way (Discord refused, a rate limit): ``stored`` is what the channel holds now and
    ``drawn`` what those messages show, to be saved, or the messages already sent would be sent again by the next
    redraw and the old ones forgotten."""

    def __init__(self, stored: dict[str, int], drawn: dict[str, str]) -> None:
        super().__init__("the channel was brought only part way")
        self.stored, self.drawn = stored, drawn


async def apply(channel: Any, stored: Mapping[str, int], messages: Mapping[str, Message],
                drawn: Mapping[str, str] | None = None,
                pacer: Pacer | None = None) -> tuple[dict[str, int], dict[str, str]]:
    """Bring the channel to ``messages``; returns the new key -> id map and key -> :func:`fingerprint` map
    (``drawn``: what the stored messages show, an edit skipped where it matches). On a failure part way, raises
    :class:`Partial` with both as far as it got (its cause is the original error). Edits of old messages wait their
    turn at ``pacer``."""
    stored, drawn = dict(stored), dict(drawn or {})
    try:
        await _apply(channel, stored, drawn, messages, pacer)
    except Exception as exc:
        raise Partial(stored, drawn) from exc
    return stored, drawn


def _forget(stored: dict[str, int], drawn: dict[str, str], key: str) -> None:
    del stored[key]
    drawn.pop(key, None)


async def _apply(channel: Any, stored: dict[str, int], drawn: dict[str, str], messages: Mapping[str, Message],
                 pacer: Pacer | None = None) -> None:
    """:func:`apply`'s work, keeping ``stored`` and ``drawn`` (changed in place) true to the channel after every
    step."""
    for _attempt in range(2):
        wanted = dict(messages)
        if CARD in stored:
            if DISCUSSION in wanted and all(key in stored for key in wanted if key != DISCUSSION):
                del wanted[DISCUSSION]  # the card is still last: it is the link
            else:
                await _delete(channel, stored[CARD])
                _forget(stored, drawn, CARD)
        missing = None
        for op in plan({key: id_ for key, id_ in stored.items() if key != CARD}, list(wanted)):
            if op.action == "delete":
                await _delete(channel, stored[op.key])
                _forget(stored, drawn, op.key)
            elif op.action == "edit":
                message = messages[op.key]
                shown = fingerprint(message)
                if drawn.get(op.key) == shown:
                    continue
                if pacer is not None:
                    await pacer.wait(channel.id, stored[op.key])
                try:
                    files = {"attachments": [discord.File(p, p.name) for p in message.files]} if message.replace_files else {}
                    await channel.get_partial_message(stored[op.key]).edit(content=message.content,
                                                                           embeds=message.embeds, view=message.view,
                                                                           **files)
                except discord.NotFound:  # deleted by hand: post it and what follows again
                    missing = op.key
                    break
                drawn[op.key] = shown
            else:
                message = messages[op.key]
                files = {"files": [discord.File(p, p.name) for p in message.files]} if message.files else {}
                sent = await channel.send(content=message.content, embeds=message.embeds,
                                          allowed_mentions=discord.AllowedMentions.none(), **_view(message), **files)
                stored[op.key], drawn[op.key] = sent.id, fingerprint(message)
        if missing is None:
            return
        log.info("Message %s of #%s was deleted; posting it and the ones after it again", missing, channel.name)
        for key in [k for k in stored if order(k) >= order(missing)]:
            if key != missing:
                await _delete(channel, stored[key])
            _forget(stored, drawn, key)


async def _delete(channel: Any, message_id: int) -> None:
    try:
        await channel.get_partial_message(message_id).delete()
    except discord.NotFound:
        pass
