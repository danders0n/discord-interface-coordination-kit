"""Keeping the channel's messages in order (docs/PLAN.md, *Event channel*, *Order kept*).

The bot stores every message id by key and edits in place. The ORBAT is last, so a new continuation is simply
posted at the end and one no longer needed is deleted from the end. The one case that re-posts is a message that
must go *before* messages already posted (a preset added after the ORBAT): everything after it is deleted and
posted again. A message someone deleted by hand is treated the same way: it and everything after it are posted
again, so the order holds."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import discord

from app.eventchannel.render import Message

FIXED = {"info": 0, "briefing": 1, "preset": 2}
log = logging.getLogger(__name__)


def order(key: str) -> int:
    if key in FIXED:
        return FIXED[key]
    kind, _, number = key.partition(":")
    assert kind == "orbat", key
    return len(FIXED) + int(number)


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
        missing = None
        for op in plan(stored, list(messages)):
            if op.action == "delete":
                try:
                    await channel.get_partial_message(stored[op.key]).delete()
                except discord.NotFound:
                    pass
                del stored[op.key]
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
                try:
                    await channel.get_partial_message(stored[key]).delete()
                except discord.NotFound:
                    pass
            del stored[key]
    return stored
