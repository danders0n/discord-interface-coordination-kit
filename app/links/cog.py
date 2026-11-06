"""Steam link commands (docs/PLAN.md, *Steam link and squad.xml*):

- ``/link steam nick``: link or relink yourself; without both it opens the modal (what *Link now* opens);
- ``/unlink``: remove your own link, and so leave squad.xml;
- ``/links set member steam nick`` and ``/links remove member``: anyone's link, for *manage links* roles. Discord
  gives a command group one visibility, so the staff commands are their own group (Manage Server at first, shown
  to the roles from `.env` by the server owner) and the members' are top-level commands;
- ``/links status``: squad.xml as it is (folder, address, how many are in it, when written, whether it is up to
  date, the files kept by hand that are missing, the last failure), the file itself and *Write now*;
- ``/links list show``: who is in squad.xml, who has a rank and no link, who is linked without a rank (and when
  the link goes), whose reminders are off, who has an own remark; a list too long for the embed comes as a file;
- ``/links remark member text only``: staff's own remark, before the remark roles or instead of them;
- ``/links remind member``: the link reminder now; ``/links reminders member on``: the automatic ones off or on."""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.app_commands import locale_str
from discord.ext import commands

from app.discord_util import reply, require, thinking
from app.eventchannel.render import mention, ts
from app.i18n import t
from app.links import ui
from app.links.service import LinkError
from app.permissions import Perm
from app.services import Services
from app.squadxml.service import FILE_NAME

EMBED_MAX = 4000


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

    # ---------------------------------------------------------------- squad.xml

    @links.command(name="status", description="squad.xml now: who is in it, when it was written, what is missing")
    @require(Perm.MANAGE_LINKS)
    async def status(self, interaction: discord.Interaction) -> None:
        s, locale = self.services, interaction.locale
        await thinking(interaction)
        status = await s.squadxml.status()
        extra = {"file": discord.File(io.BytesIO(status.text.encode("utf-8")), FILE_NAME)} if status.text else {}
        await reply(interaction, embed=ui.status_embed(status, s.settings.squad_xml.url, locale),
                    view=ui.write_view(locale), **extra)

    @links.command(name="list", description="Who is in squad.xml, who has no link, and more")
    @app_commands.describe(show="Which members")
    @app_commands.choices(show=[app_commands.Choice(name=locale_str("In squad.xml"), value="in_file"),
                                app_commands.Choice(name=locale_str("A rank, no link"), value="unlinked"),
                                app_commands.Choice(name=locale_str("Linked, no rank"), value="unranked"),
                                app_commands.Choice(name=locale_str("Reminders off"), value="muted"),
                                app_commands.Choice(name=locale_str("Own remark"), value="remarks")])
    @require(Perm.MANAGE_LINKS)
    async def list_links(self, interaction: discord.Interaction, show: str = "in_file") -> None:
        locale, guild = interaction.locale, interaction.guild
        titles = {"in_file": t("In squad.xml", locale), "unlinked": t("A rank, no link", locale),
                  "unranked": t("Linked, no rank", locale), "muted": t("Reminders off", locale),
                  "remarks": t("Own remark", locale)}
        show = show if show in titles else "in_file"

        def name(member_id: int) -> str:
            member = guild.get_member(member_id) if guild is not None else None
            return f"{member.display_name} ({member_id})" if member is not None else str(member_id)

        shown = await self._rows(show, locale, lambda value: ts(value, "R"))
        lines = [" · ".join(p for p in (mention(member_id), detail) if p) for member_id, detail in shown]
        text = "\n".join(lines)
        embed = discord.Embed(title=f"{titles[show]} ({len(lines)})", description=(
            text if len(text) <= EMBED_MAX else text[:text.rfind("\n", 0, EMBED_MAX)] + "\n…") if lines else
            t("Nobody.", locale))
        extra = {}
        if len(text) > EMBED_MAX:
            full = await self._rows(show, locale, lambda value: value.date().isoformat())
            listed = "\n".join(" · ".join(p for p in (name(member_id), detail) if p) for member_id, detail in full)
            extra["file"] = discord.File(io.BytesIO(listed.encode("utf-8-sig")), f"links-{show}.txt")
        await reply(interaction, embed=embed, **extra)

    async def _rows(self, show: str, locale: discord.Locale,
                    when: Callable[[datetime], str]) -> list[tuple[int, str]]:
        """(member id, what to say about them) for ``/links list``, in the order shown."""
        s = self.services
        if show == "in_file":
            return [(e.member_id, " · ".join(p for p in (e.nick, e.steam_id, e.remark) if p))
                    for e in await s.squadxml.entries() or []]
        links = await s.links.all()
        if show == "unlinked":
            reminders, linked, rows = await s.links.reminders(), {link.member_id for link in links}, []
            for member_id in sorted(s.link_watch.ranked() - linked):
                row = reminders.get(member_id)
                if row is not None and row.muted:
                    rows.append((member_id, t("reminders off", locale)))
                elif row is not None and row.last_at is not None:
                    rows.append((member_id, t(("reminded {count} time, last {when}",
                                               "reminded {count} times, last {when}"), locale, count=row.count,
                                              when=when(row.last_at))))
                else:
                    rows.append((member_id, t("not reminded yet", locale)))
            return rows
        if show == "unranked":
            days = s.settings.links.delete_after_days
            lost = sorted((link for link in links if link.rank_lost_at is not None), key=lambda l: l.rank_lost_at)
            return [(link.member_id, t("{nick} · {steam} · the link goes {when}", locale, nick=link.nick,
                                       steam=link.steam_id, when=when(link.rank_lost_at + timedelta(days=days))))
                    for link in lost]
        if show == "muted":
            return [(member_id, "") for member_id, row in sorted((await s.links.reminders()).items()) if row.muted]
        return [(link.member_id, f"{link.nick} · {link.remark}" + (f" · {t('instead of the roles', locale)}"
                                                                    if link.remark_only else ""))
                for link in sorted(links, key=lambda l: l.nick.casefold()) if link.remark]

    @links.command(name="remark", description="Your own remark in a member's squad.xml entry")
    @app_commands.describe(member="The member", text="The remark; leave it out to remove it",
                           only="Instead of the remark roles (default: before them)")
    @require(Perm.MANAGE_LINKS)
    async def remark(self, interaction: discord.Interaction, member: discord.Member, text: str | None = None,
                     only: bool = False) -> None:
        s, locale = self.services, interaction.locale
        try:
            link = await s.links.set_remark(member.id, text or "", only)
        except LinkError as exc:
            await reply(interaction, ui.problem(exc.problem, locale))
            return
        if link is None:
            await reply(interaction, t("{member} has no Steam link, so no squad.xml entry.", locale,
                                       member=member.mention))
            return
        s.squadxml.request()
        s.staff.post(t("squad.xml remark of {member} set by {who}: {remark}", s.settings.language,
                       member=member.mention, who=interaction.user.mention, remark=link.remark or "—"))
        shown = s.squadxml.remark_of(link, (role.id for role in member.roles))
        await reply(interaction, t("The remark of {member} in squad.xml: {remark}", locale, member=member.mention,
                                   remark=shown or "—"))

    # ---------------------------------------------------------------- reminders

    @links.command(name="remind", description="Send a member the Steam link reminder now")
    @app_commands.describe(member="The member")
    @require(Perm.MANAGE_LINKS)
    async def remind(self, interaction: discord.Interaction, member: discord.Member) -> None:
        s, locale = self.services, interaction.locale
        if await s.links.get(member.id) is not None:
            await reply(interaction, t("{member} is linked already.", locale, member=member.mention))
            return
        if {role.id for role in member.roles}.isdisjoint(s.ids.rank):
            await reply(interaction, t("{member} has no rank role, so squad.xml doesn't need them.", locale,
                                       member=member.mention))
            return
        await thinking(interaction)
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
    @require(Perm.MANAGE_LINKS)
    async def reminders(self, interaction: discord.Interaction, member: discord.Member, on: bool) -> None:
        s, locale = self.services, interaction.locale
        await s.links.mute(member.id, not on)
        await reply(interaction, t("Reminders for {member} are on.", locale, member=member.mention) if on else
                    t("Reminders for {member} are off until they link; /links remind still sends one.", locale,
                      member=member.mention))
