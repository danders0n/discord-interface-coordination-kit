"""What the attendance and the ratings add up to, privately (docs/PLAN.md, *Supervisor: attendance*, *Use*, *Who
sees what*, *Member profile*, *Mission feedback*):

- ``/attendance [member]``: the events attended and the rate; about oneself, and for the administration, also the
  no-shows, the excused and the recent events.
- ``/attendance-report period``: totals per member, bot admins (a staff command: Discord gives a command one
  visibility, so it is not a subcommand of the members' ``/attendance``).
- ``/profile [member]``: squad.xml nick and Steam link, the roles the bot reads, attendance, missions' rating.
- ``/mission-maker [member]``: the missions they authored with their averages; the comments for the author and
  the administration.
- ``/group acronym``: our events with another group; the cooperation ratings for the administration.

About themselves members see everything; about others, members see the basics, the administration (*manage any
event*, *approve events*, *bot admin*) the details."""

from __future__ import annotations

import csv
import io
from datetime import timedelta

import discord
from discord import app_commands
from discord.app_commands import locale_str
from discord.ext import commands

from app.attendance.history import History, Rated, Record, average
from app.discord_util import NotAllowed, access, reply, require
from app.eventchannel.render import mention, ts
from app.events.groups import Groups
from app.i18n import t
from app.permissions import Perm
from app.services import Services
from app.supervisor_client import SupervisorError

RECENT = 10
PERIODS = {"30": 30, "90": 90, "365": 365, "all": None}


def members_only():
    """Members who may sign up, and the administration."""

    async def predicate(interaction: discord.Interaction) -> bool:
        rights = access(interaction)
        if rights.may_sign_up or rights.is_administration:
            return True
        raise NotAllowed(t("This is for members.", interaction.locale))

    return app_commands.check(predicate)


def sees_details(interaction: discord.Interaction, member_id: int) -> bool:
    return member_id == interaction.user.id or access(interaction).is_administration


def cell(text: str) -> str:
    """A spreadsheet reads a cell starting with = + - @ as a formula: a display name stays text."""
    return f"'{text}" if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def percent(rate: float | None) -> str:
    return f"{rate:.0%}" if rate is not None else "—"


def stars(value: float | None, count: int, locale) -> str:
    if value is None:
        return t("no ratings yet", locale)
    return t(("{average} ★ ({count} rating)", "{average} ★ ({count} ratings)"), locale, average=f"{value:.1f}",
             count=count)


def status_label(status: str, locale) -> str:
    return {"attended": t("Attended", locale), "walk_in": t("Played without a slot", locale),
            "no_show": t("No-show", locale), "excused": t("Excused", locale), "to_confirm": t("To confirm", locale),
            "not_linked": t("Not linked", locale)}.get(status, status)


class AttendanceCog(commands.Cog):
    def __init__(self, services: Services) -> None:
        self.services = services
        self.history = History(services.db, services.scheduler.clock)

    def _name(self, guild: discord.Guild | None, member_id: int) -> str:
        member = guild.get_member(member_id) if guild is not None else None
        return getattr(member, "display_name", None) or str(member_id)

    def _record_fields(self, embed: discord.Embed, record: Record, details: bool, locale) -> None:
        embed.add_field(name=t("Events attended", locale), value=str(record.attended))
        embed.add_field(name=t("Attendance rate", locale), value=percent(record.rate))
        if details:
            embed.add_field(name=t("No-show", locale), value=str(record.no_show))
            embed.add_field(name=t("Excused", locale), value=str(record.excused))
            if record.recent:
                lines = [f"{ts(event.start_at, 'd')} · {event.title} · {status_label(status, locale)}"
                         for event, status in record.recent]
                embed.add_field(name=t("Recent events", locale), value="\n".join(lines)[:1024], inline=False)

    # ---------------------------------------------------------------- attendance

    @app_commands.command(name="attendance", description="Events attended and the attendance rate")
    @app_commands.describe(member="Whose (default: yours)")
    @app_commands.guild_only()
    @members_only()
    async def attendance(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        target = member or interaction.user
        locale = interaction.locale
        embed = discord.Embed(title=t("Attendance: {name}", locale, name=target.display_name))
        self._record_fields(embed, await self.history.member(target.id, RECENT), sees_details(interaction, target.id),
                            locale)
        await reply(interaction, embed=embed)

    @app_commands.command(name="attendance-report", description="Attendance totals per member for a period")
    @app_commands.describe(period="Which events")
    @app_commands.choices(period=[app_commands.Choice(name=locale_str("Last 30 days"), value="30"),
                                  app_commands.Choice(name=locale_str("Last 90 days"), value="90"),
                                  app_commands.Choice(name=locale_str("Last 365 days"), value="365"),
                                  app_commands.Choice(name=locale_str("All"), value="all")])
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    @require(Perm.BOT_ADMIN)
    async def report(self, interaction: discord.Interaction, period: str = "90") -> None:
        locale, days = interaction.locale, PERIODS.get(period, 90)
        since = self.services.scheduler.clock() - timedelta(days=days) if days else None
        totals = await self.history.totals(since)
        rows = sorted(totals.items(), key=lambda item: (-item[1].attended, item[1].no_show, item[0]))
        lines = [f"{mention(member_id)}: {r.attended} · {r.no_show} · {r.excused} · {percent(r.rate)}" for member_id, r in rows]
        title = t("Attendance in the last {days} days", locale, days=days) if days else t("Attendance, all events", locale)
        embed = discord.Embed(title=title, description="\n".join(lines)[:4000] if lines else t("Nobody yet.", locale))
        embed.set_footer(text=t("attended · no-show · excused · rate", locale))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["member_id", "name", "attended", "no_show", "excused", "rate"])
        for member_id, r in rows:
            writer.writerow([member_id, cell(self._name(interaction.guild, member_id)), r.attended, r.no_show, r.excused,
                             f"{r.rate:.3f}" if r.rate is not None else ""])
        await reply(interaction, embed=embed, file=discord.File(io.BytesIO(out.getvalue().encode("utf-8-sig")),
                                                                "attendance.csv"))

    # ---------------------------------------------------------------- profile

    @app_commands.command(name="profile", description="A member's squad.xml entry, roles, attendance and missions")
    @app_commands.describe(member="Whose (default: yours)")
    @app_commands.guild_only()
    @members_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        s, locale = self.services, interaction.locale
        target = member or interaction.user
        details = sees_details(interaction, target.id)
        embed = discord.Embed(title=t("Profile: {name}", locale, name=target.display_name))
        try:
            person = await s.links.person(target.id) if s.links is not None else None
        except SupervisorError:
            person = None
            embed.add_field(name=t("squad.xml", locale), value=t("The app can't be reached right now. Try again in a "
                                                                 "few minutes.", locale), inline=False)
        else:
            uid = (person or {}).get("steam_uid")
            if not uid:
                value = t("Not linked", locale)
            else:
                value = t("Linked", locale) if person.get("steam_state") == "confirmed" else \
                    t("Linked, waiting for staff to confirm", locale)
                value += f" · Steam ID {uid}" if details else ""
            embed.add_field(name=t("squad.xml", locale), value=value, inline=False)
        roles = {role.id for role in getattr(target, "roles", ())}
        ids = s.ids
        for name, found in ((t("Rank", locale), [r for r in ids.rank if r in roles]),
                            (t("Specializations", locale), [r for r in ids.remark if r in roles]),
                            (t("Climates", locale), [r for r in ids.climates if r in roles]),
                            (t("Creator DLC", locale), [r for r in ids.dlc.values() if r in roles])):
            if found:
                embed.add_field(name=name, value=" ".join(f"<@&{r}>" for r in found), inline=False)
        self._record_fields(embed, await self.history.member(target.id, 5), details, locale)
        made = await self.history.maker(target.id)
        if made:
            value, count = average(made)
            embed.add_field(name=t("Missions authored", locale),
                            value=f"{len(made)} · {stars(value, count, locale)}", inline=False)
        await reply(interaction, embed=embed)

    # ---------------------------------------------------------------- missions and groups

    def _rated_lines(self, rated: list[Rated], comments: bool, locale) -> list[str]:
        lines = []
        for item in rated:
            lines.append(f"{ts(item.event.start_at, 'd')} · **{item.event.title}** · "
                         f"{stars(item.average, len(item.ratings), locale)}")
            if comments:
                lines += [f"> {'★' * r.stars} {r.comment}" for r in item.ratings if r.comment]
        return lines

    @app_commands.command(name="mission-maker", description="The missions a member authored and how they were rated")
    @app_commands.describe(member="Whose (default: yours)")
    @app_commands.guild_only()
    @members_only()
    async def mission_maker(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        locale = interaction.locale
        target = member or interaction.user
        made = await self.history.maker(target.id)
        value, count = average(made)
        lines = self._rated_lines(made, sees_details(interaction, target.id), locale)
        embed = discord.Embed(title=t("Missions by {name}", locale, name=target.display_name),
                              description="\n".join(lines)[:4096] if lines else t("No missions yet.", locale))
        if made:
            embed.add_field(name=t("Average", locale), value=stars(value, count, locale))
        await reply(interaction, embed=embed)

    @app_commands.command(name="group", description="Our events with another group")
    @app_commands.describe(acronym="The group's acronym")
    @app_commands.guild_only()
    @members_only()
    async def group(self, interaction: discord.Interaction, acronym: str) -> None:
        locale = interaction.locale
        found = await Groups(self.services.db).search(acronym, 25)
        group = next((g for g in found if g.acronym.casefold() == acronym.strip().casefold()), None)
        if group is None:
            await reply(interaction, t("No group with that acronym.", locale))
            return
        events = await self.history.group(group.id)
        embed = discord.Embed(title=f"{group.acronym} · {group.name}")
        embed.add_field(name=t("Events with us", locale), value=str(len(events)))
        if access(interaction).is_administration:
            hosted = [item for item in events if item.event.host_group_id == group.id]  # guest events: cooperation
            value, count = average(hosted)
            embed.add_field(name=t("Cooperation", locale), value=stars(value, count, locale))
            lines = self._rated_lines(hosted, True, locale)
            if lines:
                embed.description = "\n".join(lines)[:4096]
        await reply(interaction, embed=embed)

    @group.autocomplete("acronym")
    async def group_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=f"{g.acronym} · {g.name}"[:100], value=g.acronym)
                for g in await Groups(self.services.db).search(current, 25)]
