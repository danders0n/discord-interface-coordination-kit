"""What the attendance and the ratings add up to, privately (docs/PLAN-front-end.md, *Commands and buttons*; how they
look: docs/PLAN.md, *Use*, *Member profile*, *Mission feedback*). Every command reads the supervisor's reports in
the member's name, so *who sees what* is the supervisor's (about oneself everything, about others the basics, staff
the details):

- ``/attendance [member]``: the operations attended and the rate (and the no-shows, excused and recent ones);
- ``/profile [member]``: the roles, the squad.xml link, attendance, the missions' rating;
- ``/mission-maker [member]``: the missions they authored with their averages (the comments where allowed);
- ``/group group``: our operations with another group (the cooperation ratings for staff);
- ``/attendance-report period``: totals per member with a CSV (staff: a command of its own, as Discord gives a
  command one visibility)."""

from __future__ import annotations

import csv
import io
from typing import Any

import discord
from discord import app_commands
from discord.app_commands import locale_str
from discord.ext import commands

from app.channel import render
from app.discord_util import reply, thinking
from app.i18n import t
from app.services import Services
from app.signup.ui import act

PERIODS = {"30": 30, "90": 90, "365": 365, "all": 0}


def percent(rate: float | None) -> str:
    return f"{rate:.0%}" if rate is not None else "—"


def cell(text: str) -> str:
    """A spreadsheet reads a cell starting with = + - @ as a formula: a display name stays text."""
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


class AttendanceCog(commands.Cog):
    def __init__(self, services: Services) -> None:
        self.services = services

    async def _report(self, interaction: discord.Interaction, kind: str,
                      member: discord.abc.User | None) -> dict[str, Any] | None:
        """The supervisor's report about ``member`` (default: the caller); None (answered) for someone it doesn't
        know."""
        who: str | int = "me"
        if member is not None and member.id != interaction.user.id:
            person = await self.services.links.person(member.id)
            if person is None:
                await reply(interaction, t("{member} is not on the app's member list yet.", interaction.locale,
                                           member=member.mention))
                return None
            who = int(person["id"])
        client = self.services.supervisor
        return await act(interaction, lambda me: client.report(me, kind, who))

    @staticmethod
    def _record(embed: discord.Embed, record: dict[str, Any], locale: Any) -> None:
        embed.add_field(name=t("Operations attended", locale), value=str(record.get("attended", 0)))
        embed.add_field(name=t("Attendance rate", locale), value=percent(record.get("rate")))
        if "no_show" in record:
            embed.add_field(name=t("No-show", locale), value=str(record.get("no_show", 0)))
            embed.add_field(name=t("Excused", locale), value=str(record.get("excused", 0)))
        recent = record.get("recent") or []
        if recent:
            lines = [f"{render.ts(render.when(op, 'start_at'), 'd')} · {render.plain(op['title'])} · "
                     f"{render.status_name(op.get('attendance') or '', locale)}" for op in recent]
            embed.add_field(name=t("Recent operations", locale), value="\n".join(lines)[:1024], inline=False)

    @app_commands.command(name="attendance", description="Operations attended and the attendance rate")
    @app_commands.describe(member="Whose (default: yours)")
    @app_commands.guild_only()
    async def attendance(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await thinking(interaction)
        found = await self._report(interaction, "member", member)
        if found is None:
            return
        name = render.plain(found["person"].get("display_name") or found["person"].get("username") or "?")
        embed = discord.Embed(title=t("Attendance: {name}", interaction.locale, name=name))
        self._record(embed, found.get("attendance") or {}, interaction.locale)
        await reply(interaction, embed=embed)

    @app_commands.command(name="profile", description="A member's squad.xml entry, roles, attendance and missions")
    @app_commands.describe(member="Whose (default: yours)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        locale = interaction.locale
        await thinking(interaction)
        found = await self._report(interaction, "member", member)
        if found is None:
            return
        name = render.plain(found["person"].get("display_name") or found["person"].get("username") or "?")
        embed = discord.Embed(title=t("Profile: {name}", locale, name=name))
        if "steam" in found:  # (only about oneself, and for staff)
            steam = found.get("steam") or {}
            if not steam.get("steam_id"):
                value = t("Not linked", locale)
            elif steam.get("state") == "confirmed":
                value = t("Linked", locale) + f" · Steam ID {steam['steam_id']}"
            else:
                value = t("Linked, waiting for staff to confirm", locale) + f" · Steam ID {steam['steam_id']}"
            embed.add_field(name=t("squad.xml", locale), value=value, inline=False)
        if found.get("roles"):
            embed.add_field(name=t("Roles", locale), value=render.plain(", ".join(found["roles"]))[:1024], inline=False)
        self._record(embed, found.get("attendance") or {}, locale)
        made = found.get("maker") or {}
        if made.get("missions"):
            embed.add_field(name=t("Missions authored", locale), value=f"{made['missions']} · " + render.stars(
                made.get("average"), int(made.get("ratings") or 0), locale), inline=False)
        await reply(interaction, embed=embed)

    @app_commands.command(name="mission-maker", description="The missions a member authored and how they were rated")
    @app_commands.describe(member="Whose (default: yours)")
    @app_commands.guild_only()
    async def mission_maker(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        locale = interaction.locale
        await thinking(interaction)
        found = await self._report(interaction, "maker", member)
        if found is None:
            return
        lines = []
        for op in found.get("operations") or []:
            lines.append(f"{render.ts(render.when(op, 'start_at'), 'd')} · **{render.plain(op['title'])}** · "
                         f"{render.stars(op.get('average'), int(op.get('count') or 0), locale)}")
            lines += [f"> {'★' * int(r['stars'])} {render.plain(r['comment'])}" for r in op.get("ratings") or []
                      if r.get("comment")]
        name = render.plain(found["person"].get("display_name") or found["person"].get("username") or "?")
        embed = discord.Embed(title=t("Missions by {name}", locale, name=name),
                              description="\n".join(lines)[:4096] if lines else t("No missions yet.", locale))
        summary = found.get("summary") or {}
        if summary.get("missions"):
            embed.add_field(name=t("Average", locale),
                            value=render.stars(summary.get("average"), int(summary.get("ratings") or 0), locale))
        await reply(interaction, embed=embed)

    @app_commands.command(name="group", description="Our operations with another group")
    @app_commands.describe(group="The group (start typing its acronym)")
    @app_commands.guild_only()
    async def group(self, interaction: discord.Interaction, group: str) -> None:
        locale = interaction.locale
        if not group.isdigit():
            await reply(interaction, t("Pick a group from the list.", locale))
            return
        await thinking(interaction)
        client = self.services.supervisor
        found = await act(interaction, lambda me: client.report(me, "group", int(group)))
        info = found.get("group") or {}
        embed = discord.Embed(title=f"{render.plain(info.get('acronym') or '?')} · {render.plain(info.get('name') or '')}")
        embed.add_field(name=t("Operations with us", locale), value=str(found.get("count", 0)))
        cooperation = found.get("cooperation")
        if cooperation is not None:  # (staff only)
            embed.add_field(name=t("Cooperation", locale), value=render.stars(
                cooperation.get("average"), int(cooperation.get("count") or 0), locale))
        lines = [f"{render.ts(render.when(op, 'start_at'), 'd')} · {render.plain(op['title'])}"
                 for op in (found.get("operations") or [])[:20]]
        if lines:
            embed.description = "\n".join(lines)[:4096]
        await reply(interaction, embed=embed)

    @group.autocomplete("group")
    async def group_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        client, typed = self.services.supervisor, current.strip().casefold()
        try:
            groups = await act(interaction, lambda me: client.groups(me))
        except Exception:  # noqa: BLE001 - autocomplete stays quiet when the supervisor refuses or is away
            return []
        return [app_commands.Choice(name=f"{g['acronym']} · {g['name']}"[:100], value=str(g["id"])) for g in groups
                if typed in f"{g['acronym']} {g['name']}".casefold()][:25]

    @app_commands.command(name="attendance-report", description="Attendance totals per member for a period")
    @app_commands.describe(period="Which operations")
    @app_commands.choices(period=[app_commands.Choice(name=locale_str("Last 30 days"), value="30"),
                                  app_commands.Choice(name=locale_str("Last 90 days"), value="90"),
                                  app_commands.Choice(name=locale_str("Last 365 days"), value="365"),
                                  app_commands.Choice(name=locale_str("All"), value="all")])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def report(self, interaction: discord.Interaction, period: str = "90") -> None:
        locale, days, client = interaction.locale, PERIODS.get(period, 90), self.services.supervisor
        await thinking(interaction)
        rows = await act(interaction, lambda me: client.attendance_totals(me, days))
        lines = [f"{render.person(r['person'])}: {r['attended']} · {r['no_show']} · {r['excused']} · "
                 f"{percent(r.get('rate'))}" for r in rows]
        title = t("Attendance in the last {days} days", locale, days=days) if days else t("Attendance, all operations",
                                                                                          locale)
        embed = discord.Embed(title=title, description="\n".join(lines)[:4000] if lines else t("Nobody yet.", locale))
        embed.set_footer(text=t("attended · no-show · excused · rate", locale))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["username", "display_name", "attended", "no_show", "excused", "rate"])
        for r in rows:
            writer.writerow([cell(str(r["person"].get("username") or "")), cell(str(r["person"].get("display_name") or "")),
                             r["attended"], r["no_show"], r["excused"], "" if r.get("rate") is None else r["rate"]])
        await reply(interaction, embed=embed, file=discord.File(io.BytesIO(out.getvalue().encode("utf-8-sig")),
                                                                "attendance.csv"))
