"""The Discord client: intents, the guild's slash commands, and the background tasks (docs/PLAN.md, phase 1;
docs/PLAN-front-end.md).

Commands are synced to the one guild only. The scheduler, the staff log and the heartbeat run as tasks next to
the gateway connection; the supervisor work (the roster push, the notices, the operations feed, the live events)
starts once the supervisor check passes. Cogs get the shared :class:`~app.services.Services`."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from app import heartbeat, startup_checks, supervisor_check
from app.admin.cog import AdminCog
from app.approval.moderation import Moderation
from app.approval.ui import ApprovalAction
from app.archive.cog import ArchiveCog
from app.archive.export import Exporter
from app.archive.service import ArchiveService
from app.channel.service import OperationChannels
from app.discord_util import NO_MENTIONS, install_error_handlers, on_app_command_error
from app.i18n import Translator, t
from app.links.cog import LinkCog
from app.links.service import LinkService
from app.links.ui import LinkNow
from app.links.watch import LinkWatch
from app.live import Live
from app.notices.deliver import Delivery
from app.notices.service import Notices
from app.operations.feed import Feed
from app.operations.header import Header
from app.privacy.cog import PrivacyCog
from app.roster.service import Member as RosterMember
from app.roster.service import Role as RosterRole
from app.roster.service import Roster
from app.services import Services
from app.signup.cog import SlotCog
from app.signup.offers import Offers
from app.signup.ui import OfferAction, SignupAction, SquadList
from app.staff_threads import KINDS

log = logging.getLogger(__name__)


def intents() -> discord.Intents:
    """Server Members for the roster (joins, departures, role changes); Message Content for the archive's
    transcripts. Both are privileged and switched on in the Discord developer portal."""
    chosen = discord.Intents.default()
    chosen.members = True
    chosen.message_content = True
    return chosen


def roster_member(member: discord.Member, guild_id: int) -> RosterMember:
    """A member as the supervisor's roster takes it (``@everyone``, whose id is the guild's, left out)."""
    return RosterMember(member.id, member.name, member.display_name,
                        tuple(r.id for r in member.roles if r.id != guild_id))


def roster_role(role: discord.Role) -> RosterRole:
    return RosterRole(role.id, role.name, role.position, role.color.value)


class EventBot(commands.Bot):
    def __init__(self, services: Services) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=intents(), help_command=None,
                         allowed_mentions=NO_MENTIONS)
        self.services = services
        self._tasks: list[asyncio.Task] = []
        self._started = False
        services.roles_of = self.roles_of
        self.build_operations()
        self.build_front_end()
        self.build_links()
        services.archive = ArchiveService(services.db, services.store, services.scheduler.clock)
        services.exporter = Exporter(services, self.guild)

    @property
    def guild_object(self) -> discord.Object:
        return discord.Object(id=self.services.settings.guild_id)

    def guild(self) -> discord.Guild | None:
        return self.get_guild(self.services.settings.guild_id)

    def roles_of(self, member_id: int) -> frozenset[int] | None:
        """A member's role ids now, or None when they are not on the server."""
        guild = self.guild()
        member = guild.get_member(member_id) if guild is not None else None
        return frozenset(role.id for role in member.roles) if member is not None else None

    def build_operations(self) -> None:
        """The operation channels, drawn from the supervisor's change feed (docs/PLAN-front-end.md, *Sync and
        drawing*), and the header's calendar."""
        s = self.services
        s.channels = OperationChannels(s.db, s.store, lambda: s.ids, s.scheduler, self.guild, s.supervisor)
        s.channels.header = Header(s.db, s.store, lambda: s.ids, self.guild, s.scheduler.clock)
        s.channels.staff = s.staff
        s.feed = Feed(s.db, s.supervisor, s.channels, s.scheduler.clock)

    def build_front_end(self) -> None:
        """The roster push, the notices and the live events (docs/PLAN-front-end.md)."""
        s = self.services
        s.roster = Roster(s.supervisor, s.scheduler, self.roster_members, self.roster_roles, self.roster_one)
        s.notices = Notices(s.db, s.supervisor, s.scheduler.clock)
        s.offers = Offers(s, self.guild, self.get_partial_messageable)
        s.moderation = Moderation(s, self.guild)
        Delivery(s, self.guild).install(s.notices, s.offers, s.moderation)
        s.channels.watchers += [s.offers, s.moderation]  # (offers closed, requests drawn as the feed changes them)
        s.live = Live(s.supervisor, {"notice": lambda _event: s.notices.wake(),
                                     "operation": lambda _event: s.feed.wake()})

    def roster_members(self) -> list[RosterMember] | None:
        """The guild's people for the roster push; None until Discord has sent the member list."""
        guild = self.guild()
        if guild is None or not guild.chunked:
            return None
        return [roster_member(m, guild.id) for m in guild.members if not m.bot]

    def roster_roles(self) -> list[RosterRole] | None:
        guild = self.guild()
        return [roster_role(r) for r in guild.roles if r.id != guild.id] if guild is not None else None

    def roster_one(self, member_id: int) -> RosterMember | None:
        guild = self.guild()
        member = guild.get_member(member_id) if guild is not None else None
        return roster_member(member, guild.id) if member is not None and not member.bot else None

    def _ours(self, guild: discord.Guild) -> bool:
        return guild.id == self.services.settings.guild_id

    async def on_member_join(self, member: discord.Member) -> None:
        if self._ours(member.guild) and not member.bot:
            await self.services.roster.member_changed(roster_member(member, member.guild.id))

    async def on_guild_role_create(self, role: discord.Role) -> None:
        if self._ours(role.guild):
            await self.services.roster.roles_changed()

    async def on_guild_role_delete(self, role: discord.Role) -> None:
        if self._ours(role.guild):
            await self.services.roster.roles_changed()

    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        if self._ours(after.guild) and (before.name, before.position, before.color) != (after.name, after.position,
                                                                                      after.color):
            await self.services.roster.roles_changed()

    def build_links(self) -> None:
        """Steam links through the supervisor, and the reminders (docs/PLAN-front-end.md); after the roster, whose
        ``as_member`` they use."""
        s = self.services
        s.links = LinkService(s.db, s.supervisor, s.roster.as_member, s.scheduler.clock)
        s.link_watch = LinkWatch(s, self.guild)

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """The supervisor frees a member's slots when they lose the sign-up role; the bot only pushes the change."""
        if not self._ours(before.guild) or after.bot:
            return
        if before.roles != after.roles or before.display_name != after.display_name:
            await self.services.roster.member_changed(roster_member(after, after.guild.id))
            if set(after.roles) - set(before.roles):  # (after the push: the supervisor knows the new roles)
                await self.services.link_watch.roles_gained(after)

    async def on_user_update(self, before: discord.User, after: discord.User) -> None:
        if (before.name, before.display_name) != (after.name, after.display_name):
            member = self.roster_one(after.id)  # (a nickname on the server wins over the global name)
            if member is not None:
                await self.services.roster.member_changed(member)

    async def on_member_remove(self, member: discord.Member) -> None:
        if self._ours(member.guild) and not member.bot:
            await self.services.roster.member_left(roster_member(member, member.guild.id))

    async def setup_hook(self) -> None:
        await self.tree.set_translator(Translator())
        self.add_dynamic_items(LinkNow, SignupAction, SquadList, OfferAction, ApprovalAction)  # outlive a restart
        self.tree.on_error = on_app_command_error
        install_error_handlers()
        for cog in (AdminCog(self.services), PrivacyCog(self.services), LinkCog(self.services),
                    ArchiveCog(self.services), SlotCog(self.services)):
            await self.add_cog(cog, guild=self.guild_object)
        await self.tree.sync(guild=self.guild_object)
        # The scheduler waits for on_ready: its actions need the guild and the resolved ids, and one run before
        # them would be marked done having done nothing (an archive, a ping).
        self._tasks = [
            asyncio.create_task(self.services.staff.run(self._send_staff), name="staff-log"),
            asyncio.create_task(heartbeat.run(self.services.supervisor), name="heartbeat"),
        ]

    def start_scheduler(self) -> None:
        if not any(task.get_name() == "scheduler" for task in self._tasks):
            self._tasks.append(asyncio.create_task(self.services.scheduler.run(), name="scheduler"))

    def start_supervisor_gate(self) -> None:
        """The supervisor work waits for the supervisor check (app/supervisor_check.py)."""
        if any(task.get_name() == "supervisor-check" for task in self._tasks):
            return
        s = self.services
        self._tasks.append(asyncio.create_task(supervisor_check.gate(
            s.supervisor, lambda: s.settings.tz, lambda: s.settings.language, s.scheduler.clock, s.staff.post,
            self.start_front_end), name="supervisor-check"))

    async def start_front_end(self) -> None:
        """The roster push (now, then daily), the redraw of what was drawn before, the notices worker, the operations
        feed and the live events: after on_ready, as they need the guild, and after the supervisor check (a redraw
        fetches files and writes the channels' places there)."""
        s = self.services
        await s.roster.start()
        await s.channels.refresh_all()  # what was drawn before; the feed brings what changed meanwhile
        names = {task.get_name() for task in self._tasks}
        for name, job in (("notices", s.notices.run), ("operations", s.feed.run), ("live", s.live.run)):
            if name not in names:
                self._tasks.append(asyncio.create_task(job(), name=name))

    def staff_channel(self) -> tuple[discord.Guild, discord.TextChannel]:
        guild = self.get_guild(self.services.settings.guild_id)
        channel = guild.get_channel(self.services.ids.staff) if guild and self.services.ids.staff else None
        if guild is None or not isinstance(channel, discord.TextChannel):
            raise LookupError("The staff channel (BOT_CHANNELS__STAFF) is not resolved")
        return guild, channel

    async def _send_staff(self, text: str) -> None:
        await self.wait_until_ready()
        guild, channel = self.staff_channel()
        await self.services.threads.send(guild, channel, "staff_log", text, allowed_mentions=NO_MENTIONS)

    async def on_ready(self) -> None:
        if self._started:
            log.info("Reconnected to Discord")
            return
        self._started = True
        settings = self.services.settings
        guild = self.get_guild(settings.guild_id)
        if guild is None:
            log.error("The bot is not on the server %s (BOT_GUILD_ID); no scheduled action runs", settings.guild_id)
            return
        self.services.ids, problems = startup_checks.check(guild, settings)
        for problem in problems:
            log.warning("Start-up check: %s", problem)
        if self.services.ids.staff is not None:
            for kind in KINDS:  # made now, so staff see where the bot will write
                try:
                    await self.services.threads.get(guild, guild.get_channel(self.services.ids.staff), kind)
                except discord.HTTPException as exc:
                    problems.append(t("Could not make the {thread} thread: {reason}", settings.language,
                                      thread=self.services.threads.name(kind), reason=exc.text or exc))
        text = t("Bot started.", settings.language)
        if problems:
            text += "\n" + t("Problems with the configuration:", settings.language) + "\n"
            text += "\n".join(f"- {problem}" for problem in problems)
        self.services.staff.post(text)
        try:
            self.services.channels.header.request()
            try:
                await self.services.link_watch.start()  # the hourly reminders, the first rollout
            except Exception:  # noqa: BLE001 - logged as an ERROR; the rest of the bot works on
                log.exception("Starting the Steam links failed")
            await self.services.exporter.start()  # the daily move of the oldest quarter into the database
        finally:
            self.start_scheduler()  # now: the guild is there and the ids resolved; what was due meanwhile runs late
            self.start_supervisor_gate()
        log.info("Logged in as %s on %s", self.user, guild.name)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.services.supervisor.close()
        await self.services.db.close()
        await super().close()
