"""Saying the supervisor's notices on Discord (docs/PLAN-front-end.md, *Notices*): one handler per kind, added to
:class:`~app.notices.service.Notices` by :meth:`Delivery.install`: the login link (a DM), staff-log notes (the held
roster push, Steam links, ``activity``, ``attendance_reported``, ``profile_failed``), the pings of ``cancelled``,
``slot_lost`` and ``attendance_unconfirmed`` in the operation's thread, and ``erased`` (what the bot holds of them).
Offers are app.signup.offers's, requests, decisions and stale drafts app.approval.moderation's."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import discord

from app.channel import render
from app.i18n import t
from app.notices.service import Later, Notices, Outcome, delivered, failed
from app.privacy.erase import erase
from app.supervisor_client import SupervisorError, SupervisorUnreachable

log = logging.getLogger(__name__)
GONE = (404, 409, 410)  # the link was claimed, replaced or used; or the app's address isn't set


def _when(value: str | None) -> str:
    """A Discord timestamp for an ISO time, or the text as it came."""
    try:
        return f"<t:{int(datetime.fromisoformat(value).timestamp())}:f>" if value else ""
    except ValueError:
        return value or ""


class Delivery:
    def __init__(self, services: Any, guild: Callable[[], Any]) -> None:
        self.s, self.guild = services, guild

    @property
    def language(self) -> str:
        return self.s.settings.language

    def install(self, notices: Notices, offers: Any = None, moderation: Any = None) -> None:
        """``offers`` (app.signup.offers.Offers) and ``moderation`` (app.approval.moderation.Moderation) say the
        operation notices of their own."""
        notices.register("login_link", self.login_link)
        notices.register("roster_guard", self.roster_guard)
        notices.register("link_request", self.link_request)
        notices.register("link_duplicate", self.link_duplicate)
        notices.register("activity", self.activity)
        notices.register("cancelled", self.cancelled)
        notices.register("slot_lost", self.slot_lost)
        notices.register("attendance_reported", self.attendance_reported)
        notices.register("attendance_unconfirmed", self.attendance_unconfirmed)
        notices.register("profile_failed", self.profile_failed)
        notices.register("erased", self.erased)
        if offers is not None:
            notices.register("offer", offers.deliver)
        if moderation is not None:
            for kind in ("approval_requested", "proposal_requested"):
                notices.register(kind, moderation.requested)
            for kind in ("approval_decided", "proposal_decided"):
                notices.register(kind, moderation.decided)
            notices.register("stale_draft", moderation.stale)

    async def _member(self, discord_id: Any) -> Any | None:
        guild = self.guild()
        if guild is None:
            raise Later("not connected to the server yet")
        try:
            member_id = int(discord_id)
        except (TypeError, ValueError):
            return None
        return guild.get_member(member_id)

    async def login_link(self, notice: dict[str, Any]) -> Outcome:
        """Claim the URL once and DM it. DMs closed: ``failed`` / ``dm_closed``, and the app shows staff the link.
        The URL is never logged."""
        persons = notice.get("persons") or []
        member = await self._member(persons[0]) if persons else None
        if member is None:
            return failed("not_in_guild")
        try:
            claimed = await self.s.supervisor.notice_claim(int(notice["id"]))
        except SupervisorUnreachable:
            raise
        except SupervisorError as exc:
            if exc.status in GONE:
                return failed(f"claim: {exc.message}")
            raise
        text = t("Your login link for the {group} app: {url}\nIt works once, until {until}. Don't share it: whoever "
                 "has it can set your password.", self.language, group=self.s.settings.group.acronym,
                 url=claimed["url"], until=_when(claimed.get("expires_at") or notice.get("data", {}).get("expires_at")))
        try:
            await member.send(text)
        except discord.Forbidden:
            return failed("dm_closed")
        except discord.HTTPException as exc:
            return failed(f"discord: {exc.status}")
        return delivered()

    @staticmethod
    def _who(notice: dict[str, Any]) -> str:
        persons = notice.get("persons") or []
        return f"<@{persons[0]}>" if persons and str(persons[0]).isdigit() else "?"

    async def link_request(self, notice: dict[str, Any]) -> Outcome:
        self.s.staff.post(t("{member} linked Steam ID {steam}: confirm it in the app (Members, Steam links).",
                            self.language, member=self._who(notice), steam=(notice.get("data") or {}).get("steam_uid")))
        return delivered()

    async def link_duplicate(self, notice: dict[str, Any]) -> Outcome:
        data = notice.get("data") or {}
        self.s.staff.post(t("{member} tried to link Steam ID {steam}, which {owner} holds.", self.language,
                            member=self._who(notice), steam=data.get("steam_uid"), owner=data.get("owner") or "?"))
        return delivered()

    async def activity(self, notice: dict[str, Any]) -> Outcome:
        """One staff-log line of what changed (who, what, old -> new), as the supervisor wrote it; no mentions."""
        text = str((notice.get("data") or {}).get("text") or "").strip()
        if text:
            self.s.staff.post(text[:1900])
        return delivered()

    def _persons(self, notice: dict[str, Any]) -> list[int]:
        return [int(p) for p in notice.get("persons") or [] if str(p).isdigit()]

    async def _ping(self, notice: dict[str, Any], text: str) -> Outcome:
        """``text`` and the notice's persons in the operation's thread (a draft: by DM)."""
        if self.guild() is None:
            raise Later("not connected to the server yet")
        persons = self._persons(notice)
        if persons:
            await self.s.channels.ping(int(notice["operation_id"]), text, persons)
        return delivered()

    async def cancelled(self, notice: dict[str, Any]) -> Outcome:
        """The slot holders and those Coming, with the reason."""
        data = notice.get("data") or {}
        title = render.plain(data.get("title") or "?")
        if data.get("reason"):
            text = t("{title} is cancelled. Reason: {reason}", self.language, title=title,
                     reason=render.plain(data["reason"]))
        else:
            text = t("{title} is cancelled.", self.language, title=title)
        return await self._ping(notice, text)

    async def slot_lost(self, notice: dict[str, Any]) -> Outcome:
        """The ORBAT changed under them: now first on the reserve list."""
        title = render.plain((notice.get("data") or {}).get("title") or "?")
        return await self._ping(notice, t("Your slot in {title} was removed from the ORBAT; you are first on the "
                                          "reserve list.", self.language, title=title))

    async def attendance_reported(self, notice: dict[str, Any]) -> Outcome:
        """The report itself comes with the feed (the channel's ``report``); the staff log gets the counts."""
        data = notice.get("data") or {}
        counts = data.get("counts") or {}
        parts = [f"{render.status_name(status, self.language)} {count}" for status, count in counts.items() if count]
        if data.get("guests"):
            parts.append(t(("{count} guest", "{count} guests"), self.language, count=int(data["guests"])))
        self.s.staff.post(t("Attendance of {title}: {counts}.", self.language,
                            title=render.plain(data.get("title") or "?"), counts=", ".join(parts) or "—"))
        return delivered()

    async def attendance_unconfirmed(self, notice: dict[str, Any]) -> Outcome:
        """A guest operation's players still to confirm: the organisers, in the thread."""
        data = notice.get("data") or {}
        return await self._ping(notice, t(("{title}: {count} player still waits to be confirmed in the attendance "
                                           "report: Edit attendance.",
                                           "{title}: {count} players still wait to be confirmed in the attendance "
                                           "report: Edit attendance."), self.language,
                                          title=render.plain(data.get("title") or "?"),
                                          count=int(data.get("to_confirm") or 0)))

    async def profile_failed(self, notice: dict[str, Any]) -> Outcome:
        data = notice.get("data") or {}
        self.s.staff.post(t("The server profile of {title} couldn't be built: {reason}", self.language,
                            title=render.plain(data.get("title") or "?"), reason=str(data.get("reason") or "?")[:500]))
        return delivered()

    async def erased(self, notice: dict[str, Any]) -> Outcome:
        """A member erased (from /privacy-erase or in the app): what the bot holds of them, under the app's number."""
        data = notice.get("data") or {}
        discord_id, former = str(data.get("discord_id") or ""), str(data.get("former") or "")
        number = former.partition(":")[2]
        if not discord_id.isdigit() or not number.isdigit():
            return failed("no_discord_id")
        done = await erase(self.s.db, int(discord_id), int(number))
        log.info("Erased what the bot held of former:%s (%s archived messages)", done.number, done.messages)
        return delivered()

    async def roster_guard(self, notice: dict[str, Any]) -> Outcome:
        data = notice.get("data") or {}
        accounts = ", ".join(str(a) for a in (data.get("accounts") or [])[:20])
        self.s.staff.post(t(("The supervisor held a roster push: it would disable {count} account ({accounts}). "
                             "Apply or drop it in the app, on the Members page.",
                             "The supervisor held a roster push: it would disable {count} accounts ({accounts}). "
                             "Apply or drop it in the app, on the Members page."), self.language,
                            count=int(data.get("count") or 0), accounts=accounts or "-"))
        return delivered()
