"""Erasing a member's data on request (docs/PLAN.md, *Privacy*, *Erasure*), without discord.py.

The Steam link is deleted (so the member drops out of squad.xml), and their Discord id and Steam ID are replaced
by one fixed pseudonym, ``former:<n>``, in every table: slots, sign-ups, offers, attendance, ratings (whose comments
are emptied), organisers, creator and author fields, approval decisions, templates, groups, links made by them.
Past events still add up, but nothing points back to the person. Discord ids are numbers, so in those columns the
pseudonym is ``-n`` (:func:`pseudonym` turns it back into words); Steam ID columns hold ``former:<n>``.

In archived transcripts their messages keep their place with the author "deleted" and no content, and their
attachments are deleted from disk. In events not started yet the member's places are simply freed (a pseudonym signed up for a coming event helps
nobody). Database backups are not rewritten; they age out with the rotation, as ``/privacy`` says."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select, update

from app.db import Database, state
from app.db.models import (ApprovalRequest, ArchivedMessage, AttendanceEntry, Event, EventOrganiser, LinkReminder,
                           MissionRating, Offer, OrbatTemplate, OtherGroup, Signup, Slot, SteamLink)

COUNTER_KEY = "erasures"


def pseudonym(member_id: int) -> str | None:
    """``former:<n>`` for an erased member's id, None for a real one."""
    return f"former:{-member_id}" if member_id < 0 else None


@dataclass(frozen=True)
class Erased:
    number: int  # the pseudonym is former:<number>
    events: list[int]  # events whose messages show the member: draw them again


async def erase(db: Database, member_id: int, now: Callable[[], datetime]) -> Erased:
    counter = await state.get(db, COUNTER_KEY) or {"last": 0}
    number = int(counter["last"]) + 1
    await state.put(db, COUNTER_KEY, {"last": number})
    former, former_steam = -number, f"former:{number}"
    async with db.session() as session, session.begin():
        link = await session.get(SteamLink, member_id)
        steam_ids = {link.steam_id} if link is not None else set()
        steam_ids |= set((await session.execute(select(AttendanceEntry.steam_id).where(
            AttendanceEntry.member_id == member_id, AttendanceEntry.steam_id.is_not(None)))).scalars())
        if link is not None:
            await session.delete(link)
        await session.execute(delete(LinkReminder).where(LinkReminder.member_id == member_id))

        coming = select(Event.id).where(Event.status.in_(("draft", "pending", "published")), Event.start_at > now())
        touched = set((await session.execute(select(Slot.event_id).where(Slot.holder_id == member_id))).scalars())
        touched |= set((await session.execute(select(Signup.event_id).where(Signup.member_id == member_id))).scalars())
        touched |= set((await session.execute(select(Event.id).where(
            (Event.created_by == member_id) | (Event.author_id == member_id)))).scalars())
        touched |= set((await session.execute(select(AttendanceEntry.event_id).where(
            AttendanceEntry.member_id == member_id))).scalars())
        await session.execute(update(Slot).where(Slot.holder_id == member_id, Slot.event_id.in_(coming))
                              .values(holder_id=None, locked=False, flagged=False))
        await session.execute(delete(Signup).where(Signup.member_id == member_id, Signup.event_id.in_(coming)))
        await session.execute(delete(Offer).where(Offer.member_id == member_id, Offer.event_id.in_(coming)))

        for model, column in ((Slot, Slot.holder_id), (Signup, Signup.member_id), (Offer, Offer.member_id),
                              (Event, Event.created_by), (Event, Event.author_id),
                              (EventOrganiser, EventOrganiser.member_id),
                              (ApprovalRequest, ApprovalRequest.requested_by), (ApprovalRequest, ApprovalRequest.decided_by),
                              (OrbatTemplate, OrbatTemplate.owner_id), (OtherGroup, OtherGroup.added_by),
                              (SteamLink, SteamLink.linked_by), (AttendanceEntry, AttendanceEntry.member_id),
                              (AttendanceEntry, AttendanceEntry.corrected_by)):
            await session.execute(update(model).where(column == member_id).values({column.key: former}))
        await session.execute(update(MissionRating).where(MissionRating.member_id == member_id)
                              .values(member_id=former, comment=""))
        for message in (await session.execute(select(ArchivedMessage).where(
                ArchivedMessage.author_id == member_id))).scalars():
            for item in message.attachments or ():
                Path(item["path"]).unlink(missing_ok=True)
            message.author_id, message.author_name, message.content, message.attachments = former, "", "", []
        if steam_ids:
            await session.execute(update(AttendanceEntry).where(AttendanceEntry.steam_id.in_(steam_ids))
                                  .values(steam_id=former_steam, name=""))
            await session.execute(update(AttendanceEntry).where(AttendanceEntry.probable_steam_id.in_(steam_ids))
                                  .values(probable_steam_id=None, probable_name=None))
    return Erased(number, sorted(touched))
