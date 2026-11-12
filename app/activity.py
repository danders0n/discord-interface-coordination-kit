"""Activity entries in the staff log thread (docs/PLAN.md, *Notifications*): who did what, one line each.

The services call it once a change is saved. It looks up the names a line needs (the event, the slot, the groups)
and posts the line to the staff log in the config language, with the time it happened, mentions silenced by the
staff log itself. It never raises: a failed entry goes to the log file and the change stands. ``Activity.off()``
(services built alone, in tests) does nothing and reads nothing.

Edits are compared, not described: :meth:`Activity.snapshot` before the change and :meth:`Activity.edited` after
write one line per detail that differs, with *old → new* (long texts, the briefing and how to join, only say that
they changed)."""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db import Database
from app.db.models import Event, EventGuestGroup, OtherGroup, Slot, Squad
from app.i18n import t
from app.settings import SettingsStore

QUIET = ("briefing", "how_to_join")  # details whose values are too long for a line
log = logging.getLogger(__name__)


def _ts(value: datetime, style: str) -> str:
    return f"<t:{int(value.timestamp())}:{style}>"


def mention(member_id: int) -> str:
    """As ``render.mention`` (which this module can't import: the sign-up service, which render imports, uses it)."""
    return f"<@{member_id}>" if member_id > 0 else f"former:{-member_id}"


def kind_label(kind: str, language: str, names: Any = None) -> str:
    from app.eventchannel.render import kind_label as label

    return label(kind, language, names)


def duration(minutes: int, language: str) -> str:
    from app.eventchannel.render import duration as text

    return text(minutes, language)


def _safe(method: Callable[..., Any]) -> Callable[..., Any]:
    """An entry must never break what it records."""

    @functools.wraps(method)
    async def wrapper(self: Activity, *args: Any, **kwargs: Any) -> Any:
        if not self.on:
            return None
        try:
            return await method(self, *args, **kwargs)
        except Exception:  # noqa: BLE001 - logged; the change itself is saved
            log.warning("Could not write the activity entry %s", method.__name__, exc_info=True)
            return None

    return wrapper


class Activity:
    def __init__(self, db: Database | None, store: SettingsStore | None, post: Callable[[str], None] | None,
                 clock: Callable[[], datetime] | None) -> None:
        self.db, self.store, self.post, self.clock = db, store, post, clock

    @classmethod
    def off(cls) -> Activity:
        return cls(None, None, None, None)

    @property
    def on(self) -> bool:
        return self.post is not None

    @property
    def language(self) -> str:
        return self.store.current.language

    def _emit(self, text: str) -> None:
        self.post(f"{_ts(self.clock(), 'T')} {text}")

    # ---------------------------------------------------------------- names

    async def _event(self, event_id: int) -> str:
        async with self.db.session() as session:
            event = await session.get(Event, event_id)
        if event is None:
            return f"#{event_id}"
        return f"**{event.title}** (<#{event.channel_id}>)" if event.channel_id else f"**{event.title}**"

    async def _slot(self, slot_id: int) -> str:
        async with self.db.session() as session:
            row = (await session.execute(select(Slot.name, Squad.name).join(Squad, Slot.squad_id == Squad.id)
                                         .where(Slot.id == slot_id))).first()
        return f"{row[1]} / {row[0]}" if row else f"#{slot_id}"

    def _who(self, member_id: int | None) -> str:
        return mention(member_id) if member_id is not None else t("the bot", self.language)

    def _members(self, member_ids: Iterable[int]) -> str:
        return ", ".join(mention(m) for m in member_ids) or "—"

    # ---------------------------------------------------------------- sign-ups

    @_safe
    async def took(self, event_id: int, member_id: int, slot_id: int, from_slot: int | None = None) -> None:
        event, slot = await self._event(event_id), await self._slot(slot_id)
        if from_slot is None:
            self._emit(t("{member} took {slot} in {event}.", self.language, member=mention(member_id), slot=slot,
                         event=event))
        else:
            self._emit(t("{member} moved from {before} to {slot} in {event}.", self.language, member=mention(member_id),
                         before=await self._slot(from_slot), slot=slot, event=event))

    @_safe
    async def left(self, event_id: int, member_id: int, slot_id: int | None) -> None:
        event = await self._event(event_id)
        if slot_id is not None:
            self._emit(t("{member} left {slot} in {event}.", self.language, member=mention(member_id),
                         slot=await self._slot(slot_id), event=event))
        else:
            self._emit(t("{member} left the sign-up of {event}.", self.language, member=mention(member_id), event=event))

    @_safe
    async def answered(self, event_id: int, member_id: int, status: str, slot_id: int | None = None,
                       coming_kind: bool = False) -> None:
        """Maybe, Can't come (Not coming in a Zeus or training event), Coming, or the reserve list; ``slot_id``:
        the slot the answer gave up."""
        language, event, who = self.language, await self._event(event_id), mention(member_id)
        if status == "maybe":
            text = t("{member} answered Maybe for {event}.", language, member=who, event=event)
        elif status == "cant" and coming_kind:
            text = t("{member} answered Not coming for {event}.", language, member=who, event=event)
        elif status == "cant":
            text = t("{member} answered Can't come for {event}.", language, member=who, event=event)
        elif status == "coming":
            text = t("{member} answered Coming for {event}.", language, member=who, event=event)
        else:
            text = t("{member} joined the reserve list of {event}.", language, member=who, event=event)
        if slot_id is not None:
            text += " " + t("(gave up {slot})", language, slot=await self._slot(slot_id))
        self._emit(text)

    @_safe
    async def offered(self, event_id: int, slot_id: int, member_id: int | None, until: datetime) -> None:
        event, slot = await self._event(event_id), await self._slot(slot_id)
        if member_id is None:
            self._emit(t("{slot} in {event} offered to the whole reserve list until {until}.", self.language, slot=slot,
                         event=event, until=_ts(until, "t")))
        else:
            self._emit(t("{slot} in {event} offered to {member} until {until}.", self.language, slot=slot, event=event,
                         member=mention(member_id), until=_ts(until, "t")))

    @_safe
    async def offer_ended(self, event_id: int, slot_id: int, member_id: int | None, state: str) -> None:
        """``state``: declined (by ``member_id``) or expired."""
        event, slot = await self._event(event_id), await self._slot(slot_id)
        if state == "declined":
            self._emit(t("{member} declined {slot} in {event}.", self.language, member=mention(member_id), slot=slot,
                         event=event))
        elif member_id is None:
            self._emit(t("Nobody on the reserve list took {slot} in {event} in time.", self.language, slot=slot,
                         event=event))
        else:
            self._emit(t("The offer of {slot} in {event} to {member} expired.", self.language, slot=slot, event=event,
                         member=mention(member_id)))

    # ---------------------------------------------------------------- organisers and the ORBAT

    @_safe
    async def locked(self, event_id: int, slot_id: int, by: int | None, locked: bool, holder: int | None) -> None:
        language, event, slot, who = self.language, await self._event(event_id), await self._slot(slot_id), self._who(by)
        if not locked:
            text = t("{by} unlocked {slot} in {event}.", language, by=who, slot=slot, event=event)
        elif holder is None:
            text = t("{by} locked {slot} in {event} closed.", language, by=who, slot=slot, event=event)
        else:
            text = t("{by} locked {member} in {slot} in {event}.", language, by=who, member=mention(holder), slot=slot,
                     event=event)
        self._emit(text)

    @_safe
    async def assigned(self, event_id: int, slot_id: int, member_id: int, by: int | None,
                       displaced: int | None) -> None:
        text = t("{by} put {member} in {slot} in {event}.", self.language, by=self._who(by), member=mention(member_id),
                 slot=await self._slot(slot_id), event=await self._event(event_id))
        if displaced is not None:
            text += " " + t("{member} goes to the top of the reserve list.", self.language, member=mention(displaced))
        self._emit(text)

    @_safe
    async def removed(self, event_id: int, slot_id: int, member_id: int, by: int | None) -> None:
        self._emit(t("{by} took {member} out of {slot} in {event}.", self.language, by=self._who(by),
                     member=mention(member_id), slot=await self._slot(slot_id), event=await self._event(event_id)))

    @_safe
    async def orbat_set(self, event_id: int, by: int | None, slots: int, displaced: list[int]) -> None:
        text = t(("{by} set the ORBAT of {event}: {count} slot.", "{by} set the ORBAT of {event}: {count} slots."),
                 self.language, by=self._who(by), event=await self._event(event_id), count=slots)
        if displaced:
            text += " " + t("Their slot is gone, now at the top of the reserve list: {members}", self.language,
                            members=self._members(displaced))
        self._emit(text)

    # ---------------------------------------------------------------- events

    @_safe
    async def created(self, event_id: int, member_id: int) -> None:
        async with self.db.session() as session:
            event = await session.get(Event, event_id)
        self._emit(t("{member} created {event} ({kind}, assembly {when}).", self.language, member=mention(member_id),
                     event=await self._event(event_id), when=_ts(event.assembly_at, "f"),
                     kind=kind_label(event.kind, self.language, self.store.current.events.kind_names)))

    async def snapshot(self, event_id: int) -> dict[str, str]:
        """The details an edit can change, as shown in an entry; empty when off (or the event is gone)."""
        if not self.on:
            return {}
        try:
            return await self._snapshot(event_id)
        except Exception:  # noqa: BLE001
            log.warning("Could not read event %s for the activity entry", event_id, exc_info=True)
            return {}

    async def _snapshot(self, event_id: int) -> dict[str, str]:
        language = self.language
        async with self.db.session() as session:
            e = await session.get(Event, event_id)
            if e is None:
                return {}
            guests = sorted((await session.execute(select(OtherGroup.name).join(
                EventGuestGroup, EventGuestGroup.group_id == OtherGroup.id).where(
                EventGuestGroup.event_id == event_id))).scalars())
            host = (await session.get(OtherGroup, e.host_group_id)).name if e.host_group_id else "—"
        return {
            "title": e.title,
            "kind": kind_label(e.kind, language, self.store.current.events.kind_names),
            "climate": e.climate or "—",
            "host": host,
            "groups": ", ".join(guests) or "—",
            "times": f"{_ts(e.assembly_at, 'f')} / {_ts(e.start_at, 't')}, {duration(e.duration_minutes, language)}",
            "signup": _ts(e.signup_opens_at, "f") if e.signup_opens_at else t("at publishing", language),
            "limit": str(e.signup_limit) if e.signup_limit else t("none", language),
            "auto_end": t("off", language) if e.dont_end_automatically else t("on", language),
            "briefing": "\n".join((e.briefing_situation, e.briefing_mission, e.briefing_notes)),
            "how_to_join": e.how_to_join,
        }

    def _label(self, key: str) -> str:
        language = self.language
        return {"title": t("the title", language), "kind": t("the kind", language),
                "climate": t("the climate", language), "host": t("the host group", language),
                "groups": t("the invited groups", language), "times": t("the times", language),
                "signup": t("the sign-up opening", language), "limit": t("the limit", language),
                "auto_end": t("the automatic end", language), "briefing": t("the briefing texts", language),
                "how_to_join": t("how to join", language)}[key]

    @_safe
    async def edited(self, event_id: int, member_id: int, before: dict[str, str]) -> None:
        """One line per detail that differs from ``before`` (a :meth:`snapshot`)."""
        if not before:
            return
        after = await self._snapshot(event_id)
        event = await self._event(event_id)
        for key, old in before.items():
            new = after.get(key, old)
            if new == old:
                continue
            if key in QUIET:
                self._emit(t("{member} changed {what} of {event}.", self.language, member=mention(member_id),
                             what=self._label(key), event=event))
            else:
                self._emit(t("{member} changed {what} of {event}: {before} → {after}.", self.language,
                             member=mention(member_id), what=self._label(key), event=event, before=old, after=new))

    @_safe
    async def uploaded(self, event_id: int, member_id: int, what: str, name: str | None = None) -> None:
        """A file or a pick: ``what`` is preset, image or mission (``name``: the preset's or the mission's)."""
        language, event, who = self.language, await self._event(event_id), mention(member_id)
        if what == "preset":
            text = t("{member} uploaded the preset {name} for {event}.", language, member=who, name=name or "—",
                     event=event)
        elif what == "mission":
            text = t("{member} set the mission of {event}: {name}.", language, member=who, name=name or "—", event=event)
        else:
            text = t("{member} changed the image of {event}.", language, member=who, event=event)
        self._emit(text)

    @_safe
    async def organiser(self, event_id: int, member_id: int, organiser_id: int, added: bool) -> None:
        if added:
            text = t("{member} made {organiser} a co-organiser of {event}.", self.language, member=mention(member_id),
                     organiser=mention(organiser_id), event=await self._event(event_id))
        else:
            text = t("{member} removed {organiser} from the co-organisers of {event}.", self.language,
                     member=mention(member_id), organiser=mention(organiser_id), event=await self._event(event_id))
        self._emit(text)

    @_safe
    async def profile(self, event_id: int, member_id: int, what: str) -> None:
        """``what``: settings (the server settings), keep or drop (the profile after the event), built."""
        language, event, who = self.language, await self._event(event_id), mention(member_id)
        if what == "settings":
            text = t("{member} changed the server settings of {event}.", language, member=who, event=event)
        elif what == "keep":
            text = t("{member} set the server profile of {event} to be kept.", language, member=who, event=event)
        elif what == "drop":
            text = t("{member} set the server profile of {event} to be deleted after the event.", language, member=who,
                     event=event)
        else:
            text = t("{member} started building the server profile of {event}.", language, member=who, event=event)
        self._emit(text)

    @_safe
    async def withdrew(self, event_id: int, member_id: int) -> None:
        self._emit(t("{member} withdrew {event} from approval; it is a draft again.", self.language,
                     member=mention(member_id), event=await self._event(event_id)))

    @_safe
    async def cancelled(self, event_id: int, member_id: int | None, reason: str, draft: bool) -> None:
        language, event, who = self.language, await self._event(event_id), self._who(member_id)
        if draft:
            text = t("{member} deleted the draft {event}. Reason: {reason}", language, member=who, event=event,
                     reason=reason or "—")
        else:
            text = t("{member} cancelled {event}. Reason: {reason}", language, member=who, event=event,
                     reason=reason or "—")
        self._emit(text)

    # ---------------------------------------------------------------- attendance and ratings

    @_safe
    async def corrected(self, event_id: int, member_id: int, member_ids: list[int], status: str) -> None:
        language = self.language
        label = {"attended": t("Attended", language), "no_show": t("No-show", language),
                 "excused": t("Excused", language)}.get(status, status)
        self._emit(t("{member} corrected the attendance of {event}: {members} → {status}.", language,
                     member=mention(member_id), event=await self._event(event_id), members=self._members(member_ids),
                     status=label))

    @_safe
    async def linked_probable(self, event_id: int, member_id: int, member_ids: list[int]) -> None:
        self._emit(t("{member} confirmed the probable Steam IDs in the attendance of {event}: {members}.",
                     self.language, member=mention(member_id), event=await self._event(event_id),
                     members=self._members(member_ids)))

    @_safe
    async def rated(self, event_id: int, member_id: int, again: bool) -> None:
        """Only that someone rated: never the stars or the comment."""
        if again:
            text = t("{member} changed their rating of {event}.", self.language, member=mention(member_id),
                     event=await self._event(event_id))
        else:
            text = t("{member} rated {event}.", self.language, member=mention(member_id),
                     event=await self._event(event_id))
        self._emit(text)
