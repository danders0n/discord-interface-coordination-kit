"""`/reload` (docs/PLAN.md, *Configuration and deployment*): read `.env` again, bot admins only."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app import startup_checks
from app.discord_util import reply, require
from app.i18n import t
from app.permissions import Perm
from app.services import Services


class AdminCog(commands.Cog):
    def __init__(self, services: Services) -> None:
        self.services = services

    @app_commands.command(name="reload", description="Read the bot's settings again")
    @app_commands.default_permissions(manage_guild=True)
    @require(Perm.BOT_ADMIN)
    async def reload(self, interaction: discord.Interaction) -> None:
        result = self.services.store.reload()
        locale = interaction.locale
        if not result.ok:
            lines = "\n".join(f"- `{line}`" for line in result.errors[:20])
            await reply(interaction, t("The settings have errors, nothing was changed:", locale) + "\n" + lines)
            return
        text = t("Settings reloaded.", locale)
        if result.needs_restart:
            text += "\n" + t("These take effect after a restart: {names}", locale,
                             names=", ".join(f"`{name}`" for name in result.needs_restart))
        if interaction.guild is not None:  # names may point elsewhere now: resolve them again
            self.services.ids, problems = startup_checks.check(interaction.guild, self.services.settings)
            if problems:
                text += "\n" + t("Problems with the configuration:", locale) + "\n"
                text += "\n".join(f"- {problem}" for problem in problems[:15])
        await reply(interaction, text)
        self.services.staff.post(t("Settings reloaded by {member}.", self.services.settings.language,
                                   member=interaction.user.mention))
