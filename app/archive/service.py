"""The long-term archive (docs/PLAN.md, *Long-term archive*, *Event channel*, *Retention*), without discord.py.

Discord allows 500 channels per server, so only the current quarter and the ones before it (``keep_quarters`` in
all) stay as channels. An older quarter's operations move into the database: the operation itself is the
supervisor's (and its last copy is in ``operation_channel``), and the exporter adds the **transcript** (every message
not posted by the bot, in the channel and its thread) and downloads the attachments, then deletes the channel.
``/archive event`` reads it back as an HTML file, for the operation's organisers and *bot admin* roles (its
participants too once the supervisor's operation lists them: docs/PLAN-front-end.md, phase 5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from app.channel.render import organisers
from app.db import Database
from app.db.models import ArchivedMessage, OperationChannel
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

    async def leaving(self) -> list[OperationChannel]:
        """Archived channels of operations from a quarter that leaves, not moved yet, oldest first."""
        tz, last = self.store.current.tz, self.leaving_quarter()
        async with self.db.session() as session:
            rows = (await session.execute(select(OperationChannel).where(
                OperationChannel.archived_at.is_not(None), OperationChannel.exported_at.is_(None),
                OperationChannel.channel_id.is_not(None)).order_by(OperationChannel.start_at))).scalars().all()
        return [r for r in rows if quarter(r.start_at or r.archived_at, tz) <= last]

    async def save_transcript(self, op_id: int, messages: list[Saved]) -> int:
        """Keep the transcript (a retried export replaces the earlier copy). Returns how many are stored."""
        async with self.db.session() as session, session.begin():
            await session.execute(delete(ArchivedMessage).where(ArchivedMessage.operation_id == op_id))
            session.add_all(ArchivedMessage(operation_id=op_id, **vars(m)) for m in messages)
        async with self.db.session() as session:
            return int((await session.execute(select(func.count(ArchivedMessage.id)).where(
                ArchivedMessage.operation_id == op_id))).scalar_one())

    async def exported(self, op_id: int) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(OperationChannel, op_id)
            row.exported_at, row.channel_id, row.thread_id, row.messages = self.clock(), None, None, {}

    async def messages(self, op_id: int) -> list[ArchivedMessage]:
        async with self.db.session() as session:
            return list((await session.execute(select(ArchivedMessage).where(
                ArchivedMessage.operation_id == op_id).order_by(ArchivedMessage.created_at,
                                                                ArchivedMessage.message_id))).scalars())

    # ---------------------------------------------------------------- reading it back

    @staticmethod
    def sees_all(access: Access) -> bool:
        return access.has(Perm.BOT_ADMIN)

    @staticmethod
    def _may(row: OperationChannel, member_id: int) -> bool:
        return member_id in organisers(row.operation or {})

    async def readable(self, member_id: int, access: Access, text: str = "", limit: int = 25) -> list[OperationChannel]:
        """The exported operations this member may read, newest first, whose title or date (dd.mm.yyyy) has
        ``text``."""
        async with self.db.session() as session:
            rows = (await session.execute(select(OperationChannel).where(OperationChannel.exported_at.is_not(None))
                                          .order_by(OperationChannel.start_at.desc()))).scalars().all()
        tz, typed, everyone = self.store.current.tz, text.strip().casefold(), self.sees_all(access)
        found = [r for r in rows if (everyone or self._may(r, member_id)) and (
            typed in r.title.casefold() or (r.start_at is not None and typed in f"{r.start_at.astimezone(tz):%d.%m.%Y}"))]
        return found[:limit]

    async def may_read(self, op_id: int, member_id: int, access: Access) -> OperationChannel | None:
        async with self.db.session() as session:
            row = await session.get(OperationChannel, op_id)
        if row is None or row.exported_at is None:
            return None
        return row if self.sees_all(access) or self._may(row, member_id) else None
