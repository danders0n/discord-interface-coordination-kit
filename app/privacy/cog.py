"""`/privacy`: what the bot stores about members, privately, in the member's language."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.discord_util import reply
from app.i18n import language_of
from app.privacy import service
from app.services import Services
from app.staff_log import chunks

EMBED_LIMIT = 4096


class PrivacyCog(commands.Cog):
    def __init__(self, services: Services) -> None:
        self.services = services

    @app_commands.command(name="privacy", description="What the bot stores about you, and how to have it erased")
    async def privacy(self, interaction: discord.Interaction) -> None:
        body = service.text(language_of(interaction.locale), self.services.settings.data_dir)
        # one embed per message: a message holds at most 6000 characters of embeds in all
        for part in chunks(body, EMBED_LIMIT)[:5]:
            await reply(interaction, embed=discord.Embed(description=part))
