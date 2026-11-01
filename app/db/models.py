"""Tables (docs/PLAN.md, *Data model*). Phase 1 has the scheduled actions and the bot's own state; the event
tables come with the phases that use them. Every schema change is an Alembic migration in ``migrations/``."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, TypeDecorator, UniqueConstraint, text
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


class Event(Base):
    """An event (docs/PLAN.md, *Data model*). Phase 3 keeps what sign-up needs; phase 4 adds the Discord side
    (channel, messages, briefing, preset, approval)."""

    __tablename__ = "event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # internal | guest | hosted | zeus
    title: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | pending | published | ended | cancelled
    assembly_at: Mapped[datetime]
    start_at: Mapped[datetime]
    duration_minutes: Mapped[int] = mapped_column(Integer)
    signup_opens_at: Mapped[datetime | None]
    signup_limit: Mapped[int | None] = mapped_column(Integer)  # Zeus: the most who may be Coming
    required_dlc: Mapped[list[str]] = mapped_column(JSON, default=list)  # Creator DLC keys of the preset (gm, vn ...)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Phase 4a: by value, so /reload can drop a mission type or climate without breaking the event.
    mission_type: Mapped[str] = mapped_column(String(50), default="")
    climate: Mapped[str] = mapped_column(String(100), default="")
    climate_role_id: Mapped[int | None] = mapped_column(BigInteger)
    author_id: Mapped[int | None] = mapped_column(BigInteger)
    host_group_id: Mapped[int | None] = mapped_column(ForeignKey("other_group.id"))  # a guest event's host
    how_to_join: Mapped[str] = mapped_column(Text, default="")  # a guest event: server, password, contacts
    briefing_situation: Mapped[str] = mapped_column(Text, default="")
    briefing_mission: Mapped[str] = mapped_column(Text, default="")
    briefing_notes: Mapped[str] = mapped_column(Text, default="")
    dont_end_automatically: Mapped[bool] = mapped_column(Boolean, default=False)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    # Phase 4b: the event channel and the bot's messages in it, in order (key -> message id).
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    thread_id: Mapped[int | None] = mapped_column(BigInteger)
    messages: Mapped[dict[str, Any]] = mapped_column(default=dict)
    announcement_id: Mapped[int | None] = mapped_column(BigInteger)
    archived_at: Mapped[datetime | None]  # moved to an archive category
    exported_at: Mapped[datetime | None]  # moved into the database (phase 8)
    # Phase 4c-2: the launcher preset and the briefing image, as files in the data folder.
    preset_name: Mapped[str | None] = mapped_column(String(200))
    preset_mods: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [[workshop id, name], ...]
    preset_file: Mapped[str | None] = mapped_column(String(300))
    briefing_image: Mapped[str | None] = mapped_column(String(300))
    attached: Mapped[dict[str, Any]] = mapped_column(default=dict)  # message key -> the files' tag last sent


class OtherGroup(Base):
    """Another group (docs/PLAN.md, *Event kinds*): a guest event's host or a hosted event's guest. Corrected,
    never deleted, because events refer to it."""

    __tablename__ = "other_group"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    acronym: Mapped[str] = mapped_column(String(10), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    added_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class EventOrganiser(Base):
    __tablename__ = "event_organiser"

    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), primary_key=True)
    member_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class EventGuestGroup(Base):
    __tablename__ = "event_guest_group"

    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("other_group.id"), primary_key=True)


class ApprovalRequest(Base):
    """A request for the moderators (docs/PLAN.md, *Approval*): publishing a member's event, the host reservation
    of a member's Zeus event, or new times for an approved member's event (``proposed``)."""

    __tablename__ = "approval_request"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # publish | server | times
    state: Mapped[str] = mapped_column(String(20), default="pending")  # pending | approved | rejected | withdrawn
    proposed: Mapped[dict[str, Any]] = mapped_column(default=dict)  # times: assembly_at, start_at, duration_minutes
    requested_by: Mapped[int] = mapped_column(BigInteger)
    decided_by: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[int | None] = mapped_column(BigInteger)  # the request in the moderation thread
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    decided_at: Mapped[datetime | None]


class Squad(Base):
    __tablename__ = "squad"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))


class Slot(Base):
    """One place in the ORBAT. An organiser's assignment is a holder plus ``locked``; a slot locked closed (``!``)
    is ``locked`` without one. One slot per member per event: the partial unique index."""

    __tablename__ = "slot"
    __table_args__ = (Index("ux_slot_holder", "event_id", "holder_id", unique=True,
                            sqlite_where=text("holder_id IS NOT NULL")),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    squad_id: Mapped[int] = mapped_column(ForeignKey("squad.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(100))
    required_role_id: Mapped[int | None] = mapped_column(BigInteger)
    holder_id: Mapped[int | None] = mapped_column(BigInteger)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    flagged: Mapped[bool] = mapped_column(Boolean, default=False)  # ⚠: the holder lost the required role


class Signup(Base):
    """A member's answer other than a slot: ``maybe``, ``cant``, ``reserve`` (with its place in the queue), or in
    a Zeus event ``coming``."""

    __tablename__ = "signup"
    __table_args__ = (UniqueConstraint("event_id", "member_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10))
    reserve_position: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Offer(Base):
    """A freed slot offered to the reserve list: to one member, or (``member_id`` null) to all of it at once."""

    __tablename__ = "offer"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"), index=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("slot.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(10), default="open")  # open | taken | declined | expired | withdrawn
    expires_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class OrbatTemplate(Base):
    """A shared ORBAT template: every creator uses them; the owner and *manage any event* change them."""

    __tablename__ = "orbat_template"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    owner_id: Mapped[int] = mapped_column(BigInteger)
    text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class HostReservation(Base):
    """An event's reservation of the host on the supervisor (docs/PLAN.md, *Supervisor: reservation*): what the bot
    asked for, and how it went. One per event; the event table (phase 4) refers to it by ``event_id``."""

    __tablename__ = "host_reservation"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    reservation_id: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20))  # booked | retrying | failed | ended | cancelling | cancelled
    error: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(80))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    link: Mapped[str | None] = mapped_column(String(500))
    profile_id: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class BotState(Base):
    """Small values the bot keeps between restarts (the header message id, the last start)."""

    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
