"""Keeping the Steam links and squad.xml in step with the server (docs/PLAN.md, *Steam link and squad.xml*).

- A member who gains or loses a rank or remark role, leaves, or renames their Discord account: squad.xml is
  written again (debounced); a rank lost or regained moves the link's ``rank_lost_at``.
- A member who gains a rank without a link gets a DM with *Link now* at once; if their DMs are closed, a ping in
  the reminder channel instead.
- An hourly scheduled action reminds the members with a rank and no link every N days, at most ``dms_per_hour``
  at a time, and deletes the links of members N days without a rank (told to the staff log).
- The first run posts one announcement with *Link now* in the reminder channel; it counts as everyone's first
  reminder, so the DMs start N days later.

The services stay Discord-free; this is the thin layer that sends."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

import discord

from app.i18n import t
from app.links.ui import link_view
from app.scheduler import Action
from app.squadxml.service import Person

log = logging.getLogger(__name__)
KIND = "link_reminders"
EVERY = timedelta(hours=1)


class LinkWatch:
    def __init__(self, services: Any, guild: Callable[[], Any], roster: Callable[[], Mapping[int, Person] | None]) -> None:
        self.s, self.guild, self.roster = services, guild, roster
        services.scheduler.register(KIND, self._hourly)

    @property
    def language(self) -> str:
        return self.s.settings.language

    def ranked(self) -> set[int]:
        people, rank = self.roster() or {}, self.s.ids.rank
        return {member_id for member_id, person in people.items() if not person.role_ids.isdisjoint(rank)}

    # ---------------------------------------------------------------- start-up

    async def start(self) -> None:
        """Once connected: what changed while the bot was down, the hourly action, the first rollout, squad.xml."""
        if self.roster() is None:
            return
        ranked = self.ranked()
        await self.s.links.reconcile(ranked)
        if not await self.s.scheduler.exists(KIND):
            await self.s.scheduler.schedule(KIND, self.s.scheduler.clock() + EVERY, key=KIND)
        if self.s.ids.rank and not await self.s.links.rollout_done():
            await self._rollout(ranked)
        await self.s.squadxml.write_now()

    async def _rollout(self, ranked: set[int]) -> None:
        linked = {link.member_id for link in await self.s.links.all()}
        channel = self._reminder_channel()
        if channel is not None:
            await channel.send(t("Link your Steam account with the bot: it keeps our squad.xml and counts attendance. "
                                 "Press Link now, or use /link; /privacy tells what is published.", self.language),
                               view=link_view(self.language))
        else:
            log.warning("No reminder channel (BOT_CHANNELS__LINK_REMINDER): the Steam link rollout posted nothing")
        await self.s.links.mark_rollout(ranked - linked)
        self.s.staff.post(t(("Steam link rollout: {count} member without a link gets a DM in {days} days unless they "
                             "link first.",
                             "Steam link rollout: {count} members without a link get a DM in {days} days unless they "
                             "link first."), self.language, count=len(ranked - linked),
                            days=self.s.settings.links.reminder_days))

    # ---------------------------------------------------------------- members

    async def member_changed(self, member: Any, before: frozenset[int]) -> None:
        ids = self.s.ids
        after = frozenset(role.id for role in member.roles)
        had, has = not before.isdisjoint(ids.rank), not after.isdisjoint(ids.rank)
        if had != has:
            await self.s.links.rank_changed(member.id, has)
            if has and not getattr(member, "bot", False) and await self.s.links.get(member.id) is None:
                await self.remind(member)
                await self.s.links.reminded([member.id])
        if not (before ^ after).isdisjoint(ids.rank | set(ids.remark)):
            self.s.squadxml.request()

    async def member_left(self, member_id: int) -> None:
        await self.s.links.rank_changed(member_id, False)
        self.s.squadxml.request()

    async def renamed(self, member_id: int) -> None:
        """A Discord username changed: it is the ``<icq>`` of a linked member."""
        if await self.s.links.get(member_id) is not None:
            self.s.squadxml.request()

    # ---------------------------------------------------------------- reminders

    def _reminder_channel(self) -> Any | None:
        guild, channel_id = self.guild(), self.s.ids.link_reminder
        return guild.get_channel(channel_id) if guild is not None and channel_id else None

    async def remind(self, member: Any) -> bool:
        """A DM with *Link now*; with DMs closed, a ping in the reminder channel. True when either went out."""
        text = t("You have a rank in {group} but no Steam link yet. Link your Steam account so you are in our "
                 "squad.xml and attendance counts you: press Link now, or use /link.", self.language,
                 group=self.s.settings.group.acronym)
        try:
            await member.send(text, view=link_view(self.language))
            return True
        except discord.HTTPException:
            pass
        channel = self._reminder_channel()
        if channel is None:
            log.info("Could not remind %s to link Steam: DMs closed and no reminder channel", member.id)
            return False
        await channel.send(f"{member.mention} {text}", view=link_view(self.language),
                           allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False))
        return True

    async def _hourly(self, action: Action, now: datetime) -> datetime:
        guild, settings = self.guild(), self.s.settings
        if guild is not None and self.roster() is not None and await self.s.links.rollout_done():
            due = await self.s.links.due_reminders(self.ranked(), timedelta(days=settings.links.reminder_days),
                                                   settings.links.dms_per_hour)
            for member_id in due:
                member = guild.get_member(member_id)
                if member is not None:
                    await self.remind(member)
            await self.s.links.reminded(due)
        for link in await self.s.links.expire(settings.links.delete_after_days):
            self.s.staff.post(t("Steam link of {member} deleted after {days} days without a rank: {steam} ({nick}).",
                                self.language, member=f"<@{link.member_id}>", days=settings.links.delete_after_days,
                                steam=link.steam_id, nick=link.nick))
        return now + EVERY
