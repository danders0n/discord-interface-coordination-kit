"""The moderation thread and the approval notices (docs/PLAN-front-end.md, *Notices*): requests posted with their
buttons, decisions drawn and told, stale drafts.

- ``approval_requested`` / ``proposal_requested``: the request message in the staff channel's moderation thread; its
  id is kept in ``moderation_message``.
- ``approval_decided`` / ``proposal_decided``: the message drawn decided (no buttons); whoever asked is told in the
  operation's thread (a draft has none: by DM), and the staff log hears it.
- ``stale_draft``: the organisers told (by DM for a draft), a staff-log note; a pending operation's request posted
  again, so it is seen.
- The feed (a watcher of the channels) keeps a pending request's message current (blocked or not) and draws one
  that was withdrawn, replaced by a newer one or whose operation was deleted; drawn final, the row goes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import discord
from sqlalchemy import select

from app.approval.ui import asked, request_message
from app.channel import render
from app.db.models import ModerationMessage
from app.i18n import t
from app.notices.service import Later, Outcome, delivered, failed

log = logging.getLogger(__name__)


def _discord_ids(values: list[Any]) -> list[int]:
    return [int(v) for v in values if str(v).isdigit()]


class Moderation:
    def __init__(self, services: Any, guild: Callable[[], Any]) -> None:
        self.s, self.guild = services, guild
        self._drawn: dict[int, str] = {}  # request id -> what its message shows (no edit when nothing changed)

    @property
    def language(self) -> str:
        return self.s.settings.language

    def _connected(self) -> Any:
        guild = self.guild()
        if guild is None:
            raise Later("not connected to the server yet")
        return guild

    async def _thread(self, guild: Any) -> Any | None:
        staff = guild.get_channel(self.s.ids.staff) if self.s.ids.staff else None
        return await self.s.threads.get(guild, staff, "moderation") if staff is not None else None

    async def _context(self, op_id: int) -> tuple[int | None, str | None]:
        """The operation's channel and kind name, from what the bot draws."""
        row = await self.s.channels.row(op_id)
        if row is None:
            return None, None
        return row.channel_id, (row.operation or {}).get("kind_name")

    async def _row(self, request_id: int) -> ModerationMessage | None:
        async with self.s.db.session() as session:
            return await session.get(ModerationMessage, request_id)

    # ---------------------------------------------------------------- posting and drawing

    async def post(self, request: dict[str, Any], op_id: int, again: bool = False) -> bool:
        """The request in the moderation thread (``again``: posted anew, the older message without buttons).
        A message posted before is edited instead. False without a staff channel."""
        guild = self._connected()
        thread = await self._thread(guild)
        if thread is None:
            return False
        request_id = int(request["id"])
        row = await self._row(request_id)
        if row is not None and not again:
            await self.draw(request, op_id)
            return True
        channel_id, kind_name = await self._context(op_id)
        embed, view = request_message(request, self.language, channel_id, kind_name)
        sent = await self.s.threads.send(guild, guild.get_channel(self.s.ids.staff), "moderation", embed=embed,
                                         **({"view": view} if view is not None else {}))
        if row is not None:  # the older message: the same request, without buttons
            await self._edit(thread, row.message_id, embed, None)
        async with self.s.db.session() as session, session.begin():
            found = await session.get(ModerationMessage, request_id)
            if found is None:
                session.add(ModerationMessage(request_id=request_id, operation_id=op_id, message_id=sent.id))
            else:
                found.message_id = sent.id
        self._drawn[request_id] = repr((request.get("state"), request.get("blocked")))
        return True

    async def draw(self, request: dict[str, Any], op_id: int, state: str | None = None) -> None:
        """The message as the request is now (``state``: ``withdrawn`` when it is no longer listed); once final,
        the row goes."""
        request_id = int(request["id"])
        row = await self._row(request_id)
        guild = self.guild()
        if row is None or guild is None:
            return
        state = state or request.get("state") or "pending"
        drawn = repr((state, request.get("blocked")))
        if self._drawn.get(request_id) != drawn:
            thread = await self._thread(guild)
            if thread is None:
                return
            channel_id, kind_name = await self._context(op_id)
            embed, view = request_message(request, self.language, channel_id, kind_name, state)
            if not await self._edit(thread, row.message_id, embed, view):
                return
            self._drawn[request_id] = drawn
        if state != "pending":
            async with self.s.db.session() as session, session.begin():
                found = await session.get(ModerationMessage, request_id)
                if found is not None:
                    await session.delete(found)
            self._drawn.pop(request_id, None)

    @staticmethod
    async def _edit(thread: Any, message_id: int, embed: discord.Embed, view: discord.ui.View | None) -> bool:
        try:
            await thread.get_partial_message(message_id).edit(embed=embed, view=view)
        except discord.NotFound:
            return True  # deleted by hand: nothing left to draw
        except discord.HTTPException as exc:
            log.info("Editing a moderation message failed: %s", exc)
            return False
        return True

    # ---------------------------------------------------------------- the feed's watcher

    async def changed(self, op: dict[str, Any]) -> None:
        op_id = int(op["id"])
        async with self.s.db.session() as session:
            rows = list((await session.execute(select(ModerationMessage).where(
                ModerationMessage.operation_id == op_id))).scalars())
        listed = {int(r["id"]): r for r in op.get("requests") or [] if str(r.get("id", "")).isdigit()}
        for row in rows:
            request = listed.get(row.request_id)
            if request is not None:
                await self.draw(request | {"operation": request.get("operation") or _brief(op)}, op_id)
            else:  # a newer request of its kind replaced it
                await self.draw({"id": row.request_id, "operation": _brief(op)}, op_id, state="withdrawn")

    async def gone(self, op_id: int) -> None:
        async with self.s.db.session() as session:
            ids = list((await session.execute(select(ModerationMessage.request_id).where(
                ModerationMessage.operation_id == op_id))).scalars())
        for request_id in ids:
            await self.draw({"id": request_id, "title": f"OP-{op_id}"}, op_id, state="withdrawn")

    # ---------------------------------------------------------------- notices

    async def requested(self, notice: dict[str, Any]) -> Outcome:
        """``approval_requested`` / ``proposal_requested``."""
        request = notice.get("data") or {}
        if not await self.post(request, int(notice["operation_id"])):
            return failed("no_staff_channel")
        return delivered()

    async def decided(self, notice: dict[str, Any]) -> Outcome:
        """``approval_decided`` / ``proposal_decided``: drawn, whoever asked told, the staff log."""
        self._connected()
        request, op_id = notice.get("data") or {}, int(notice["operation_id"])
        await self.draw(request, op_id)
        what, title = asked(request.get("kind", ""), self.language), render.plain(request.get("title") or "?")
        if request.get("state") == "approved":
            text = t("Approved: {asked} – {title}.", self.language, asked=what, title=title)
        else:
            text = t("Rejected: {asked} – {title}. Reason: {reason}", self.language, asked=what, title=title,
                     reason=render.plain(request.get("reason") or "—"))
        persons = _discord_ids(notice.get("persons") or [])
        if persons:
            await self.s.channels.ping(op_id, text, persons)
        self.s.staff.post(f"{text} ({t('decided by {member}', self.language, member=render.plain(request.get('decided_by') or '?'))})")
        return delivered()

    async def stale(self, notice: dict[str, Any]) -> Outcome:
        """``stale_draft``: a reminder before the deletion, or the deletion itself."""
        self._connected()
        data, op_id = notice.get("data") or {}, int(notice["operation_id"])
        title = render.plain(data.get("title") or f"OP-{op_id}")
        persons = _discord_ids(notice.get("persons") or [])
        if data.get("stage") == "deleted":
            text = t("{title} was deleted: it wasn't published before its assembly.", self.language, title=title)
            staff = t("Deleted, never published: {title}.", self.language, title=title)
        else:
            deleted_at = data.get("deleted_at")
            when = render.ts(datetime.fromisoformat(deleted_at), "R") if deleted_at else "?"
            text = t("{title} is still not published; it is deleted {when} unless it is.", self.language,
                     title=title, when=when)
            channel_id, _ = await self._context(op_id)
            staff = t("Still not published: {title} ({channel}).", self.language, title=title,
                      channel=f"<#{channel_id}>" if channel_id else "—")
        if persons:
            await self.s.channels.ping(op_id, text, persons)
        self.s.staff.post(staff)
        if data.get("stage") != "deleted" and data.get("status") == "pending":  # up the moderation thread again
            row = await self.s.channels.row(op_id)
            for request in (row.operation if row is not None else {}).get("requests") or []:
                if request.get("kind") == "publish" and request.get("state") == "pending":
                    await self.post(request, op_id, again=True)
        return delivered()


def _brief(op: dict[str, Any]) -> dict[str, Any]:
    """The operation as a request carries it."""
    return {name: op.get(name) for name in ("id", "key", "title", "kind", "status", "assembly_at", "start_at",
                                            "duration_minutes")}
