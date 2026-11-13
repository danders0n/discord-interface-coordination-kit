"""Erasing a member's data on request (docs/PLAN.md, *Privacy*, *Erasure*), without discord.py.

Their sign-ups, attendance, ratings, organiser places and Steam link are the supervisor's: its erasure replaces them
by a pseudonym (``/privacy-erase`` asks it in the staff member's name, or staff erase in the app), and its ``erased``
notice brings the erasure here (app/notices/deliver.py). What the bot itself holds goes here: their link reminders are deleted, and in
archived transcripts their messages keep their place with the author "deleted" and no content, their attachments
deleted from disk. Discord ids are numbers, so the pseudonym ``former:<n>`` is stored as ``-n`` (:func:`pseudonym`
turns it back into words). Database backups are not rewritten; they age out with the rotation, as ``/privacy``
says."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from app.db import Database, state
from app.db.models import ArchivedMessage, LinkReminder

COUNTER_KEY = "erasures"


def pseudonym(member_id: int) -> str | None:
    """``former:<n>`` for an erased member's id, None for a real one."""
    return f"former:{-member_id}" if member_id < 0 else None


@dataclass(frozen=True)
class Erased:
    number: int  # the pseudonym is former:<number>
    messages: int  # archived messages emptied


async def erase(db: Database, member_id: int, number: int | None = None) -> Erased:
    """``number``: the supervisor's ``former:<n>`` (its ``erased`` notice), so both name them alike; without one (a
    member the supervisor never knew) the bot's own counter, kept past the supervisor's numbers it has seen."""
    counter = await state.get(db, COUNTER_KEY) or {"last": 0}
    if number is None:
        number = int(counter["last"]) + 1
    await state.put(db, COUNTER_KEY, {"last": max(int(counter["last"]), number)})
    emptied = 0
    async with db.session() as session, session.begin():
        await session.execute(delete(LinkReminder).where(LinkReminder.member_id == member_id))
        for message in (await session.execute(select(ArchivedMessage).where(
                ArchivedMessage.author_id == member_id))).scalars():
            for item in message.attachments or ():
                Path(item["path"]).unlink(missing_ok=True)
            message.author_id, message.author_name, message.content, message.attachments = -number, "", "", []
            emptied += 1
    return Erased(number, emptied)
