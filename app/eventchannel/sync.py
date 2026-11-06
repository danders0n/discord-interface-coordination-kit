"""Keeping the channel's messages in order (docs/PLAN.md, *Event channel*, *Order kept*).

The bot stores every message id by key and edits in place. A continuation follows its first message (``briefing:1``
after ``briefing``, ``orbat:1`` after ``orbat:0``); the report and the discussion link come last. A message that
must go *before* messages already posted (a preset added after the ORBAT, a new ORBAT continuation before the
discussion link) is the one case that re-posts: everything after it is deleted and posted again. A message someone deleted by hand is treated the same way: it and everything after it are posted
again, so the order holds.

The thread is made after the other messages, so Discord's own "started a thread" card is last; stored as ``card``,
it stands in for the discussion link (decided 2026-11-02). Discord can't post that card again, so once a message
must go after it (a late preset, an ORBAT continuation, the report), the card is deleted and the link is posted."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import discord

from app.eventchannel.render import Message

FIXED = {"info": 0, "briefing": 10, "preset": 100}
PAGED = {"briefing": 10, "orbat": 1000}  # key:N is the first message's place + N
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


async def apply(channel: Any, stored: dict[str, int], messages: Mapping[str, Message]) -> dict[str, int]:
    """Bring the channel to ``messages``; returns the new key -> id map (save it even after an error)."""
    stored = dict(stored)
    for _attempt in range(2):
        wanted = dict(messages)
        if CARD in stored:
            if DISCUSSION in wanted and all(key in stored for key in wanted if key != DISCUSSION):
                del wanted[DISCUSSION]  # the card is still last: it is the link
            else:
                await _delete(channel, stored.pop(CARD))
        missing = None
        for op in plan({key: id_ for key, id_ in stored.items() if key != CARD}, list(wanted)):
            if op.action == "delete":
                await _delete(channel, stored.pop(op.key))
            elif op.action == "edit":
                message = messages[op.key]
                try:
                    files = {"attachments": [discord.File(p, p.name) for p in message.files]} if message.replace_files else {}
                    await channel.get_partial_message(stored[op.key]).edit(content=message.content,
                                                                           embeds=message.embeds, view=message.view,
                                                                           **files)
                except discord.NotFound:  # deleted by hand: post it and what follows again
                    missing = op.key
                    break
            else:
                message = messages[op.key]
                files = {"files": [discord.File(p, p.name) for p in message.files]} if message.files else {}
                sent = await channel.send(content=message.content, embeds=message.embeds,
                                          allowed_mentions=discord.AllowedMentions.none(), **_view(message), **files)
                stored[op.key] = sent.id
        if missing is None:
            return stored
        log.info("Message %s of #%s was deleted; posting it and the ones after it again", missing, channel.name)
        for key in [k for k in stored if order(k) >= order(missing)]:
            if key != missing:
                await _delete(channel, stored[key])
            del stored[key]
    return stored


async def _delete(channel: Any, message_id: int) -> None:
    try:
        await channel.get_partial_message(message_id).delete()
    except discord.NotFound:
        pass
