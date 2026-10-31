"""The staff log and moderation threads (docs/PLAN.md, *Notifications*): the bot keeps both in the staff channel.

It makes a thread the first time it needs it, remembers its id in ``bot_state``, makes a new one when the old is
gone (deleted, or the staff channel changed), follows a renamed thread name in `.env`, and unarchives a thread
that Discord archived after a quiet week before writing to it."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from app.db import Database, state
from app.discord_util import NO_MENTIONS
from app.settings import SettingsStore

KINDS = ("staff_log", "moderation")
WEEK = 10080  # minutes: the longest auto-archive Discord allows
log = logging.getLogger(__name__)


class StaffThreads:
    def __init__(self, db: Database, store: SettingsStore) -> None:
        self.db = db
        self.store = store
        self._locks = {kind: asyncio.Lock() for kind in KINDS}

    def name(self, kind: str) -> str:
        channels = self.store.current.channels
        return channels.staff_log_thread if kind == "staff_log" else channels.moderation_thread

    async def get(self, guild: Any, staff: Any, kind: str) -> Any:
        """The thread of ``kind`` in the ``staff`` channel, made or repaired as needed, ready to take messages."""
        async with self._locks[kind]:
            thread = await self._known(guild, kind)
            if thread is None or thread.parent_id != staff.id:
                thread = await staff.create_thread(name=self.name(kind), type=discord.ChannelType.public_thread,
                                                   auto_archive_duration=WEEK)
                await state.put(self.db, f"thread:{kind}", {"id": thread.id})
                log.info("Made the %s thread %s in #%s", kind, thread.id, staff.name)
            changes: dict[str, Any] = {}
            if thread.archived:
                changes["archived"] = False
            if thread.name != self.name(kind):
                changes["name"] = self.name(kind)
            if changes:
                thread = await thread.edit(**changes)
            return thread

    async def _known(self, guild: Any, kind: str) -> Any | None:
        stored = await state.get(self.db, f"thread:{kind}")
        if not stored:
            return None
        thread = guild.get_thread(stored["id"])
        if thread is not None:
            return thread
        try:  # an archived thread isn't in the cache
            return await guild.fetch_channel(stored["id"])
        except discord.NotFound:
            return None

    async def send(self, guild: Any, staff: Any, kind: str, content: str | None = None, **kwargs: Any) -> Any:
        kwargs.setdefault("allowed_mentions", NO_MENTIONS)
        thread = await self.get(guild, staff, kind)
        return await thread.send(content, **kwargs)
