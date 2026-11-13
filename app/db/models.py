"""Tables (docs/PLAN-front-end.md, *Database*). The supervisor owns the operations and everything about them; the
bot keeps its Discord plumbing (channels, threads, message ids), the delivery bookkeeping and the archive of members'
messages. Every schema change is an Alembic migration in ``migrations/``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text, TypeDecorator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UtcDateTime(TypeDecorator):
    """Stored as naive UTC (SQLite has no zones), always read back as aware UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime: times are stored as UTC and must carry a zone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UtcDateTime, dict[str, Any]: JSON}


class ScheduledAction(Base):
    """A timed action (docs/PLAN.md, *Scheduled actions and downtime*). ``key`` makes scheduling idempotent:
    scheduling the same key again moves the existing row instead of adding one."""

    __tablename__ = "scheduled_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    key: Mapped[str | None] = mapped_column(String(200), unique=True)
    operation_id: Mapped[int | None] = mapped_column(Integer, index=True)
    due_at: Mapped[datetime] = mapped_column(index=True)
    done_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OperationChannel(Base):
    """An operation's place on Discord (docs/PLAN-front-end.md, *Sync and drawing*): its channel, thread and the bot's
    messages in it (key -> message id, keyed as app/channel/render.py keys them, ``card`` for the thread card), the
    announcement. ``operation`` is the operation as last drawn (the supervisor's JSON): the header's calendar and the
    archive read it, and it is never the truth. ``change_seq``: the feed's number of that copy."""

    __tablename__ = "operation_channel"

    operation_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    title: Mapped[str] = mapped_column(String(100), default="")
    start_at: Mapped[datetime | None] = mapped_column(index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger)
    category_id: Mapped[int | None] = mapped_column(BigInteger)
    messages: Mapped[dict[str, Any]] = mapped_column(default=dict)
    attached: Mapped[dict[str, Any]] = mapped_column(default=dict)  # message key -> the files' tag last sent
    announcement_id: Mapped[int | None] = mapped_column(BigInteger)
    operation: Mapped[dict[str, Any]] = mapped_column(default=dict)
    change_seq: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None]  # moved to an archive category
    exported_at: Mapped[datetime | None]  # moved into the database (the long-term archive)
    gone_at: Mapped[datetime | None]  # deleted in the supervisor (a tombstone, or missing from a reset)


class ModerationMessage(Base):
    """An approval request's message in the moderation thread (docs/PLAN-front-end.md, *Notices*)."""

    __tablename__ = "moderation_message"

    request_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    operation_id: Mapped[int] = mapped_column(Integer, index=True)
    message_id: Mapped[int] = mapped_column(BigInteger)


class OfferMessage(Base):
    """A slot offer as the bot delivered it: by DM or in the thread (docs/PLAN-front-end.md, *Notices*)."""

    __tablename__ = "offer_message"

    offer_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    operation_id: Mapped[int] = mapped_column(Integer, index=True)
    via: Mapped[str] = mapped_column(String(10))  # dm | thread
    channel_id: Mapped[int] = mapped_column(BigInteger)  # the DM channel or the thread
    message_id: Mapped[int] = mapped_column(BigInteger)


class ArchivedMessage(Base):
    """A member's message in an operation's channel or thread, copied before the channel is deleted (docs/PLAN.md,
    *Long-term archive*). ``attachments``: ``[{name, path, size}]``, the files downloaded to the archive folder."""

    __tablename__ = "archived_message"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_id: Mapped[int] = mapped_column(Integer, index=True)
    channel_id: Mapped[int] = mapped_column(BigInteger)  # the channel or its thread
    message_id: Mapped[int] = mapped_column(BigInteger)
    author_id: Mapped[int] = mapped_column(BigInteger, index=True)
    author_name: Mapped[str] = mapped_column(String(100))  # as it was then
    created_at: Mapped[datetime]
    edited_at: Mapped[datetime | None]
    content: Mapped[str] = mapped_column(Text, default="")
    reply_to: Mapped[int | None] = mapped_column(BigInteger)
    attachments: Mapped[list[Any]] = mapped_column(JSON, default=list)


class LinkReminder(Base):
    """A member without a Steam link: when they were last reminded, whether their first sign-up asked, and whether
    staff turned the reminders off (``muted``; linking deletes the row, so it lasts until then)."""

    __tablename__ = "link_reminder"

    member_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    last_at: Mapped[datetime | None]
    count: Mapped[int] = mapped_column(Integer, default=0)
    asked_at_signup: Mapped[bool] = mapped_column(Boolean, default=False)
    muted: Mapped[bool] = mapped_column(Boolean, default=False)


class NoticeDelivery(Base):
    """A supervisor notice the bot started to deliver (docs/PLAN-front-end.md, *Notices*). Written before acting,
    so a crash never says a thing twice: a row still ``started`` after a restart is reported ``failed``
    (``bot_crashed``). ``reported_at``: the supervisor heard the outcome."""

    __tablename__ = "notice_delivery"

    notice_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    kind: Mapped[str] = mapped_column(String(40))
    state: Mapped[str] = mapped_column(String(20))  # started, delivered, failed
    reason: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
    reported_at: Mapped[datetime | None]


class BotState(Base):
    """Small values the bot keeps between restarts (the feed's and the notices' cursors, the header message id)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
