"""`/privacy`: what the bot stores about members, privately, in the member's language; and `/privacy-erase` for bot
admins (docs/PLAN.md, *Privacy*, *Erasure*). The erasure is a staff command of its own because Discord gives a
command group one visibility; it asks for confirmation first, as it can't be undone."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.discord_util import finish, reply, require
from app.i18n import language_of, t
from app.permissions import Perm
from app.privacy import service
from app.privacy.erase import erase
from app.services import Services
from app.staff_log import chunks

EMBED_LIMIT = 4096
TIMEOUT = 5 * 60


class PrivacyCog(commands.Cog):
    def __init__(self, services: Services) -> None:
        self.services = services

    @app_commands.command(name="privacy", description="What the bot stores about you, and how to have it erased")
    async def privacy(self, interaction: discord.Interaction) -> None:
        body = service.text(language_of(interaction.locale), self.services.settings.data_dir)
        # one embed per message: a message holds at most 6000 characters of embeds in all
        for part in chunks(body, EMBED_LIMIT)[:5]:
            await reply(interaction, embed=discord.Embed(description=part))

    @app_commands.command(name="privacy-erase", description="Erase a member's data: Steam link, ids in every table")
    @app_commands.describe(member="The member (or their user id, if they left)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @require(Perm.BOT_ADMIN)
    async def erase(self, interaction: discord.Interaction, member: discord.User) -> None:
        await reply(interaction, t("Erase the data of {member}? Their Steam link is deleted and their ids are replaced "
                                   "by a pseudonym everywhere. This can't be undone.", interaction.locale,
                                   member=member.mention), view=ConfirmErase(self.services, member.id, interaction.locale))


class ConfirmErase(discord.ui.View):
    def __init__(self, services: Services, member_id: int, locale) -> None:
        super().__init__(timeout=TIMEOUT)
        self.services, self.member_id = services, member_id
        button = discord.ui.Button(label=t("Erase", locale), style=discord.ButtonStyle.danger)
        button.callback = self._erase
        self.add_item(button)

    async def _erase(self, interaction: discord.Interaction) -> None:
        s = self.services
        done = await erase(s.db, self.member_id, s.scheduler.clock)
        self.stop()
        if s.squadxml is not None:
            s.squadxml.request()
        if s.channels is not None:
            for event_id in done.events:
                await s.channels.changed(event_id)
        # The staff log keeps that it ran, not whom it was about.
        s.staff.post(t("{admin} ran a data erasure; the member is now former:{number}.", s.settings.language,
                       admin=interaction.user.mention, number=done.number))
        await finish(interaction, t("Erased: the member is now former:{number}.", interaction.locale,
                                    number=done.number))
