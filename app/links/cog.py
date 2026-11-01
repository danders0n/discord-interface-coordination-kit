"""Steam link commands (docs/PLAN.md, *Steam link and squad.xml*):

- ``/link steam nick``: link or relink yourself; without both it opens the modal (what *Link now* opens);
- ``/unlink``: remove your own link, and so leave squad.xml;
- ``/links set member steam nick`` and ``/links remove member``: anyone's link, for *manage links* roles. Discord
  gives a command group one visibility, so the staff commands are their own group (Manage Server at first, shown
  to the roles from `.env` by the server owner) and the members' are top-level commands."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.discord_util import reply, require
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
    @app_commands.describe(steam="Your Steam64 ID or steamcommunity.com/profiles/… address",
                           nick="Your nick in squad.xml")
    @app_commands.guild_only()
    async def link(self, interaction: discord.Interaction, steam: str | None = None, nick: str | None = None) -> None:
        if not steam or not nick:
            current = await self.services.links.get(interaction.user.id)
            await interaction.response.send_modal(ui.LinkModal(interaction, current, steam, nick))
            return
        await ui.save(interaction, interaction.user.id, steam, nick)

    @app_commands.command(name="unlink", description="Remove your Steam link (you leave squad.xml)")
    @app_commands.guild_only()
    async def unlink(self, interaction: discord.Interaction) -> None:
        await self._remove(interaction, interaction.user.id)

    @links.command(name="set", description="Set a member's Steam link and nick")
    @app_commands.describe(member="The member", steam="Their Steam64 ID or profile address", nick="Their nick in squad.xml")
    @require(Perm.MANAGE_LINKS)
    async def set_link(self, interaction: discord.Interaction, member: discord.Member, steam: str, nick: str) -> None:
        await ui.save(interaction, member.id, steam, nick)

    @links.command(name="remove", description="Remove a member's Steam link")
    @app_commands.describe(member="The member (or their user id, if they left)")
    @require(Perm.MANAGE_LINKS)
    async def remove_link(self, interaction: discord.Interaction, member: discord.User) -> None:
        await self._remove(interaction, member.id)

    async def _remove(self, interaction: discord.Interaction, member_id: int) -> None:
        s, locale = self.services, interaction.locale
        gone = await s.links.remove(member_id)
        if gone is None:
            await reply(interaction, t("There is no Steam link to remove.", locale))
            return
        s.squadxml.request()
        s.staff.post(t("Steam link of {member} removed by {who}: {steam} ({nick}).", s.settings.language,
                       member=f"<@{member_id}>", who=interaction.user.mention, steam=gone.steam_id, nick=gone.nick))
        await reply(interaction, t("The Steam link is removed; squad.xml follows shortly.", locale))
