"""Saying the supervisor's notices on Discord (docs/PLAN-front-end.md, *Notices*): one handler per kind, added to
:class:`~app.notices.service.Notices` by :func:`install`. So far the kinds the supervisor's phase 1 sends: the login
link (a DM) and the held roster push (a staff-log note). The operation notices come with the operations API."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import discord

from app.i18n import t
from app.notices.service import Later, Notices, Outcome, delivered, failed
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

    def install(self, notices: Notices) -> None:
        notices.register("login_link", self.login_link)
        notices.register("roster_guard", self.roster_guard)
        notices.register("link_request", self.link_request)
        notices.register("link_duplicate", self.link_duplicate)

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

    async def roster_guard(self, notice: dict[str, Any]) -> Outcome:
        data = notice.get("data") or {}
        accounts = ", ".join(str(a) for a in (data.get("accounts") or [])[:20])
        self.s.staff.post(t(("The supervisor held a roster push: it would disable {count} account ({accounts}). "
                             "Apply or drop it in the app, on the Members page.",
                             "The supervisor held a roster push: it would disable {count} accounts ({accounts}). "
                             "Apply or drop it in the app, on the Members page."), self.language,
                            count=int(data.get("count") or 0), accounts=accounts or "-"))
        return delivered()
