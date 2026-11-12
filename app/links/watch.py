"""Steam link reminders (docs/PLAN-front-end.md, *Commands and buttons*). The supervisor knows which roles are squad.xml
ranks and who is linked (``GET /api/members?unlinked=true``); the bot delivers:

- A member who gains a role is asked once at once, if the supervisor then lists them as unlinked and they were never
  reminded (unless staff turned their reminders off): a DM with *Link now*, or a ping in the reminder channel if
  their DMs are closed. ``/links remind`` sends the same.
- An hourly scheduled action reminds the unlinked every N days, at most ``dms_per_hour`` at a time.
- The first run posts one announcement with *Link now* in the reminder channel; it counts as everyone's first
  reminder, so the DMs start N days later.

Squad.xml and the links of members without a rank are the supervisor's. This is the thin layer that sends."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import discord

from app.i18n import t
from app.links.ui import link_view
from app.scheduler import Action
from app.supervisor_client import SupervisorError

log = logging.getLogger(__name__)
KIND = "link_reminders"
EVERY = timedelta(hours=1)


class LinkWatch:
    def __init__(self, services: Any, guild: Callable[[], Any]) -> None:
        self.s, self.guild = services, guild
        services.scheduler.register(KIND, self._hourly)

    @property
    def language(self) -> str:
        return self.s.settings.language

    # ---------------------------------------------------------------- start-up

    async def start(self) -> None:
        """Once connected: the hourly action and the first rollout."""
        if not await self.s.scheduler.exists(KIND):
            await self.s.scheduler.schedule(KIND, self.s.scheduler.clock() + EVERY, key=KIND)
        if not await self.s.links.rollout_done():
            try:
                unlinked = await self.s.links.unlinked()
            except SupervisorError as exc:
                log.warning("The Steam link rollout waits for the supervisor: %s", exc.message)
                return
            await self._rollout(unlinked)

    async def _rollout(self, unlinked: set[int]) -> None:
        channel = self._reminder_channel()
        if channel is not None:
            await channel.send(t("Link your Steam account with the bot: it keeps our squad.xml and counts attendance. "
                                 "Press Link now, or use /link; /privacy tells what is published.", self.language),
                               view=link_view(self.language))
        else:
            log.warning("No reminder channel (BOT_CHANNELS__LINK_REMINDER): the Steam link rollout posted nothing")
        await self.s.links.mark_rollout(unlinked)
        self.s.staff.post(t(("Steam link rollout: {count} member without a link gets a DM in {days} days unless they "
                             "link first.",
                             "Steam link rollout: {count} members without a link get a DM in {days} days unless they "
                             "link first."), self.language, count=len(unlinked),
                            days=self.s.settings.links.reminder_days))

    # ---------------------------------------------------------------- members

    async def roles_gained(self, member: Any) -> None:
        """After the member's push: ask a newly ranked member at once (only the first time)."""
        if getattr(member, "bot", False) or not await self.s.links.never_reminded(member.id):
            return
        try:
            if member.id not in await self.s.links.unlinked():
                return
        except SupervisorError as exc:  # the hourly run asks them later
            log.info("No link check for %s: %s", member.id, exc.message)
            return
        if await self.remind(member) is not None:
            await self.s.links.reminded([member.id])

    # ---------------------------------------------------------------- reminders

    def _reminder_channel(self) -> Any | None:
        guild, channel_id = self.guild(), self.s.ids.link_reminder
        return guild.get_channel(channel_id) if guild is not None and channel_id else None

    async def remind(self, member: Any) -> str | None:
        """A DM with *Link now*; with DMs closed, a ping in the reminder channel. ``"dm"`` or ``"channel"``: how it
        went out; None: it didn't."""
        text = t("You have a rank in {group} but no Steam link yet. Link your Steam account so you are in our "
                 "squad.xml and attendance counts you: press Link now, or use /link.", self.language,
                 group=self.s.settings.group.acronym)
        try:
            await member.send(text, view=link_view(self.language))
            return "dm"
        except discord.HTTPException:
            pass
        channel = self._reminder_channel()
        if channel is None:
            log.info("Could not remind %s to link Steam: DMs closed and no reminder channel", member.id)
            return None
        await channel.send(f"{member.mention} {text}", view=link_view(self.language),
                           allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
        return "channel"

    async def _hourly(self, action: Action, now: datetime) -> datetime:
        guild, settings = self.guild(), self.s.settings
        if guild is None:
            return now + EVERY
        try:
            unlinked = await self.s.links.unlinked()
        except SupervisorError as exc:  # the next hour asks again
            log.warning("Steam link reminders skipped: %s", exc.message)
            return now + EVERY
        if not await self.s.links.rollout_done():  # (the supervisor was away at start-up)
            await self._rollout(unlinked)
            return now + EVERY
        due = await self.s.links.due_reminders(unlinked, timedelta(days=settings.links.reminder_days),
                                               settings.links.dms_per_hour)
        tried = []
        for member_id in due:
            member = guild.get_member(member_id)
            if member is None:
                continue
            try:
                await self.remind(member)
            except discord.HTTPException as exc:  # (the reminder channel refused): the next run tries them again
                log.warning("Reminding %s to link Steam failed: %s", member_id, exc)
                continue
            tried.append(member_id)
        await self.s.links.reminded(tried)  # (not the failed ones: the next hour asks them again)
        return now + EVERY
