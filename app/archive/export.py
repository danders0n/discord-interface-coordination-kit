"""Moving the oldest quarter's channels into the database (docs/PLAN.md, *Long-term archive*).

A daily scheduled action (05:00, after the backup) looks for archived channels of a quarter that no longer stays.
For each: every message not posted by the bot, in the channel and its thread, with its attachments downloaded to
``<archive folder>/<event id>/<message id>-<file name>`` (Discord's links expire). Then the copy is checked (the
message count matches, every attachment is on disk with its size) and only then is the channel deleted, its
thread with it. A failure leaves the channel in place, goes to the staff log and is tried again the next day.
Archive categories left empty are deleted at the end."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import discord

from app.archive.service import Saved, quarter, quarter_name
from app.i18n import t
from app.scheduler import Action

log = logging.getLogger(__name__)
KIND = "archive_export"
HOUR = 5  # local time, after the 04:00 backup


def next_run(now: datetime, tz) -> datetime:
    local = now.astimezone(tz)
    run = local.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    if run <= local:
        run = (local + timedelta(days=1)).replace(hour=HOUR, minute=0, second=0, microsecond=0)
    return run.astimezone(UTC)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class Exporter:
    def __init__(self, services: Any, guild: Callable[[], Any]) -> None:
        self.s, self.guild = services, guild
        services.scheduler.register(KIND, self._run)

    async def start(self) -> None:
        if not await self.s.scheduler.exists(KIND):
            await self.s.scheduler.schedule(KIND, next_run(self.s.scheduler.clock(), self.s.settings.tz), key=KIND)

    async def _run(self, action: Action, now: datetime) -> datetime:
        guild, settings = self.guild(), self.s.settings
        if guild is None:
            return now + timedelta(hours=1)
        moved, quarters = 0, set()
        for event in await self.s.archive.leaving():
            try:
                await self.export(guild, event)
            except Exception as exc:  # noqa: BLE001 - the channel stays; tomorrow's run tries again
                log.exception("Moving event %s into the archive failed", event.id)
                self.s.staff.post(t("Moving {title} into the database archive failed: {reason}. The channel stays; the "
                                    "bot tries again tomorrow.", settings.language, title=event.title, reason=exc))
                continue
            moved += 1
            quarters.add(quarter(event.start_at, settings.tz))
        if moved:
            await self._tidy(guild, quarters)
            self.s.staff.post(t(("{count} event moved into the database archive.",
                                 "{count} events moved into the database archive."), settings.language, count=moved))
        return next_run(now, settings.tz)

    async def export(self, guild: Any, event: Any) -> None:
        channel = guild.get_channel(event.channel_id)
        if channel is None:  # deleted by hand: nothing left to copy
            await self.s.archive.exported(event.id)
            return
        sources = [channel]
        if event.thread_id:
            thread = guild.get_thread(event.thread_id)
            if thread is None:
                try:
                    thread = await guild.fetch_channel(event.thread_id)
                except discord.NotFound:
                    thread = None
            if thread is not None:
                sources.append(thread)
        folder = self.s.archive.folder / str(event.id)
        saved, expected = [], []
        for source in sources:
            async for message in source.history(limit=None, oldest_first=True):
                if message.author.id == guild.me.id:
                    continue
                files = []
                for attachment in message.attachments:
                    path = folder / f"{message.id}-{attachment.filename}"
                    await asyncio.to_thread(_write, path, await attachment.read())
                    files.append({"name": attachment.filename, "path": str(path), "size": attachment.size})
                    expected.append((path, attachment.size))
                reference = getattr(message, "reference", None)
                saved.append(Saved(source.id, message.id, message.author.id,
                                   (getattr(message.author, "display_name", None) or message.author.name)[:100],
                                   message.created_at, message.edited_at, message.content or "",
                                   reference.message_id if reference is not None else None, files))
        stored = await self.s.archive.save_transcript(event.id, saved)
        missing = [p for p, size in expected if not p.is_file() or p.stat().st_size != size]
        if stored != len(saved) or missing:
            raise RuntimeError(f"the copy doesn't match ({stored} of {len(saved)} messages, {len(missing)} files missing)")
        await channel.delete(reason="Moved into the long-term archive")
        await self.s.archive.exported(event.id)

    async def _tidy(self, guild: Any, quarters: set[int]) -> None:
        """The leaving quarters' archive categories (overflows too), once empty."""
        pattern = self.s.settings.channels.archive_pattern
        names = {quarter_name(q, pattern) for q in quarters}
        for category in list(guild.categories):
            if (category.name in names or any(category.name.startswith(f"{n} (") for n in names)) and not category.channels:
                await category.delete(reason="Its quarter moved into the long-term archive")
