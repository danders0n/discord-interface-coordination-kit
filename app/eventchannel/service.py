"""The event channel on Discord (docs/PLAN.md, *Event channel*, *Draft and publish*, *Notifications*, *Cancelling*).

- A draft gets a channel in the hidden drafts category, seen by its organisers, *manage any event* and *approve
  events*, with its discussion thread made at once (every ping goes there, never between the bot's messages).
- The bot's messages are redrawn from the database. Requests are **batched**: at most one redraw per event every
  ``interval`` seconds, however many sign-ups arrive at once. A redraw also follows a new name (date, host),
  renaming in the background because Discord allows two renames per ten minutes.
- Publishing moves the channel to the events category (its permissions, set by hand: members read, write in
  threads), keeps the organisers' right to post, and posts the announcement, which later edits keep in step.
- Cancelling marks the messages, pings the members signed up in the thread, edits the announcement and archives
  at once; a draft's channel is deleted instead. A published event is archived ``archive_hours`` after its end.
- The archive is a category per quarter of the event date (``archive_pattern``), made when missing, with an
  overflow ``(2)`` once one holds 50 channels; the channel is read-only there and its thread locked.

It is both notifiers: :class:`~app.events.service.EventNotifier` and :class:`~app.signup.service.Notifier`.
Pings for sign-up opening, reminders, stale drafts, offers and approval requests come in phase 4d."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

import discord
from sqlalchemy import select

from app.db import Database
from app.db.models import Event, Signup, Slot
from app.eventchannel import render, sync
from app.i18n import t
from app.resolve import Ids
from app.scheduler import Action, Scheduler
from app.settings import SettingsStore

ARCHIVE_KIND = "archive"
CATEGORY_LIMIT = 50
log = logging.getLogger(__name__)

BOT = discord.PermissionOverwrite(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                  create_public_threads=True, manage_threads=True, manage_channels=True,
                                  manage_roles=True, embed_links=True, attach_files=True, read_message_history=True)
ORGANISER = discord.PermissionOverwrite(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                        read_message_history=True, attach_files=True, embed_links=True)
HIDDEN = discord.PermissionOverwrite(view_channel=False)
READ_ONLY = discord.PermissionOverwrite(send_messages=False, send_messages_in_threads=False,
                                        create_public_threads=False, add_reactions=False)


def mention_chunks(text: str, member_ids: list[int], limit: int = 2000) -> list[str]:
    """``text`` and the members' mentions, split into messages Discord takes (docs/PLAN.md, *Notifications*)."""
    messages, current = [], text
    for member_id in member_ids:
        mention = f"<@{member_id}>"
        if len(current) + len(mention) + 1 > limit:
            messages.append(current)
            current = ""
        current = f"{current} {mention}".strip() if current else mention
    if current:
        messages.append(current)
    return messages


class Redrawer:
    """At most one redraw per event every ``interval`` seconds; requests in between are folded into it."""

    def __init__(self, draw: Callable[[int], Awaitable[None]], interval: float) -> None:
        self.draw, self.interval = draw, interval
        self._tasks: dict[int, asyncio.Task] = {}
        self._again: set[int] = set()

    def request(self, event_id: int) -> None:
        if event_id in self._tasks:
            self._again.add(event_id)
        else:
            self._tasks[event_id] = asyncio.create_task(self._run(event_id))

    async def _run(self, event_id: int) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                self._again.discard(event_id)
                try:
                    await self.draw(event_id)
                except Exception:  # noqa: BLE001 - one event's failure must not stop the others
                    log.exception("Redrawing event %s failed", event_id)
                if event_id not in self._again:
                    return
        finally:
            self._tasks.pop(event_id, None)

    async def settle(self) -> None:
        """Wait until nothing is pending (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)


class EventChannels:
    def __init__(self, db: Database, store: SettingsStore, ids: Callable[[], Ids], scheduler: Scheduler,
                 guild: Callable[[], Any], interval: float = 2.0, clock: Callable[[], datetime] | None = None) -> None:
        """``guild()``: the Discord server (None while not connected). ``events`` and ``signups`` are set after
        construction, as they take this object as their notifier."""
        self.db, self.store, self.ids, self.scheduler, self.guild = db, store, ids, scheduler, guild
        self.clock = clock or scheduler.clock
        self.events: Any = None
        self.signups: Any = None
        self.header: Any = None  # app.events.header.Header: the calendar follows every change
        self.staff: Any = None  # StaffLog
        self.threads: Any = None  # StaffThreads: the moderation thread
        self.services: Any = None  # for the approval messages
        self.roles_of: Callable[[int], frozenset[int] | None] = lambda _member: None
        self.redrawer = Redrawer(self.redraw_now, interval)
        self._renames: dict[int, asyncio.Task] = {}
        scheduler.register(ARCHIVE_KIND, self._archive_action)

    # ---------------------------------------------------------------- helpers

    @property
    def language(self) -> str:
        return self.store.current.language

    async def _event(self, event_id: int) -> Event:
        async with self.db.session() as session:
            return await session.get(Event, event_id)

    async def _save(self, event_id: int, **values: Any) -> None:
        async with self.db.session() as session, session.begin():
            event = await session.get(Event, event_id)
            for name, value in values.items():
                setattr(event, name, value)

    def _channel(self, event: Event) -> Any | None:
        guild = self.guild()
        return guild.get_channel(event.channel_id) if guild is not None and event.channel_id else None

    async def _topic(self, event: Event) -> str:
        local = event.assembly_at.astimezone(self.store.current.tz)
        return f"{event.title} · {render.kind_label(event.kind, self.language)} · {local:%d.%m %H:%M}"

    async def _context(self, event: Event) -> render.Context:
        host = None
        if event.host_group_id:
            from app.events.groups import Groups
            host = await Groups(self.db).get(event.host_group_id)
        guests = await self.events.guest_groups(event.id) if self.events else []
        async with self.db.session() as session:
            coming = len((await session.execute(select(Signup.id).where(
                Signup.event_id == event.id, Signup.status == "coming"))).all())
        booked = await self.events.reservations.get(event.id) if self.events else None
        return render.Context(self.language, self.clock(), host, guests, coming,
                              server_confirmed=booked is not None and booked.state == "booked")

    def _organiser_overwrites(self, guild: Any, event: Event, organisers: set[int]) -> dict:
        overwrites: dict = {guild.me: BOT}
        for member_id in {event.created_by, *organisers}:
            overwrites[discord.Object(id=member_id, type=discord.Member)] = ORGANISER
        ids = self.ids()
        for role_id in ids.permissions.get("manage_any_event", frozenset()) | ids.permissions.get("approve_events", frozenset()):
            overwrites[discord.Object(id=role_id, type=discord.Role)] = ORGANISER
        return overwrites

    async def _overwrites(self, guild: Any, event: Event) -> dict:
        """A draft: hidden from everyone else. Published: the category's, plus the organisers."""
        organisers = await self.events.organisers(event.id)
        if event.status in ("draft", "pending"):
            return {guild.default_role: HIDDEN} | self._organiser_overwrites(guild, event, organisers)
        category = guild.get_channel(self.ids().events_category)
        base = dict(category.overwrites) if category is not None else {}
        return base | self._organiser_overwrites(guild, event, organisers)

    # ---------------------------------------------------------------- the channel's life

    async def open_draft(self, event_id: int) -> Any:
        """The draft's channel, thread and messages. Returns the channel."""
        guild, event = self.guild(), await self._event(event_id)
        category = guild.get_channel(self.ids().drafts_category)
        channel = await guild.create_text_channel(await self.events.channel_name(event), category=category,
                                                  overwrites=await self._overwrites(guild, event),
                                                  topic=await self._topic(event))
        thread = await channel.create_thread(name=t("Discussion", self.language), type=discord.ChannelType.public_thread,
                                             auto_archive_duration=10080)
        await self._save(event_id, channel_id=channel.id, thread_id=thread.id)
        await self.redraw_now(event_id)
        await self.events.refresh_reservation(event_id)  # now it can link to the channel
        return channel

    def link(self, event: Event) -> str | None:
        guild = self.guild()
        if guild is None or not event.channel_id:
            return None
        return f"https://discord.com/channels/{guild.id}/{event.channel_id}"

    async def redraw_now(self, event_id: int) -> None:
        event = await self._event(event_id)
        channel = self._channel(event) if event is not None else None
        if channel is None:
            return
        board = await self.signups.board(event_id)
        ctx = await self._context(event)
        messages = render.messages(event, board, ctx)
        if event.status != "cancelled":
            from app.events.ui import PanelButton  # (the UI imports this module)
            view = discord.ui.View(timeout=None)
            view.add_item(PanelButton(event_id, t("Organiser panel", self.language)))
            messages["info"].view = view
            if event.status == "published" and "orbat:0" in messages:
                from app.signup.ui import buttons
                messages["orbat:0"].view = buttons(event_id, event.kind == "zeus", self.language)
        attached = dict(event.attached or {})
        for key, message in messages.items():  # upload a file again only when it changed
            message.replace_files = bool(message.files) and attached.get(key) != message.tag
        stored = await sync.apply(channel, {k: int(v) for k, v in (event.messages or {}).items()}, messages)
        tags = {key: message.tag for key, message in messages.items() if message.files}
        await self._save(event_id, messages=stored, attached=tags)
        await self._announcement(event, ctx)
        if event.archived_at is None and event.status != "cancelled":
            self._rename(event, channel, await self.events.channel_name(event), await self._topic(event))

    def _rename(self, event: Event, channel: Any, name: str, topic: str) -> None:
        """In the background, newest wins: a rename waits for Discord's limit without holding up anything."""
        if channel.name == name and getattr(channel, "topic", topic) == topic:
            return
        running = self._renames.get(event.id)
        if running is not None and not running.done():
            running.cancel()

        async def go() -> None:
            try:
                await channel.edit(name=name, topic=topic)
            except discord.HTTPException as exc:
                log.warning("Renaming #%s to %s failed: %s", channel.name, name, exc)

        self._renames[event.id] = asyncio.create_task(go())

    async def _announcement(self, event: Event, ctx: render.Context) -> None:
        if not event.announcement_id:
            return
        guild = self.guild()
        channel = guild.get_channel(self.ids().announcements) if guild else None
        if channel is None:
            return
        message = render.announcement(event, ctx, event.channel_id)
        try:
            await channel.get_partial_message(event.announcement_id).edit(embeds=message.embeds)
        except discord.NotFound:
            await self._save(event.id, announcement_id=None)

    async def publish(self, event_id: int) -> None:
        guild, event = self.guild(), await self._event(event_id)
        channel = self._channel(event)
        if channel is None:
            return
        await channel.edit(category=guild.get_channel(self.ids().events_category),
                           overwrites=await self._overwrites(guild, event))
        await self.redraw_now(event_id)
        ann = guild.get_channel(self.ids().announcements)
        if ann is not None:
            roles = [r for r in (self.ids().announcement, event.climate_role_id) if r]
            ctx = await self._context(event)
            message = render.announcement(event, ctx, event.channel_id)
            content = " ".join(f"<@&{r}>" for r in roles) or None
            sent = await ann.send(content=content, embeds=message.embeds,
                                  allowed_mentions=discord.AllowedMentions(roles=[discord.Object(r) for r in roles],
                                                                           users=False, everyone=False))
            await self._save(event_id, announcement_id=sent.id)
        times = self.store.current.times
        await self.scheduler.schedule(ARCHIVE_KIND, event.start_at + timedelta(
            minutes=event.duration_minutes) + timedelta(hours=times.archive_hours),
            key=f"{ARCHIVE_KIND}:{event_id}", event_id=event_id)

    async def delete(self, event_id: int) -> None:
        event = await self._event(event_id)
        channel = self._channel(event)
        if channel is not None:
            await channel.delete(reason="Draft cancelled")
        await self._save(event_id, channel_id=None, thread_id=None, messages={})

    async def cancel(self, event_id: int) -> None:
        event = await self._event(event_id)
        await self.redraw_now(event_id)
        thread = self.guild().get_thread(event.thread_id) if event.thread_id else None
        if thread is not None:
            text = t("{title} is cancelled: {reason}", self.language, title=event.title,
                     reason=event.cancel_reason or "—")
            for content in mention_chunks(text, await self._signed_up(event_id)):
                await thread.send(content, allowed_mentions=discord.AllowedMentions(users=True, roles=False,
                                                                                   everyone=False))
        await self.scheduler.cancel(f"{ARCHIVE_KIND}:{event_id}")
        await self.archive(event_id)

    async def _signed_up(self, event_id: int) -> list[int]:
        async with self.db.session() as session:
            holders = (await session.execute(select(Slot.holder_id).where(
                Slot.event_id == event_id, Slot.holder_id.is_not(None)))).scalars().all()
            coming = (await session.execute(select(Signup.member_id).where(
                Signup.event_id == event_id, Signup.status.in_(("coming", "reserve"))))).scalars().all()
        return list(dict.fromkeys([*holders, *coming]))

    # ---------------------------------------------------------------- archive

    async def archive(self, event_id: int) -> None:
        guild, event = self.guild(), await self._event(event_id)
        channel = self._channel(event)
        if channel is None or event.archived_at is not None:
            return
        category = await self._archive_category(guild, event)
        await channel.edit(category=category, overwrites=dict(category.overwrites) | {guild.me: BOT})
        thread = guild.get_thread(event.thread_id) if event.thread_id else None
        if thread is None and event.thread_id:
            try:
                thread = await guild.fetch_channel(event.thread_id)
            except discord.NotFound:
                thread = None
        if thread is not None:
            await thread.edit(archived=True, locked=True)
        await self._save(event_id, archived_at=self.clock())

    async def _archive_category(self, guild: Any, event: Event) -> Any:
        local = event.start_at.astimezone(self.store.current.tz)
        base = self.store.current.channels.archive_pattern.format(quarter=(local.month - 1) // 3 + 1, year=local.year)
        names = [base] + [f"{base} ({n})" for n in range(2, 20)]
        existing = {c.name: c for c in guild.categories}
        for name in names:
            category = existing.get(name)
            if category is None:
                overwrites = {guild.default_role: READ_ONLY, guild.me: BOT}
                return await guild.create_category(name, overwrites=overwrites, reason="Event archive")
            if len(category.channels) < CATEGORY_LIMIT:
                return category
        raise RuntimeError(f"No room left in the archive categories of {base}")

    async def _archive_action(self, action: Action, now: datetime) -> None:
        await self.archive(action.event_id)  # type: ignore[arg-type]

    # ---------------------------------------------------------------- start-up

    async def refresh_all(self) -> None:
        """After a restart: redraw every channel not archived yet (sign-up states, missed changes)."""
        async with self.db.session() as session:
            ids = (await session.execute(select(Event.id).where(Event.channel_id.is_not(None),
                                                                Event.archived_at.is_(None)))).scalars().all()
        for event_id in ids:
            self.redrawer.request(event_id)

    # ---------------------------------------------------------------- the notifiers

    def _calendar(self) -> None:
        if self.header is not None:
            self.header.request()

    async def changed(self, event_id: int) -> None:
        self.redrawer.request(event_id)
        self._calendar()

    async def published(self, event_id: int) -> None:
        await self.publish(event_id)
        self._calendar()

    async def deleted(self, event_id: int) -> None:
        await self.delete(event_id)
        self._calendar()

    async def cancelled(self, event_id: int) -> None:
        await self.cancel(event_id)
        self._calendar()

    async def signup_opened(self, event_id: int, ping: bool) -> None:
        self.redrawer.request(event_id)
        event = await self._event(event_id)
        roles = [r for r in (self.ids().announcement, event.climate_role_id) if r]
        if ping and roles:
            text = " ".join(f"<@&{r}>" for r in roles) + " " + t("Sign-up for {title} is open!", self.language,
                                                                  title=event.title)
            await self._thread_send(event, text, discord.AllowedMentions(roles=[discord.Object(r) for r in roles],
                                                                         users=False, everyone=False))

    async def reminder(self, event_id: int) -> None:
        event = await self._event(event_id)
        text = t("Reminder: {title}, assembly {when}.", self.language, title=event.title,
                 when=render.ts(event.assembly_at, "R"))
        await self._ping(event, text, await self._attending(event_id))

    async def stale(self, event_id: int) -> None:
        event = await self._event(event_id)
        cancel_at = event.assembly_at - timedelta(hours=self.store.current.times.stale_cancel_hours)
        text = t("{title} is still not published; it is cancelled {when} unless it is.", self.language,
                 title=event.title, when=render.ts(cancel_at, "R"))
        await self._ping(event, text, [event.created_by])
        self._staff(t("Still a draft: {title} ({channel}).", self.language, title=event.title,
                      channel=f"<#{event.channel_id}>" if event.channel_id else "—"))

    async def approval_requested(self, request_id: int) -> None:
        from app.approval.ui import request_message
        from app.db.models import ApprovalRequest

        if self.threads is None or self.services is None:
            return
        guild = self.guild()
        staff = guild.get_channel(self.ids().staff) if guild and self.ids().staff else None
        if staff is None:
            return
        async with self.db.session() as session:
            request = await session.get(ApprovalRequest, request_id)
        embed, view = await request_message(self.services, request)
        sent = await self.threads.send(guild, staff, "moderation", embed=embed, **({"view": view} if view else {}))
        async with self.db.session() as session, session.begin():
            (await session.get(ApprovalRequest, request_id)).message_id = sent.id

    async def refresh_requests(self, event_id: int) -> None:
        """Draw the event's pending requests again (Approve is on once the reservation succeeded)."""
        from app.db.models import ApprovalRequest

        async with self.db.session() as session:
            ids = (await session.execute(select(ApprovalRequest.id).where(
                ApprovalRequest.event_id == event_id, ApprovalRequest.state == "pending"))).scalars().all()
        for request_id in ids:
            await self._redraw_request(request_id)

    async def _redraw_request(self, request_id: int) -> Any:
        from app.approval.ui import request_message
        from app.db.models import ApprovalRequest

        async with self.db.session() as session:
            request = await session.get(ApprovalRequest, request_id)
        guild = self.guild()
        staff = guild.get_channel(self.ids().staff) if guild and self.ids().staff else None
        if request is None or request.message_id is None or staff is None or self.threads is None:
            return request
        thread = await self.threads.get(guild, staff, "moderation")
        embed, view = await request_message(self.services, request)
        try:
            await thread.get_partial_message(request.message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            pass
        return request

    async def approval_decided(self, request_id: int) -> None:
        request = await self._redraw_request(request_id)
        if request is None:
            return
        event = await self._event(request.event_id)
        if request.state == "approved":
            text = t("Approved: {title}.", self.language, title=event.title)
        else:
            text = t("Rejected: {title}. Reason: {reason}", self.language, title=event.title, reason=request.reason or "—")
        await self._ping(event, text, [request.requested_by])
        self._staff(f"{text} ({t('decided by {member}', self.language, member=f'<@{request.decided_by}>')})")
        self.redrawer.request(event.id)

    async def offered(self, offer: Any, member_ids: list[int]) -> None:
        from app.signup.ui import offer_buttons

        event = await self._event(offer.event_id)
        until = render.ts(offer.expires_at, "R")
        if offer.member_id is None:  # to the whole reserve list, in the thread
            text = t("A slot in {title} is free: the first to take it has it (until {until}).", self.language,
                     title=event.title, until=until)
            await self._ping(event, text, member_ids, view=offer_buttons(offer.id, self.language, everyone=True))
            return
        member = self.guild().get_member(offer.member_id) if self.guild() else None
        text = t("A slot in {title} is free for you. Take it before {until}?", self.language, title=event.title,
                 until=until)
        if member is not None:
            try:
                await member.send(text, view=offer_buttons(offer.id, self.language))
                return
            except discord.HTTPException:  # DMs closed: in the event's thread instead
                pass
        await self._ping(event, text, [offer.member_id], view=offer_buttons(offer.id, self.language))

    async def lost_slot(self, event_id: int, member_ids: list[int]) -> None:
        event = await self._event(event_id)
        await self._ping(event, t("The ORBAT of {title} changed and your slot is gone: you are at the top of the "
                                  "reserve list.", self.language, title=event.title), member_ids)

    # ---------------------------------------------------------------- pings

    async def _thread_send(self, event: Event, content: str, mentions: discord.AllowedMentions, **kwargs: Any) -> None:
        guild = self.guild()
        thread = guild.get_thread(event.thread_id) if guild is not None and event.thread_id else None
        if thread is None and guild is not None and event.thread_id:
            try:
                thread = await guild.fetch_channel(event.thread_id)
            except discord.NotFound:
                return
        if thread is not None:
            await thread.send(content, allowed_mentions=mentions, **kwargs)

    async def _ping(self, event: Event, text: str, member_ids: list[int], **kwargs: Any) -> None:
        """In the event's thread, split before Discord's 2000 characters; buttons go on the first message."""
        for index, content in enumerate(mention_chunks(text, member_ids)):
            await self._thread_send(event, content, discord.AllowedMentions(users=True, roles=False, everyone=False),
                                    **(kwargs if index == 0 else {}))

    async def _attending(self, event_id: int) -> list[int]:
        async with self.db.session() as session:
            holders = (await session.execute(select(Slot.holder_id).where(
                Slot.event_id == event_id, Slot.holder_id.is_not(None)))).scalars().all()
            coming = (await session.execute(select(Signup.member_id).where(
                Signup.event_id == event_id, Signup.status == "coming"))).scalars().all()
        return list(dict.fromkeys([*holders, *coming]))

    def _staff(self, text: str) -> None:
        if self.staff is not None:
            self.staff.post(text)
