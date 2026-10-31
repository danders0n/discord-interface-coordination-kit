"""Tables (docs/PLAN.md, *Data model*). Phase 1 has the scheduled actions and the bot's own state; the event
tables come with the phases that use them. Every schema change is an Alembic migration in ``migrations/``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text, TypeDecorator
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
    event_id: Mapped[int | None] = mapped_column(Integer, index=True)
    due_at: Mapped[datetime] = mapped_column(index=True)
    done_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BotState(Base):
    """Small values the bot keeps between restarts (the header message id, the last start)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
