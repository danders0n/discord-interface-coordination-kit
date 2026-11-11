"""The staff log and moderation threads (docs/PLAN.md, *Notifications*): the bot keeps both in the staff channel.

It makes a thread the first time it needs it, remembers its id in ``bot_state``, makes a new one when the old is
gone (deleted, or the staff channel changed), takes over a thread of its own with the same name in the staff channel
rather than making a second one (the database didn't know it: another copy of the data), follows a renamed thread name in `.env`, and unarchives a thread
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
ARCHIVED = 100  # archived threads searched for one to take over
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
                thread = await self._existing(guild, staff, kind)
                if thread is not None:
                    log.info("Took over the %s thread %s in #%s", kind, thread.id, staff.name)
                else:
                    thread = await staff.create_thread(name=self.name(kind), type=discord.ChannelType.public_thread,
                                                       auto_archive_duration=WEEK)
                    log.info("Made the %s thread %s in #%s", kind, thread.id, staff.name)
                await state.put(self.db, f"thread:{kind}", {"id": thread.id})
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

    async def _existing(self, guild: Any, staff: Any, kind: str) -> Any | None:
        """A thread the bot made earlier in ``staff`` under this kind's name that the database doesn't know (another
        copy of the data, a restored backup): the oldest, which holds the history. Open threads first, then
        archived ones."""
        name, me = self.name(kind), guild.me.id

        def ours(thread: Any) -> bool:
            return thread.name == name and thread.owner_id == me

        found = [thread for thread in staff.threads if ours(thread)]
        if not found:
            try:
                found = [thread async for thread in staff.archived_threads(limit=ARCHIVED) if ours(thread)]
            except discord.HTTPException:
                log.warning("Could not list the archived threads of #%s", staff.name)
        return min(found, key=lambda thread: thread.id, default=None)

    async def send(self, guild: Any, staff: Any, kind: str, content: str | None = None, **kwargs: Any) -> Any:
        kwargs.setdefault("allowed_mentions", NO_MENTIONS)
        thread = await self.get(guild, staff, kind)
        return await thread.send(content, **kwargs)
