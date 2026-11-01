"""The Discord client: intents, the guild's slash commands, and the background tasks (docs/PLAN.md, phase 1).

Commands are synced to the one guild only. The scheduler, the staff log and the heartbeat run as tasks next to
the gateway connection. Cogs get the shared :class:`~app.services.Services`."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from app import heartbeat, startup_checks
from app.admin.cog import AdminCog
from app.discord_util import NO_MENTIONS, on_app_command_error
from app.i18n import Translator, t
from app.privacy.cog import PrivacyCog
from app.servers.cog import ServerCog
from app.eventchannel.service import EventChannels
from app.events.cog import EventCog
from app.events.header import Header
from app.events.service import EventService
from app.events.ui import CreateEvent, PanelAction, PanelButton, expired
from app.approval.ui import ApprovalAction
from app.signup.ui import OfferAction, SignupAction
from app.services import Services
from app.signup.service import Member, SignupService
from app.staff_threads import KINDS

log = logging.getLogger(__name__)


def intents() -> discord.Intents:
    """Server Members for role changes and departures (squad.xml, sign-ups); Message Content for the archive's
    transcripts. Both are privileged and switched on in the Discord developer portal."""
    chosen = discord.Intents.default()
    chosen.members = True
    chosen.message_content = True
    return chosen


class EventBot(commands.Bot):
    def __init__(self, services: Services) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=intents(), help_command=None,
                         allowed_mentions=NO_MENTIONS)
        self.services = services
        self._tasks: list[asyncio.Task] = []
        self._started = False
        self.build_events()

    @property
    def guild_object(self) -> discord.Object:
        return discord.Object(id=self.services.settings.guild_id)

    def guild(self) -> discord.Guild | None:
        return self.get_guild(self.services.settings.guild_id)

    def roles_of(self, member_id: int) -> frozenset[int] | None:
        """A member's role ids now, or None when they are not on the server (for offers and flags)."""
        guild = self.guild()
        member = guild.get_member(member_id) if guild is not None else None
        return frozenset(role.id for role in member.roles) if member is not None else None

    def build_events(self) -> None:
        """The event stack (docs/PLAN.md, phase 4): the channels are the notifier of the event and sign-up
        services, and need the guild, so the bot puts them together."""
        s = self.services
        channels = EventChannels(s.db, s.store, lambda: s.ids, s.scheduler, self.guild)
        events = EventService(s.db, s.store, lambda: s.ids, s.scheduler, s.reservations, channels, link=channels.link)
        signups = SignupService(s.db, s.store, lambda: s.ids, s.scheduler, channels, self.roles_of)
        channels.events, channels.signups = events, signups
        channels.header = Header(s.db, s.store, lambda: s.ids, self.guild, s.scheduler.clock)
        channels.staff, channels.threads, channels.services, channels.roles_of = s.staff, s.threads, s, self.roles_of
        s.channels, s.events, s.signups = channels, events, signups

    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if before.guild.id == self.services.settings.guild_id and before.roles != after.roles:
            await self.services.signups.member_changed(Member(after.id, frozenset(r.id for r in after.roles)),
                                                       previous=frozenset(r.id for r in before.roles))

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        await expired(interaction)  # a wizard from before a restart

    async def on_member_remove(self, member: discord.Member) -> None:
        if member.guild.id == self.services.settings.guild_id:
            await self.services.signups.member_left(member.id)

    async def setup_hook(self) -> None:
        await self.tree.set_translator(Translator())
        self.add_dynamic_items(CreateEvent, PanelButton, PanelAction, SignupAction, OfferAction,  # outlive a restart
                               ApprovalAction)
        self.tree.on_error = on_app_command_error
        for cog in (AdminCog(self.services), PrivacyCog(self.services), ServerCog(self.services), EventCog(self.services)):
            await self.add_cog(cog, guild=self.guild_object)
        await self.tree.sync(guild=self.guild_object)
        self._tasks = [
            asyncio.create_task(self.services.scheduler.run(), name="scheduler"),
            asyncio.create_task(self.services.staff.run(self._send_staff), name="staff-log"),
            asyncio.create_task(heartbeat.run(self.services.supervisor), name="heartbeat"),
        ]

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
            log.error("The bot is not on the server %s (BOT_GUILD_ID)", settings.guild_id)
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
        await self.services.channels.refresh_all()  # whatever changed while the bot was down
        self.services.channels.header.request()
        log.info("Logged in as %s on %s", self.user, guild.name)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.services.supervisor.close()
        await self.services.db.close()
        await super().close()
