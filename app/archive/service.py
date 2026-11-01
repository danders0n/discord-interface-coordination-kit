"""The long-term archive (docs/PLAN.md, *Long-term archive*, *Event channel*, *Retention*), without discord.py.

Discord allows 500 channels per server, so only the current quarter and the ones before it (``keep_quarters`` in
all) stay as channels. An older quarter's events move into the database: their own data is there already, and the
exporter adds the **transcript** (every message not posted by the bot, in the channel and its thread) and downloads
the attachments, then deletes the channel. ``/archive event`` reads it back as an HTML file, for the event's
participants, its organisers, and *manage any event* and *bot admin* roles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select

from app.db import Database
from app.db.models import ArchivedMessage, Event, EventOrganiser, Signup, Slot
from app.permissions import Access, Perm
from app.settings import SettingsStore


def quarter(when: datetime, tz: tzinfo) -> int:
    """A quarter as one number (year * 4 + 0..3), in the config time zone."""
    local = when.astimezone(tz)
    return local.year * 4 + (local.month - 1) // 3


def quarter_name(number: int, pattern: str) -> str:
    """The archive category of a quarter, as ``archive_pattern`` names it."""
    return pattern.format(quarter=number % 4 + 1, year=number // 4)


@dataclass(frozen=True)
class Saved:
    """One message as the exporter read it, its attachments on disk already."""

    channel_id: int
    message_id: int
    author_id: int
    author_name: str
    created_at: datetime
    edited_at: datetime | None
    content: str
    reply_to: int | None
    attachments: list[dict[str, Any]]  # name, path, size


class ArchiveService:
    def __init__(self, db: Database, store: SettingsStore, clock: Callable[[], datetime] | None = None) -> None:
        self.db, self.store = db, store
        self.clock = clock or (lambda: datetime.now(UTC))

    @property
    def folder(self) -> Path:
        settings = self.store.current
        return settings.archive.attachments_dir or settings.data_dir / "archive"

    def leaving_quarter(self) -> int:
        """The newest quarter that no longer stays as channels."""
        return quarter(self.clock(), self.store.current.tz) - self.store.current.archive.keep_quarters

    async def leaving(self) -> list[Event]:
        """Archived channels of events from a quarter that leaves, not moved yet, oldest first."""
        tz, last = self.store.current.tz, self.leaving_quarter()
        async with self.db.session() as session:
            events = (await session.execute(select(Event).where(
                Event.archived_at.is_not(None), Event.exported_at.is_(None), Event.channel_id.is_not(None))
                .order_by(Event.start_at))).scalars().all()
        return [e for e in events if quarter(e.start_at, tz) <= last]

    async def save_transcript(self, event_id: int, messages: list[Saved]) -> int:
        """Keep the transcript (a retried export replaces the earlier copy). Returns how many are stored."""
        async with self.db.session() as session, session.begin():
            await session.execute(delete(ArchivedMessage).where(ArchivedMessage.event_id == event_id))
            session.add_all(ArchivedMessage(event_id=event_id, **vars(m)) for m in messages)
        async with self.db.session() as session:
            return int((await session.execute(select(func.count(ArchivedMessage.id)).where(
                ArchivedMessage.event_id == event_id))).scalar_one())

    async def exported(self, event_id: int) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event_id)
            row.exported_at, row.channel_id, row.thread_id, row.messages = self.clock(), None, None, {}

    async def messages(self, event_id: int) -> list[ArchivedMessage]:
        async with self.db.session() as session:
            return list((await session.execute(select(ArchivedMessage).where(
                ArchivedMessage.event_id == event_id).order_by(ArchivedMessage.created_at,
                                                               ArchivedMessage.message_id))).scalars())

    # ---------------------------------------------------------------- reading it back

    @staticmethod
    def _participant(member_id: int):
        """Held a slot, or was Coming, Maybe, Can't come or on the reserve list, or organised it."""
        return or_(Event.created_by == member_id,
                   Event.id.in_(select(Slot.event_id).where(Slot.holder_id == member_id)),
                   Event.id.in_(select(Signup.event_id).where(Signup.member_id == member_id)),
                   Event.id.in_(select(EventOrganiser.event_id).where(EventOrganiser.member_id == member_id)))

    @staticmethod
    def sees_all(access: Access) -> bool:
        return access.any(Perm.MANAGE_ANY_EVENT, Perm.BOT_ADMIN)

    async def readable(self, member_id: int, access: Access, text: str = "", limit: int = 25) -> list[Event]:
        """The exported events this member may read, newest first, whose title or date (dd.mm.yyyy) has ``text``."""
        query = select(Event).where(Event.exported_at.is_not(None))
        if not self.sees_all(access):
            query = query.where(self._participant(member_id))
        async with self.db.session() as session:
            events = (await session.execute(query.order_by(Event.start_at.desc()))).scalars().all()
        tz, typed = self.store.current.tz, text.strip().casefold()
        found = [e for e in events if typed in e.title.casefold() or typed in f"{e.start_at.astimezone(tz):%d.%m.%Y}"]
        return found[:limit]

    async def may_read(self, event_id: int, member_id: int, access: Access) -> Event | None:
        async with self.db.session() as session:
            query = select(Event).where(Event.id == event_id, Event.exported_at.is_not(None))
            if not self.sees_all(access):
                query = query.where(self._participant(member_id))
            return (await session.execute(query)).scalar_one_or_none()
