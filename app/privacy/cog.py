"""`/privacy`: what the bot stores about members, privately, in the member's language; and `/privacy-erase` for staff
(docs/PLAN.md, *Privacy*, *Erasure*; docs/PLAN-front-end.md). The erasure is a staff command of its own because
Discord gives a command group one visibility; it asks for confirmation first, as it can't be undone, and runs in the
supervisor in the staff member's name."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.discord_util import access, finish, reply
from app.i18n import language_of, t
from app.permissions import Perm
from app.privacy import service
from app.privacy.erase import erase
from app.services import Services
from app.signup.ui import act
from app.supervisor_client import SupervisorError
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

    @app_commands.command(name="privacy-erase", description="Erase a member's data: their account, sign-ups, "
                                                            "attendance and the bot's archive")
    @app_commands.describe(member="The member (or their user id, if they left)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def erase(self, interaction: discord.Interaction, member: discord.User) -> None:
        await reply(interaction, t("Erase {member}? In the app their account is deleted and they become former:<n> "
                                   "in sign-ups, attendance and ratings; their Steam link goes. The bot deletes their "
                                   "link reminders and empties their archived messages. This can't be undone.",
                                   interaction.locale, member=member.mention),
                    view=ConfirmErase(self.services, member.id, interaction.locale))


class ConfirmErase(discord.ui.View):
    def __init__(self, services: Services, member_id: int, locale) -> None:
        super().__init__(timeout=TIMEOUT)
        self.services, self.member_id = services, member_id
        button = discord.ui.Button(label=t("Erase", locale), style=discord.ButtonStyle.danger)
        button.callback = self._erase
        self.add_item(button)

    async def _erase(self, interaction: discord.Interaction) -> None:
        """In the app, in the staff member's name (it needs Manage users there): its ``erased`` notice then empties
        what the bot holds, under the app's number. A member the app never knew: only the bot's data, for *bot
        admin*."""
        s, locale = self.services, interaction.locale
        await interaction.response.defer()
        try:
            done = await act(interaction, lambda me: s.supervisor.erase_member(me, self.member_id))
        except SupervisorError as exc:
            if exc.code != "not_found":
                raise
            if not access(interaction).has(Perm.BOT_ADMIN):
                await finish(interaction, t("You don't have the permission for this.", locale))
                return
            local = await erase(s.db, self.member_id)
            self.stop()
            s.staff.post(t("{admin} ran a data erasure; the member is now former:{number}.", s.settings.language,
                           admin=interaction.user.mention, number=local.number))
            await finish(interaction, t("Erased: the member is now former:{number}.", locale, number=local.number))
            return
        self.stop()
        if s.notices is not None:
            s.notices.wake()  # (the erased notice: the bot's archive follows)
        s.staff.post(t("{admin} ran a data erasure; the member is now former:{number}.", s.settings.language,
                       admin=interaction.user.mention, number=done["number"]))
        await finish(interaction, t("Erased: the member is now former:{number}.", locale, number=done["number"]))
