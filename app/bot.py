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
from app.services import Services

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

    @property
    def guild_object(self) -> discord.Object:
        return discord.Object(id=self.services.settings.guild_id)

    async def setup_hook(self) -> None:
        await self.tree.set_translator(Translator())
        self.tree.on_error = on_app_command_error
        for cog in (AdminCog(self.services), PrivacyCog(self.services)):
            await self.add_cog(cog, guild=self.guild_object)
        await self.tree.sync(guild=self.guild_object)
        self._tasks = [
            asyncio.create_task(self.services.scheduler.run(), name="scheduler"),
            asyncio.create_task(self.services.staff.run(self._send_staff), name="staff-log"),
            asyncio.create_task(heartbeat.run(self.services.supervisor), name="heartbeat"),
        ]

    async def _send_staff(self, text: str) -> None:
        await self.wait_until_ready()
        channel = self.get_channel(self.services.settings.channels.staff_log)
        if not isinstance(channel, discord.abc.Messageable):
            raise LookupError(f"Staff log channel {self.services.settings.channels.staff_log} not found")
        await channel.send(text, allowed_mentions=NO_MENTIONS)

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
        problems = startup_checks.check(guild, settings)
        for problem in problems:
            log.warning("Start-up check: %s", problem)
        text = t("Bot started.", settings.language)
        if problems:
            text += "\n" + t("Problems with the configuration:", settings.language) + "\n"
            text += "\n".join(f"- {problem}" for problem in problems)
        self.services.staff.post(text)
        log.info("Logged in as %s on %s", self.user, guild.name)

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await self.services.supervisor.close()
        await self.services.db.close()
        await super().close()
