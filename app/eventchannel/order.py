"""The channels of a category in date order (docs/PLAN.md, *Event channel*, *Order of the channels*).

In the events and drafts categories the channels of ``Ids.top`` come first, in that order (the header first unless
``BOT_CHANNELS__TOP`` lists it), then any other channel that isn't an event's, as it was, then the events by their
start. An archive category holds events only, by start. Discord puts a new channel at the bottom and keeps a moved
channel's old number, so the bot puts a category in order after every change: one request for the whole category,
and only when the order differs. Voice channels have their own order on Discord and are left alone."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

import discord

TEXT = (discord.ChannelType.text, discord.ChannelType.news, discord.ChannelType.forum, discord.ChannelType.media)


def wanted(channels: list[Any], top: Iterable[int], starts: Mapping[int, tuple[datetime, int]]) -> list[Any]:
    """``channels`` in their current order; ``starts``: an event's channel id -> (its start, the event id)."""
    by_id = {c.id: c for c in channels}
    first = [by_id[i] for i in dict.fromkeys(top) if i in by_id]
    events = sorted((c for c in channels if c.id in starts and c not in first), key=lambda c: starts[c.id])
    rest = [c for c in channels if c not in first and c not in events]
    return first + rest + events


async def apply(guild: Any, category: Any, top: Iterable[int], starts: Mapping[int, tuple[datetime, int]]) -> bool:
    """Put ``category`` in order; True when that took a request. ``category.channels`` is in Discord's order."""
    channels = [c for c in category.channels if c.type in TEXT]
    order = wanted(channels, top, starts)
    if order == channels:
        return False
    # discord.py has no public call for several positions at once; GuildChannel.move() sends this same request.
    await guild._state.http.bulk_channel_update(guild.id, [{"id": c.id, "position": n} for n, c in enumerate(order)],
                                                reason="Event channels in date order")
    return True
