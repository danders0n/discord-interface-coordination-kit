"""Offers of freed slots (docs/PLAN-front-end.md, *Notices*, ``offer``). The supervisor decides who is offered a
slot and when the offer ends; the bot delivers it and says so:

- To one member: a DM with *Take it* / *No thanks*; DMs closed: a ping in the operation's thread with the same
  buttons. Then ``POST .../offers/{offer}/delivered`` (``dm`` or ``thread``): the offer's 30 minutes start only now,
  so a slot freed while the bot was down is not lost to the clock. The answer gives the deadline, which the message
  then shows.
- To the whole reserve list (the last hour before the assembly): one ping in the thread, *Take it* only.
- The message is kept in ``offer_message``. When the feed shows the offer no longer open (taken, declined, expired,
  withdrawn), the message is closed: its buttons go, a line says it ended.

If the supervisor can't hear ``delivered`` (down), the report is retried by a scheduled action; the DM is never sent
twice (the notice is delivered once, see ``app.notices.service``)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import discord
from sqlalchemy import select

from app.channel import render
from app.db.models import OfferMessage
from app.i18n import t
from app.notices.service import Later, Outcome, delivered, failed
from app.scheduler import Action
from app.signup.ui import offer_buttons
from app.supervisor_client import SupervisorError, SupervisorUnreachable

log = logging.getLogger(__name__)
REPORT_KIND = "offer_delivered"  # telling the supervisor again, when it was away
RETRY = timedelta(minutes=1)


class Offers:
    def __init__(self, services: Any, guild: Callable[[], Any], messageable: Callable[[int], Any]) -> None:
        """``messageable(channel id)``: a DM channel or thread to edit a message in, by id (the client's
        ``get_partial_messageable``; DM channels aren't cached after a restart)."""
        self.s, self.guild, self.messageable = services, guild, messageable
        services.scheduler.register(REPORT_KIND, self._report_action)

    @property
    def language(self) -> str:
        return self.s.settings.language

    # ---------------------------------------------------------------- delivering

    async def deliver(self, notice: dict[str, Any]) -> Outcome:
        guild = self.guild()
        if guild is None:
            raise Later("not connected to the server yet")
        data = notice.get("data") or {}
        op_id, offer_id = int(notice["operation_id"]), int(data["offer_id"])
        slot = data.get("slot") or {}
        where = " / ".join(plain for plain in (render.plain(slot.get("squad") or ""), render.plain(slot.get("name") or ""))
                           if plain)
        title = render.plain(data.get("title") or f"OP-{op_id}")
        persons = [int(p) for p in notice.get("persons") or [] if str(p).isdigit()]
        if data.get("to_all"):
            text = t("A slot in {title} is free: {slot}. The first on the reserve list to take it has it.",
                     self.language, title=title, slot=where)
            sent = await self.s.channels.ping(op_id, text, persons,
                                              view=offer_buttons(op_id, offer_id, self.language, everyone=True))
            via = "thread" if sent is not None else "dm"
        else:
            member = guild.get_member(persons[0]) if persons else None
            if member is None:
                return failed("not_in_guild")
            text = t("A slot in {title} is free for you: {slot}. Do you take it?", self.language, title=title,
                     slot=where)
            view = offer_buttons(op_id, offer_id, self.language)
            try:
                sent, via = await member.send(text, view=view), "dm"
            except discord.HTTPException:  # DMs closed: in the operation's thread instead
                sent = await self.s.channels.ping(op_id, text, [member.id], view=view)
                via = "thread"
                if sent is None:
                    return failed("dm_closed")
        if sent is not None:
            await self._keep(offer_id, op_id, via, sent)
        deadline = await self._report(op_id, offer_id, via)
        if deadline is not None and sent is not None and not data.get("to_all"):  # (as sent: a thread's keeps its mention)
            await self._edit(sent, (sent.content or text) + "\n" + t("The offer is yours until {until}.", self.language,
                                                                      until=render.ts(deadline, "R")), view)
        return delivered()

    async def _keep(self, offer_id: int, op_id: int, via: str, sent: Any) -> None:
        async with self.s.db.session() as session, session.begin():
            row = await session.get(OfferMessage, offer_id)
            if row is None:
                row = OfferMessage(offer_id=offer_id, operation_id=op_id, via=via, channel_id=0, message_id=0)
                session.add(row)
            row.via, row.channel_id, row.message_id = via, int(sent.channel.id), int(sent.id)

    async def _report(self, op_id: int, offer_id: int, via: str) -> datetime | None:
        """``delivered``; returns the offer's deadline from the answer. Not heard: retried in a minute."""
        try:
            op = await self.s.supervisor.offer_delivered(op_id, offer_id, via)
        except SupervisorUnreachable as exc:
            log.warning("The supervisor didn't hear that offer %s was delivered (%s): told again in a minute",
                        offer_id, exc.message)
            await self.s.scheduler.schedule(REPORT_KIND, self.s.scheduler.clock() + RETRY,
                                            key=f"{REPORT_KIND}:{offer_id}", operation_id=op_id,
                                            payload={"offer_id": offer_id, "via": via})
            return None
        except SupervisorError as exc:  # the offer ended meanwhile: nothing to start
            log.info("The supervisor refused the delivery of offer %s: %s", offer_id, exc.message)
            return None
        offer = next((o for o in op.get("offers") or [] if o.get("id") == offer_id), None)
        expires = (offer or {}).get("expires_at")
        return datetime.fromisoformat(expires) if expires else None

    async def _report_action(self, action: Action, now: datetime) -> None:
        payload = action.payload
        try:
            await self.s.supervisor.offer_delivered(action.operation_id, int(payload["offer_id"]), payload["via"])
        except SupervisorUnreachable:
            raise  # the scheduler tries again later
        except SupervisorError as exc:
            log.info("The supervisor refused the delivery of offer %s: %s", payload["offer_id"], exc.message)

    @staticmethod
    async def _edit(sent: Any, content: str, view: discord.ui.View | None) -> None:
        try:
            await sent.edit(content=content, view=view)
        except discord.HTTPException as exc:
            log.info("Editing an offer's message failed: %s", exc)

    # ---------------------------------------------------------------- closing (the feed's watcher)

    async def changed(self, op: dict[str, Any]) -> None:
        """Offers of this operation that are no longer open: their messages closed. An operation read before the
        offer was made would look the same, so the supervisor is asked once more before closing."""
        op_id = int(op["id"])
        rows = await self._rows(op_id)
        open_ids = {o.get("id") for o in op.get("offers") or []}
        candidates = [row for row in rows if row.offer_id not in open_ids]
        if not candidates:
            return
        try:
            now = await self.s.supervisor.operation(op_id)
        except SupervisorError:
            return  # the next change tries again
        open_ids = {o.get("id") for o in now.get("offers") or []}
        for row in candidates:
            if row.offer_id not in open_ids:
                await self._close(row, op.get("title") or f"OP-{op_id}")

    async def gone(self, op_id: int) -> None:
        for row in await self._rows(op_id):
            await self._close(row, f"OP-{op_id}")

    async def _rows(self, op_id: int) -> list[OfferMessage]:
        async with self.s.db.session() as session:
            return list((await session.execute(select(OfferMessage).where(OfferMessage.operation_id == op_id))).scalars())

    async def _close(self, row: OfferMessage, title: str) -> None:
        try:
            await self.messageable(row.channel_id).get_partial_message(row.message_id).edit(
                content=t("This offer of a slot in {title} has ended.", self.language, title=render.plain(title)),
                view=None)
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:  # kept: the next change tries again
            log.info("Closing the message of offer %s failed: %s", row.offer_id, exc)
            return
        async with self.s.db.session() as session, session.begin():
            found = await session.get(OfferMessage, row.offer_id)
            if found is not None:
                await session.delete(found)
