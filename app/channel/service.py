"""The operation channels on Discord (docs/PLAN-front-end.md, *Sync and drawing*; how they look: docs/PLAN.md,
*Event channel*).

The feed (app/operations/feed.py) hands every changed operation to :meth:`OperationChannels.apply`, which keeps it
in ``operation_channel`` and asks for a redraw. A redraw brings Discord to what the operation says, every step safe
to repeat, so a redraw after a crash, a restart or the bot's own echo does no harm:

- An open operation (draft, pending, published) has a channel in the events category: the category's permissions
  plus its organisers (the author and co-organisers, as Discord ids) and the roles whose app role may manage or
  approve operations (from the supervisor's ``meta``), who may post. The channel's URL goes back to the supervisor
  (``PUT /api/operations/{id}/discord``), and the thread's once it exists.
- The messages are redrawn from the operation (app/channel/render.py, kept in order by app/channel/sync.py), in
  batches: at most one redraw per operation every ``interval`` seconds; an open operation's info carries *Organiser
  panel* (app/operations/ui.py). The name is the supervisor's
  ``channel_name``, renamed in the background (Discord allows two renames per ten minutes).
- Published: the discussion thread is made after the messages, so Discord's thread card is last; the announcement
  goes out once, mentioning the announcement role and the climate's role, and is edited on every change. Sign-up
  opening pings those roles in the thread (skipped more than an hour late); the start redraws (sign-up closes). The
  sign-up buttons sit on the last ORBAT message. Reminders ping the slot holders (or Coming) in the thread at
  ``times.reminder_minutes`` before the assembly, skipped once the assembly or a later reminder has come.
- Watchers (offers, the moderation thread) hear of every change the feed hands over, and of an operation gone.
- Cancelled: drawn as such, the announcement edited, archived at once. Over (ended, or past its end): archived
  ``archive_hours`` after the end. A deleted draft (a tombstone) loses its channel; an operation missing from a
  reset is archived if it was ever published, deleted otherwise.
- The archive is a category per quarter of the start (``archive_pattern``), made when missing, with an overflow
  ``(2)`` once one holds 50 channels; the channel is read-only there and its thread locked."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import discord
from sqlalchemy import select

from app.channel import order, render, sync
from app.db import Database
from app.db.models import OperationChannel
from app.i18n import t
from app.resolve import Ids
from app.scheduler import Action, Scheduler
from app.settings import SettingsStore
from app.attendance import ui as attendance_ui
from app.operations.ui import panel_view
from app.signup import ui as signup_ui
from app.supervisor_client import SupervisorClient, SupervisorError

ARCHIVE_KIND = "archive"
CLOSE_KIND = "signup_close"  # at the start: drawn again, the sign-up closed
SIGNUP_KIND = "signup_open"  # the sign-up opens: the roles pinged in the thread
DRAW_KIND = "draw_retry"  # a redraw that couldn't get the operation's files: tried again
RATING_KIND = "rating_close"  # the rating window closes: drawn again, without Rate (the supervisor writes no change)
REMINDER_KIND = "reminder"  # times.reminder_minutes before the assembly: the slot holders (or Coming) pinged
CATEGORY_LIMIT = 50
CATEGORY_WARNING = 45  # the staff log hears from here on (docs/PLAN.md, *Event channel*, *Where*)
PING_LATE = timedelta(hours=1)  # a sign-up ping later than this is skipped
RETRY = timedelta(minutes=1)
log = logging.getLogger(__name__)

BOT = discord.PermissionOverwrite(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                  create_public_threads=True, manage_threads=True, manage_channels=True,
                                  manage_roles=True, embed_links=True, attach_files=True, read_message_history=True)
ORGANISER = discord.PermissionOverwrite(view_channel=True, send_messages=True, send_messages_in_threads=True,
                                        read_message_history=True, attach_files=True, embed_links=True)
READ_ONLY = discord.PermissionOverwrite(send_messages=False, send_messages_in_threads=False,
                                        create_public_threads=False, add_reactions=False)


class Redrawer:
    """At most one redraw per key every ``interval`` seconds; requests in between are folded into it."""

    def __init__(self, draw: Callable[[int], Awaitable[None]], interval: float) -> None:
        self.draw, self.interval = draw, interval
        self._tasks: dict[int, asyncio.Task] = {}
        self._again: set[int] = set()

    def request(self, key: int) -> None:
        if key in self._tasks:
            self._again.add(key)
        else:
            self._tasks[key] = asyncio.create_task(self._run(key))

    async def _run(self, key: int) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                self._again.discard(key)
                try:
                    await self.draw(key)
                except Exception:  # noqa: BLE001 - one operation's failure must not stop the others
                    log.exception("Redrawing %s failed", key)
                if key not in self._again:
                    return
        finally:
            self._tasks.pop(key, None)

    async def settle(self) -> None:
        """Wait until nothing is pending (tests, shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)


def _id(value: Any) -> int | None:
    text = str(value or "")
    return int(text) if text.isdigit() else None


def mention_chunks(text: str, member_ids: list[int], limit: int = render.MAX_CONTENT) -> list[str]:
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


def attending(op: dict[str, Any]) -> list[int]:
    """The Discord ids of the slot holders and of those Coming (whom a reminder pings)."""
    entries = [slot.get("holder") for squad in op.get("orbat") or [] for slot in squad.get("slots") or []]
    entries += (op.get("signups") or {}).get("coming") or []
    return list(dict.fromkeys(i for i in (_id((e or {}).get("discord_id")) for e in entries) if i is not None))


class OperationChannels:
    def __init__(self, db: Database, store: SettingsStore, ids: Callable[[], Ids], scheduler: Scheduler,
                 guild: Callable[[], Any], client: SupervisorClient, interval: float = 2.0,
                 clock: Callable[[], datetime] | None = None) -> None:
        """``guild()``: the Discord server (None while not connected)."""
        self.db, self.store, self.ids, self.scheduler, self.guild = db, store, ids, scheduler, guild
        self.client = client
        self.clock = clock or scheduler.clock
        self.meta: dict[str, Any] = {}  # the supervisor's GET /api/operations/meta, kept by the feed
        self.header: Any = None  # app.operations.header.Header: the calendar follows every change
        self.staff: Any = None  # StaffLog
        self.redrawer = Redrawer(self.draw_now, interval)
        self.sorter = Redrawer(self._sort, interval)  # by category id: the channels in date order
        self._renames: dict[int, asyncio.Task] = {}
        self._drawing: dict[int, asyncio.Lock] = {}  # one redraw per operation at a time
        self._overwrites_due: set[int] = set()  # organisers changed (or a restart): the overwrites follow
        # What else follows an operation's changes (the offers' DMs, the moderation thread): objects with
        # ``async changed(op)`` and ``async gone(op_id)``, told after the operation is kept.
        self.watchers: list[Any] = []
        scheduler.register(ARCHIVE_KIND, self._archive_action)
        scheduler.register(CLOSE_KIND, self._redraw_action)
        scheduler.register(SIGNUP_KIND, self._signup_action)
        scheduler.register(DRAW_KIND, self._redraw_action)
        scheduler.register(RATING_KIND, self._redraw_action)
        scheduler.register(REMINDER_KIND, self._reminder_action)

    # ---------------------------------------------------------------- helpers

    @property
    def language(self) -> str:
        return self.store.current.language

    async def row(self, op_id: int) -> OperationChannel | None:
        async with self.db.session() as session:
            return await session.get(OperationChannel, op_id)

    async def _save(self, op_id: int, **values: Any) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(OperationChannel, op_id)
            for name, value in values.items():
                setattr(row, name, value)

    def _channel(self, row: OperationChannel) -> Any | None:
        guild = self.guild()
        return guild.get_channel(row.channel_id) if guild is not None and row.channel_id else None

    def _connected(self) -> Any:
        """The guild, for work that must not pass for done without it: raises, so an action is tried again."""
        guild = self.guild()
        if guild is None:
            raise RuntimeError("Not connected to the Discord server")
        return guild

    def _staff(self, text: str) -> None:
        if self.staff is not None:
            self.staff.post(text)

    def _calendar(self) -> None:
        if self.header is not None:
            self.header.request()

    def link(self, channel_id: int | None) -> str | None:
        guild = self.guild()
        return f"https://discord.com/channels/{guild.id}/{channel_id}" if guild is not None and channel_id else None

    # ---------------------------------------------------------------- what the feed hands over

    def _over(self, op: dict[str, Any]) -> bool:
        """Past the time its channel would be archived anyway."""
        ends = render.when(op, "ends_at")
        return ends is not None and ends + timedelta(hours=self.store.current.times.archive_hours) <= self.clock()

    async def apply(self, op: dict[str, Any]) -> None:
        """An operation as it is now: kept, and drawn shortly. One the bot never drew that is closed or over is
        left alone (an old operation listed by a first read of the feed)."""
        op_id = int(op["id"])
        async with self.db.session() as session, session.begin():
            row = await session.get(OperationChannel, op_id)
            if row is None:
                if op["status"] not in render.OPEN or self._over(op):
                    return
                row = OperationChannel(operation_id=op_id, messages={}, attached={}, operation={})
                session.add(row)
            if render.organisers(row.operation or {}) != render.organisers(op) or not row.operation:
                self._overwrites_due.add(op_id)
            row.operation, row.change_seq = op, int(op.get("change_seq") or 0)
            row.title, row.status, row.start_at = op["title"][:100], op["status"], render.when(op, "start_at")
            row.gone_at = None
        self.redrawer.request(op_id)
        self._calendar()
        await self._tell("changed", op)

    async def gone(self, op_id: int) -> None:
        """Deleted in the supervisor (a tombstone, or missing from a reset): the channel goes."""
        async with self.db.session() as session, session.begin():
            row = await session.get(OperationChannel, op_id)
            if row is None or row.gone_at is not None:
                return
            row.gone_at = self.clock()
        self.redrawer.request(op_id)
        self._calendar()
        await self._tell("gone", op_id)

    async def _tell(self, what: str, value: Any) -> None:
        """The watchers; one that fails is logged and the feed goes on (what they draw is redrawn next time)."""
        for watcher in self.watchers:
            try:
                await getattr(watcher, what)(value)
            except Exception:  # noqa: BLE001
                log.exception("%s.%s failed", type(watcher).__name__, what)

    async def by_channel(self, channel_id: int | None) -> OperationChannel | None:
        """The operation whose channel or thread this is (``/slot`` typed there)."""
        if not channel_id:
            return None
        async with self.db.session() as session:
            return (await session.execute(select(OperationChannel).where(
                (OperationChannel.channel_id == channel_id) | (OperationChannel.thread_id == channel_id)))).scalars().first()

    async def known(self) -> set[int]:
        """The operations the bot still draws (for a reset: those missing from it are gone)."""
        async with self.db.session() as session:
            return set((await session.execute(select(OperationChannel.operation_id).where(
                OperationChannel.gone_at.is_(None), OperationChannel.archived_at.is_(None)))).scalars())

    # ---------------------------------------------------------------- category room and order

    def crowded(self) -> list[tuple[str, int]]:
        guild, category_id = self.guild(), self.ids().events_category
        category = guild.get_channel(category_id) if guild is not None and category_id else None
        return [(category.name, len(category.channels))] if category is not None else []

    def _warn_room(self) -> None:
        for name, count in self.crowded():
            if count >= CATEGORY_WARNING:
                self._staff(t("The category {category} holds {count} of {limit} channels.", self.language,
                              category=name, count=count, limit=CATEGORY_LIMIT))

    def _sort_later(self, *category_ids: int | None) -> None:
        for category_id in category_ids:
            if category_id:
                self.sorter.request(category_id)

    async def _sort(self, category_id: int) -> None:
        guild = self.guild()
        category = guild.get_channel(category_id) if guild is not None else None
        if category is None:
            return
        async with self.db.session() as session:
            rows = (await session.execute(select(OperationChannel.channel_id, OperationChannel.start_at,
                                                 OperationChannel.operation_id).where(
                OperationChannel.channel_id.in_([c.id for c in category.channels])))).all()
        ids = self.ids()
        top = ids.top if category_id == ids.events_category else ()
        starts = {channel: (start, op_id) for channel, start, op_id in rows if start is not None}
        try:
            await order.apply(guild, category, top, starts)
        except discord.HTTPException as exc:
            log.warning("Putting %s in date order failed: %s", category.name, exc)

    # ---------------------------------------------------------------- drawing

    def _lock(self, op_id: int) -> asyncio.Lock:
        return self._drawing.setdefault(op_id, asyncio.Lock())

    async def draw_now(self, op_id: int) -> None:
        async with self._lock(op_id):
            await self._draw(op_id)

    async def _draw(self, op_id: int) -> None:
        row = await self.row(op_id)
        if row is None or row.exported_at is not None:
            return
        guild = self._connected()
        if row.gone_at is not None:
            await self._remove(row)
            return
        op, channel = row.operation, self._channel(row)
        if channel is None:
            if row.archived_at is not None or op["status"] not in render.OPEN:
                return
            channel = await self._open(guild, row)
            row = await self.row(op_id)
        if op_id in self._overwrites_due and self.meta and row.archived_at is None:  # (meta: the staff roles)
            self._overwrites_due.discard(op_id)
            await channel.edit(overwrites=self._overwrites(guild, op))
        try:
            files = await self._files(op)
        except SupervisorError as exc:  # the next try redraws; what is posted stays meanwhile
            log.warning("The files of OP-%s couldn't be read (%s): drawn again in a minute", op_id, exc.message)
            await self.scheduler.schedule(DRAW_KIND, self.clock() + RETRY, key=f"{DRAW_KIND}:{op_id}",
                                          operation_id=op_id)
            return
        await self._messages(row, channel, files)
        row = await self.row(op_id)
        if op["status"] == "published" and not row.thread_id:
            await self._open_thread(row, channel)
            row = await self.row(op_id)
        await self._plumbing(row)
        await self._announce(guild, row)
        if row.archived_at is None and op["status"] in render.OPEN:
            self._rename(op_id, channel, op["channel_name"], render.topic(op, self.store.current.tz))
        self._sort_later(channel.category_id)
        await self._schedule(row)

    async def _open(self, guild: Any, row: OperationChannel) -> Any:
        """The channel of an open operation, in the events category."""
        op = row.operation
        category = guild.get_channel(self.ids().events_category)
        channel = await guild.create_text_channel(op["channel_name"], category=category,
                                                  overwrites=self._overwrites(guild, op),
                                                  topic=render.topic(op, self.store.current.tz))
        await self._save(row.operation_id, channel_id=channel.id, category_id=channel.category_id, thread_id=None,
                         messages={}, attached={})
        if self.meta:  # (else the staff roles join once the feed has read them)
            self._overwrites_due.discard(row.operation_id)
        self._warn_room()
        return channel

    def _overwrites(self, guild: Any, op: dict[str, Any]) -> dict:
        """The events category's (members read, don't write), plus the organisers and the staff who may post."""
        category = guild.get_channel(self.ids().events_category)
        found: dict = dict(category.overwrites) if category is not None else {}
        found[guild.me] = BOT
        for member_id in render.organisers(op):
            found[discord.Object(id=member_id, type=discord.Member)] = ORGANISER
        staff = self.meta.get("staff_roles") or {}
        for role_id in {_id(r) for r in [*(staff.get("manage") or []), *(staff.get("approve") or [])]} - {None}:
            found[discord.Object(id=role_id, type=discord.Role)] = ORGANISER
        return found

    async def _files(self, op: dict[str, Any]) -> dict[str, Path]:
        """The operation's files on disk (``<data>/files/<operation>/<hash>/<name>``), fetched once per content;
        older versions are deleted. Raises SupervisorError when one can't be fetched."""
        folder = self.store.current.data_dir / "files" / str(op["id"])
        found, keep = {}, set()
        for slot, info in (op.get("files") or {}).items():
            if not isinstance(info, dict) or not info.get("file") or not info.get("hash"):
                continue
            name = Path(info.get("original") if slot == "preset" and info.get("original") else info["file"]).name
            path = folder / str(info["hash"])[:16] / name
            if not path.is_file():
                content = await self.client.operation_file(int(op["id"]), info["file"])
                await asyncio.to_thread(_write, path, content)
            found[slot], keep = path, keep | {path.parent.name}
        if folder.is_dir():
            for old in folder.iterdir():
                if old.is_dir() and old.name not in keep:
                    await asyncio.to_thread(_remove_tree, old)
        return found

    async def _messages(self, row: OperationChannel, channel: Any, files: dict[str, Path]) -> None:
        ctx = render.Context(self.language, self.clock(), files, row.thread_id)
        messages = render.messages(row.operation, ctx)
        if row.operation["status"] in render.OPEN:  # the organisers' panel under the info
            messages["info"].view = panel_view(row.operation_id, self.language)
        pages = [key for key in messages if key.startswith("orbat:")]
        if pages:  # the sign-up buttons under the last ORBAT message (None: removed, as on a cancelled operation)
            messages[pages[-1]].view = signup_ui.buttons(row.operation, self.language, ctx.now)
        if "report" in messages:  # Rate (while open) and Edit attendance under the report
            messages["report"].view = attendance_ui.report_buttons(row.operation, self.language, ctx.now)
        attached = dict(row.attached or {})
        for key, message in messages.items():  # upload a file again only when it changed
            message.replace_files = bool(message.files) and attached.get(key) != message.tag
        try:
            stored = await sync.apply(channel, {k: int(v) for k, v in (row.messages or {}).items()}, messages)
        except sync.Partial as exc:  # what was sent is kept, so the next redraw edits it rather than sending again
            await self._save(row.operation_id, messages=exc.stored)
            raise exc.__cause__ from None
        tags = {key: message.tag for key, message in messages.items() if message.files}
        await self._save(row.operation_id, messages=stored, attached=tags)

    async def _open_thread(self, row: OperationChannel, channel: Any) -> None:
        """The discussion thread, made after the messages: Discord's "started a thread" card is last and stands in
        for the link (decided 2026-11-02)."""
        thread = await channel.create_thread(
            name=self.store.current.events.discussion or t("Discussion", self.language),
            type=discord.ChannelType.public_thread, auto_archive_duration=10080)
        messages = dict(row.messages or {})
        try:
            async for message in channel.history(limit=5):
                if message.type == discord.MessageType.thread_created:
                    messages[sync.CARD] = message.id
                    break
        except discord.HTTPException as exc:
            log.warning("Finding the thread card in #%s failed: %s", channel.name, exc)
        await self._save(row.operation_id, thread_id=thread.id, messages=messages)
        if sync.CARD not in messages:
            self.redrawer.request(row.operation_id)  # the link instead

    async def _plumbing(self, row: OperationChannel) -> None:
        """The channel's and the thread's URLs in the supervisor, when they differ (the app links to them)."""
        wanted = {"channel_url": self.link(row.channel_id), "thread_url": self.link(row.thread_id)}
        have = row.operation.get("discord") or {}
        changed = {k: v for k, v in wanted.items() if v is not None and have.get(k) != v}
        if not changed:
            return
        try:
            await self.client.set_operation_discord(row.operation_id, **changed)
        except SupervisorError as exc:  # every redraw tries again; the feed's echo is one
            log.warning("Telling the supervisor where OP-%s is on Discord failed: %s", row.operation_id, exc.message)

    def _rename(self, op_id: int, channel: Any, name: str, topic: str) -> None:
        """In the background, newest wins: a rename waits for Discord's limit without holding up anything."""
        if channel.name == name and getattr(channel, "topic", topic) == topic:
            return
        running = self._renames.get(op_id)
        if running is not None and not running.done():
            running.cancel()

        async def go() -> None:
            try:
                await channel.edit(name=name, topic=topic)
            except discord.HTTPException as exc:
                log.warning("Renaming #%s to %s failed: %s", channel.name, name, exc)

        self._renames[op_id] = asyncio.create_task(go())

    async def _announce(self, guild: Any, row: OperationChannel) -> None:
        """Published: the announcement once, with the announcement and climate roles; then kept in step."""
        op = row.operation
        channel = guild.get_channel(self.ids().announcements) if self.ids().announcements else None
        if channel is None:
            return
        message = render.announcement(op, self.language, row.channel_id)
        if row.announcement_id:
            try:
                await channel.get_partial_message(row.announcement_id).edit(embeds=message.embeds)
            except discord.NotFound:
                await self._save(row.operation_id, announcement_id=None)
            return
        if op["status"] != "published":
            return
        roles = self._roles(op)
        content = " ".join(f"<@&{r}>" for r in roles) or None
        sent = await channel.send(content=content, embeds=message.embeds, allowed_mentions=discord.AllowedMentions(
            roles=[discord.Object(r) for r in roles], users=False, everyone=False))
        await self._save(row.operation_id, announcement_id=sent.id)

    def _roles(self, op: dict[str, Any]) -> list[int]:
        return [r for r in dict.fromkeys((self.ids().announcement, _id(op.get("climate_role_id")))) if r]

    async def _schedule(self, row: OperationChannel) -> None:
        """The operation's own actions, recomputed on every redraw: sign-up opening and closing, the archive."""
        op, now, op_id = row.operation, self.clock(), row.operation_id
        start, opens, ends = render.when(op, "start_at"), render.when(op, "signup_opens_at"), render.when(op, "ends_at")
        published = op["status"] == "published"
        if published and start is not None and start > now:
            await self.scheduler.schedule(CLOSE_KIND, start, key=f"{CLOSE_KIND}:{op_id}", operation_id=op_id)
        elif not published:
            await self.scheduler.cancel(f"{CLOSE_KIND}:{op_id}")
        if published and opens is not None and opens > now:
            await self.scheduler.schedule(SIGNUP_KIND, opens, key=f"{SIGNUP_KIND}:{op_id}", operation_id=op_id,
                                          payload={"at": op["signup_opens_at"]})
        elif not published:
            await self.scheduler.cancel(f"{SIGNUP_KIND}:{op_id}")
        await self._schedule_reminders(op, published, now)
        closes = render.when(op.get("rating") or {}, "closes_at")
        if closes is not None and closes > now:
            await self.scheduler.schedule(RATING_KIND, closes, key=f"{RATING_KIND}:{op_id}", operation_id=op_id)
        if row.archived_at is not None:
            return
        if op["status"] == "cancelled":
            await self.archive(op_id)
        elif op["status"] in ("published", "ended") and ends is not None:
            await self.scheduler.schedule(ARCHIVE_KIND, ends + timedelta(hours=self.store.current.times.archive_hours),
                                          key=f"{ARCHIVE_KIND}:{op_id}", operation_id=op_id)

    async def _schedule_reminders(self, op: dict[str, Any], published: bool, now: datetime) -> None:
        """At the configured offsets before the assembly, while ahead (one already due runs, late or not, and its
        handler decides); one for an offset /reload removed is dropped, all of them when no longer published."""
        op_id, offsets = int(op["id"]), self.store.current.times.reminder_minutes
        keys = {minutes: f"{REMINDER_KIND}:{op_id}:{minutes}" for minutes in offsets}
        await self.scheduler.cancel_all(REMINDER_KIND, op_id, keep=set(keys.values()) if published else set())
        assembly = render.when(op, "assembly_at")
        if not published or assembly is None:
            return
        for minutes, key in keys.items():
            due = assembly - timedelta(minutes=minutes)
            if due > now:
                await self.scheduler.schedule(REMINDER_KIND, due, key=key, operation_id=op_id,
                                              payload={"minutes": minutes})

    async def _remove(self, row: OperationChannel) -> None:
        """Gone in the supervisor: a channel never published is deleted, one that was is archived."""
        channel = self._channel(row)
        await self.scheduler.cancel_all(REMINDER_KIND, row.operation_id)
        for kind in (ARCHIVE_KIND, CLOSE_KIND, SIGNUP_KIND, DRAW_KIND, RATING_KIND):
            await self.scheduler.cancel(f"{kind}:{row.operation_id}")
        if channel is not None and row.thread_id and row.archived_at is None:
            await self.archive(row.operation_id)
        elif channel is not None and row.archived_at is None:
            await channel.delete(reason="Draft deleted")
            await self._save(row.operation_id, channel_id=None, thread_id=None, messages={})

    # ---------------------------------------------------------------- archive

    async def archive(self, op_id: int) -> None:
        guild, row = self._connected(), await self.row(op_id)
        channel = self._channel(row) if row is not None else None
        if channel is None or row.archived_at is not None:
            return
        category = await self._archive_category(guild, row)
        await channel.edit(category=category, overwrites=dict(category.overwrites) | {guild.me: BOT})
        self._sort_later(category.id)
        thread = guild.get_thread(row.thread_id) if row.thread_id else None
        if thread is None and row.thread_id:
            try:
                thread = await guild.fetch_channel(row.thread_id)
            except discord.NotFound:
                thread = None
        if thread is not None:
            await thread.edit(archived=True, locked=True)
        await self._save(op_id, archived_at=self.clock(), category_id=category.id)
        await self.scheduler.cancel(f"{ARCHIVE_KIND}:{op_id}")

    async def _archive_category(self, guild: Any, row: OperationChannel) -> Any:
        local = (row.start_at or self.clock()).astimezone(self.store.current.tz)
        base = self.store.current.channels.archive_pattern.format(quarter=(local.month - 1) // 3 + 1, year=local.year)
        names = [base] + [f"{base} ({n})" for n in range(2, 20)]
        existing = {c.name: c for c in guild.categories}
        for name in names:
            category = existing.get(name)
            if category is None:
                return await guild.create_category(name, overwrites=self._archive_overwrites(guild),
                                                   reason="Operation archive")
            if len(category.channels) < CATEGORY_LIMIT:
                return category
        raise RuntimeError(f"No room left in the archive categories of {base}")

    def _archive_overwrites(self, guild: Any) -> dict:
        """The events category's (who sees the operations sees their archive, nobody else), every one read-only."""
        events = guild.get_channel(self.ids().events_category)
        found: dict = {}
        for target, overwrite in dict(getattr(events, "overwrites", None) or {}).items():
            allow, deny = overwrite.pair()
            found[target] = discord.PermissionOverwrite.from_pair(allow, deny)
        found.setdefault(guild.default_role, discord.PermissionOverwrite())
        for overwrite in found.values():
            overwrite.update(**{name: False for name, value in READ_ONLY if value is False})
        return found | {guild.me: BOT}

    # ---------------------------------------------------------------- actions

    async def _archive_action(self, action: Action, now: datetime) -> None:
        await self.archive(action.operation_id)  # type: ignore[arg-type]  (raises without the guild: retried)

    async def _redraw_action(self, action: Action, now: datetime) -> None:
        self._connected()
        self.redrawer.request(action.operation_id)  # type: ignore[arg-type]
        self._calendar()

    async def _signup_action(self, action: Action, now: datetime) -> None:
        """Sign-up opens: the roles in the thread, unless more than an hour late or the operation changed."""
        guild = self._connected()
        self.redrawer.request(action.operation_id)  # type: ignore[arg-type]
        row = await self.row(action.operation_id)  # type: ignore[arg-type]
        if row is None or action.late(now) > PING_LATE:
            return
        op = row.operation
        if op.get("status") != "published" or op.get("signup_opens_at") != action.payload.get("at"):
            return
        roles = self._roles(op)
        thread = await self._thread(guild, row)
        if not roles or thread is None:
            return
        text = " ".join(f"<@&{r}>" for r in roles) + " " + t("Sign-up for {title} is open!", self.language,
                                                              title=op["title"])
        await thread.send(text, allowed_mentions=discord.AllowedMentions(
            roles=[discord.Object(r) for r in roles], users=False, everyone=False))

    async def _reminder_action(self, action: Action, now: datetime) -> None:
        """The slot holders (or Coming) in the thread; skipped once the assembly or a later reminder has come."""
        self._connected()
        row = await self.row(action.operation_id)  # type: ignore[arg-type]
        op = row.operation if row is not None else {}
        assembly = render.when(op, "assembly_at") if op else None
        if op.get("status") != "published" or assembly is None or now >= assembly:
            return
        later = [m for m in self.store.current.times.reminder_minutes if m < int(action.payload.get("minutes", 0))]
        if any(now >= assembly - timedelta(minutes=m) for m in later):
            return  # a later reminder is due already: that one speaks
        members = attending(op)
        if members:
            await self.ping(row.operation_id, t("Reminder: {title}, assembly {when}.", self.language,
                                                title=op["title"], when=render.ts(assembly, "R")), members)

    async def ping(self, op_id: int, text: str, member_ids: list[int], view: discord.ui.View | None = None) -> Any:
        """``text`` and the members' mentions in the operation's thread, split before Discord's 2000 characters; a
        view goes on the first message, which is returned. An archived thread (a cancelled operation) is opened for
        it and closed again. Without a thread (a draft): by DM, and None is returned."""
        guild = self._connected()
        row = await self.row(op_id)
        thread = await self._thread(guild, row) if row is not None else None
        if thread is None:
            for member_id in dict.fromkeys(member_ids):
                member = guild.get_member(member_id)
                try:
                    if member is not None:
                        await member.send(text, **({"view": view} if view is not None else {}))
                except discord.HTTPException as exc:
                    log.info("A DM to %s about OP-%s failed: %s", member_id, op_id, exc)
            return None
        reopened = bool(getattr(thread, "archived", False))
        if reopened:
            await thread.edit(archived=False)
        first = None
        try:
            for index, content in enumerate(mention_chunks(text, list(dict.fromkeys(member_ids)))):
                extra = {"view": view} if view is not None and index == 0 else {}
                sent = await thread.send(content, allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False), **extra)
                first = first or sent
        finally:
            if reopened:
                await thread.edit(archived=True, locked=True)
        return first

    async def _thread(self, guild: Any, row: OperationChannel) -> Any | None:
        thread = guild.get_thread(row.thread_id) if row.thread_id else None
        if thread is None and row.thread_id:
            try:
                thread = await guild.fetch_channel(row.thread_id)
            except discord.NotFound:
                return None
        return thread

    # ---------------------------------------------------------------- start-up

    async def refresh_all(self) -> None:
        """After a restart: redraw every channel still drawn (missed changes, sign-up states), with its overwrites,
        and put the events category in order (channels moved by hand)."""
        self._sort_later(self.ids().events_category)
        async with self.db.session() as session:
            ids = (await session.execute(select(OperationChannel.operation_id).where(
                OperationChannel.archived_at.is_(None), OperationChannel.exported_at.is_(None)))).scalars().all()
        for op_id in ids:
            self._overwrites_due.add(op_id)
            self.redrawer.request(op_id)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_bytes(data)
    temporary.replace(path)


def _remove_tree(folder: Path) -> None:
    for item in folder.iterdir():
        item.unlink(missing_ok=True)
    folder.rmdir()
