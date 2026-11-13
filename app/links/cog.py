"""Steam link commands (docs/PLAN-front-end.md, *Commands and buttons*):

- ``/link steam``: link or relink yourself (through the supervisor, unconfirmed until staff confirm it); without
  ``steam`` it opens the modal (what *Link now* opens);
- ``/unlink``: remove your own link, and so leave squad.xml;
- ``/links remind member``: the link reminder now; ``/links reminders member on``: the automatic ones off or on.
  Discord gives a command group one visibility, so the staff commands are their own group (Manage Server at first,
  shown to the roles from `.env` by the server owner).

Setting anyone's link, remarks, the squad.xml status and the lists are the app's (Members → Steam links)."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.discord_util import reply, require, thinking
from app.i18n import t
from app.links import ui
from app.permissions import Perm
from app.services import Services


class LinkCog(commands.Cog):
    links = app_commands.Group(name="links", description="Members' Steam links (staff)", guild_only=True,
                               default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, services: Services) -> None:
        self.services = services

    @app_commands.command(name="link", description="Link your Steam account: squad.xml and attendance")
    @app_commands.describe(steam="Your Steam64 ID or steamcommunity.com/profiles/… address")
    @app_commands.guild_only()
    async def link(self, interaction: discord.Interaction, steam: str | None = None) -> None:
        if not steam:
            await interaction.response.send_modal(ui.LinkModal(interaction))
            return
        await ui.save(interaction, steam)

    @app_commands.command(name="unlink", description="Remove your Steam link (you leave squad.xml)")
    @app_commands.guild_only()
    async def unlink(self, interaction: discord.Interaction) -> None:
        locale = interaction.locale
        await thinking(interaction)
        before, _person = await self.services.links.unlink(interaction.user.id)
        await reply(interaction, t("There is no Steam link to remove.", locale) if before is None else
                    t("The Steam link is removed; squad.xml follows shortly.", locale))

    # ---------------------------------------------------------------- reminders

    @links.command(name="remind", description="Send a member the Steam link reminder now")
    @app_commands.describe(member="The member")
    @require(Perm.BOT_ADMIN)
    async def remind(self, interaction: discord.Interaction, member: discord.Member) -> None:
        s, locale = self.services, interaction.locale
        await thinking(interaction)
        if member.id not in await s.links.unlinked():
            person = await s.links.person(member.id)
            await reply(interaction, t("{member} is linked already.", locale, member=member.mention)
                        if person and person.get("steam_uid") else
                        t("{member} has no rank role, so squad.xml doesn't need them.", locale, member=member.mention))
            return
        how = await s.link_watch.remind(member)
        if how is not None:
            await s.links.reminded([member.id])
        await reply(interaction, {
            "dm": t("Reminder sent to {member} by DM.", locale, member=member.mention),
            "channel": t("The DMs of {member} are closed: they were pinged in the reminder channel.", locale,
                         member=member.mention),
            None: t("The DMs of {member} are closed and there is no reminder channel: nothing was sent.", locale,
                    member=member.mention),
        }[how])

    @links.command(name="reminders", description="Turn a member's automatic Steam link reminders off or on")
    @app_commands.describe(member="The member", on="Off: no DMs, pings or sign-up questions until they link")
    @require(Perm.BOT_ADMIN)
    async def reminders(self, interaction: discord.Interaction, member: discord.Member, on: bool) -> None:
        s, locale = self.services, interaction.locale
        await s.links.mute(member.id, not on)
        await reply(interaction, t("Reminders for {member} are on.", locale, member=member.mention) if on else
                    t("Reminders for {member} are off until they link; /links remind still sends one.", locale,
                      member=member.mention))
