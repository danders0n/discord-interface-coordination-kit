"""The event channel's messages (docs/PLAN.md, *Event channel*, *Messages, in order*), drawn from the database.

Pure functions: an event and its board in, :class:`Message` payloads out, keyed in channel order ``info``,
``briefing``, ``preset`` (phase 4c), ``orbat:0``, ``orbat:1`` ... A Zeus event's sign-up list takes the ORBAT's
place. The ORBAT is one embed per squad then the lists, split into further messages before Discord's limits
(10 embeds, 6000 characters). Everything is in the public language. Mentions inside embeds never ping."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import discord

from app.db.models import Event, OtherGroup
from app.i18n import t
from app.presets import CDLCS
from app.signup.service import Board

MAX_EMBEDS = 10
MAX_TOTAL = 6000
MAX_DESCRIPTION = 4096
COLOURS = {"draft": 0x95A5A6, "pending": 0xF1C40F, "published": 0x2ECC71, "ended": 0x3498DB, "cancelled": 0xE74C3C}


@dataclass
class Message:
    content: str | None = None
    embeds: list[discord.Embed] = field(default_factory=list)
    view: discord.ui.View | None = None  # buttons, attached by the Discord side (a View needs a running loop)
    files: list[Path] = field(default_factory=list)  # attached on sending; on editing only when replace_files
    replace_files: bool = False

    @property
    def tag(self) -> str:
        """Changes when a file changes, so the Discord side knows to upload it again."""
        return ";".join(f"{p.name}:{p.stat().st_mtime_ns}" for p in self.files if p.is_file())

    def size(self) -> int:
        return sum(len(e) for e in self.embeds)


@dataclass
class Context:
    """What the info embed shows besides the event row."""

    language: str
    now: datetime
    host: OtherGroup | None = None
    guests: list[OtherGroup] = field(default_factory=list)
    coming: int = 0  # Zeus: how many are Coming
    server_confirmed: bool = True  # a member's Zeus event before its reservation is approved
    attendance: list = field(default_factory=list)  # the report's lines (AttendanceEntry)
    min_minutes: int = 0  # mission time that counts as attended
    rating: tuple[float | None, int] = (None, 0)  # the mission's average rating and how many (not a guest event's)


def ts(value: datetime, style: str) -> str:
    return discord.utils.format_dt(value, style)  # type: ignore[arg-type]


def duration(minutes: int, language: str) -> str:
    hours, rest = divmod(minutes, 60)
    if not rest:
        return t("{hours} h", language, hours=hours)
    if not hours:
        return t("{minutes} min", language, minutes=rest)
    return t("{hours} h {minutes} min", language, hours=hours, minutes=rest)


def kind_label(kind: str, language: str) -> str:
    return {"internal": t("Internal", language), "guest": t("Guest", language), "hosted": t("Hosted", language),
            "zeus": t("Zeus", language)}[kind]


def signup_state(event: Event, ctx: Context) -> str:
    language = ctx.language
    if event.status in ("draft", "pending"):
        return t("Opens when the event is published", language)
    if event.status != "published" or ctx.now >= event.start_at:
        return t("Closed", language)
    if event.signup_opens_at is not None and ctx.now < event.signup_opens_at:
        return t("Opens {when}", language, when=ts(event.signup_opens_at, "R"))
    return t("Open", language)


def info(event: Event, ctx: Context) -> Message:
    language = ctx.language
    marker = {"draft": t("DRAFT", language), "pending": t("WAITING FOR APPROVAL", language),
              "cancelled": t("CANCELLED", language), "ended": t("ENDED", language)}.get(event.status)
    embed = discord.Embed(title=f"{marker} · {event.title}" if marker else event.title,
                          colour=COLOURS.get(event.status, 0x2ECC71))
    if event.status == "cancelled" and event.cancel_reason:
        embed.description = t("Cancelled: {reason}", language, reason=event.cancel_reason)
    embed.add_field(name=t("Kind", language), value=kind_label(event.kind, language))
    if event.mission_type:
        embed.add_field(name=t("Mission type", language), value=event.mission_type)
    if event.climate_role_id:
        embed.add_field(name=t("Climate", language), value=f"<@&{event.climate_role_id}>")
    if ctx.host is not None:
        embed.add_field(name=t("Host", language), value=f"{ctx.host.acronym} · {ctx.host.name}")
    if ctx.guests:
        embed.add_field(name=t("Invited", language), value=", ".join(g.acronym for g in ctx.guests))
    if event.author_id:
        embed.add_field(name=t("Author", language), value=f"<@{event.author_id}>")
    embed.add_field(name=t("Date", language), value=ts(event.assembly_at, "D"), inline=False)
    embed.add_field(name=t("Assembly", language), value=f"{ts(event.assembly_at, 't')} ({ts(event.assembly_at, 'R')})")
    embed.add_field(name=t("Start", language), value=ts(event.start_at, "t"))
    embed.add_field(name=t("Duration", language), value=duration(event.duration_minutes, language))
    state = signup_state(event, ctx)
    if event.kind == "zeus" and event.signup_limit:
        state += " · " + t("{count} of {limit} coming", language, count=ctx.coming, limit=event.signup_limit)
    embed.add_field(name=t("Sign-up", language), value=state, inline=False)
    if event.kind == "zeus" and not ctx.server_confirmed:
        embed.add_field(name=t("Server", language), value=t("Not confirmed yet", language), inline=False)
    if event.preset_file:
        dlc = ", ".join(CDLCS[k].name for k in event.required_dlc or () if k in CDLCS)
        modset = t(("{name}: {count} mod", "{name}: {count} mods"), language, name=event.preset_name or t("Preset", language),
                   count=len(event.preset_mods or ()))
        if dlc:
            modset += "\n" + t("Creator DLC: {dlc}", language, dlc=dlc)
        embed.add_field(name=t("Modset", language), value=modset, inline=False)
    if event.kind == "guest" and event.how_to_join:
        embed.add_field(name=t("How to join", language), value=event.how_to_join[:1024], inline=False)
    return Message(embeds=[embed])


def briefing(event: Event, language: str) -> Message:
    parts = [(t("Situation", language), event.briefing_situation), (t("Mission", language), event.briefing_mission),
             (t("Notes", language), event.briefing_notes)]
    text = "\n\n".join(f"**{name}**\n{body}" for name, body in parts if body.strip())
    embed = discord.Embed(title=t("Briefing", language), description=(text or t("Not written yet.", language))[:MAX_DESCRIPTION])
    image = Path(event.briefing_image) if event.briefing_image else None
    if image is not None and image.is_file():
        embed.set_image(url=f"attachment://{image.name}")
        return Message(embeds=[embed], files=[image])
    return Message(embeds=[embed])


def _people(ids: list[int], language: str, limit: int = 1000) -> str:
    """Mentions that fit ``limit`` characters, then "and N more"."""
    out, used = [], 0
    for index, member_id in enumerate(ids):
        mention = f"<@{member_id}>"
        if used + len(mention) + 2 > limit:
            return ", ".join(out) + " " + t("and {count} more", language, count=len(ids) - index)
        out.append(mention)
        used += len(mention) + 2
    return ", ".join(out) or "—"


def _slot_line(slot, offered: bool, language: str) -> str:
    role = f" <@&{slot.required_role_id}>" if slot.required_role_id else ""
    if slot.holder_id is not None:
        holder = f"<@{slot.holder_id}>" + (" 🔒" if slot.locked else "")
    elif slot.locked:
        holder = "🔒 " + t("closed", language)
    elif offered:
        holder = "⏳ " + t("offered", language)
    else:
        holder = "—"
    return f"**{slot.name}**{role}: {holder}"


def _pages(embeds: list[discord.Embed]) -> list[Message]:
    pages: list[Message] = [Message()]
    for embed in embeds:
        page = pages[-1]
        if page.embeds and (len(page.embeds) >= MAX_EMBEDS or page.size() + len(embed) > MAX_TOTAL):
            pages.append(page := Message())
        page.embeds.append(embed)
    return pages


def orbat(board: Board, language: str) -> list[Message]:
    embeds = []
    for squad, slots in board.squads:
        lines = [_slot_line(slot, slot.id in board.offered, language) for slot in slots]
        taken = sum(slot.holder_id is not None for slot in slots)
        embeds.append(discord.Embed(title=f"{squad.name} ({taken}/{len(slots)})",
                                    description="\n".join(lines)[:MAX_DESCRIPTION]))
    if not embeds:
        embeds.append(discord.Embed(title=t("ORBAT", language), description=t("Not set yet.", language)))
    lists = discord.Embed(title=t("Other answers", language))
    for status, name in (("maybe", t("Maybe", language)), ("cant", t("Can't come", language)),
                         ("reserve", t("Reserve list", language))):
        members = board.status(status)
        if members:
            lists.add_field(name=f"{name} ({len(members)})", value=_people(members, language), inline=False)
    if lists.fields:
        embeds.append(lists)
    return _pages(embeds)


def zeus_list(board: Board, event: Event, language: str) -> list[Message]:
    coming, not_coming = board.status("coming"), board.status("cant")
    limit = f"/{event.signup_limit}" if event.signup_limit else ""
    embed = discord.Embed(title=t("Sign-up", language))
    embed.add_field(name=f"{t('Coming', language)} ({len(coming)}{limit})", value=_people(coming, language), inline=False)
    embed.add_field(name=f"{t('Not coming', language)} ({len(not_coming)})", value=_people(not_coming, language),
                    inline=False)
    return [Message(embeds=[embed])]


def messages(event: Event, board: Board, ctx: Context) -> dict[str, Message]:
    """Every message of the channel, keyed in channel order."""
    found = {"info": info(event, ctx), "briefing": briefing(event, ctx.language)}
    preset = Path(event.preset_file) if event.preset_file else None
    if preset is not None and preset.is_file():
        found["preset"] = Message(content=t("Modset for the Arma 3 Launcher: {name}", ctx.language,
                                           name=event.preset_name or preset.name), files=[preset])
    pages = zeus_list(board, event, ctx.language) if event.kind == "zeus" else orbat(board, ctx.language)
    found |= {f"orbat:{i}": page for i, page in enumerate(pages)}
    if event.attendance_state in ("reported", "unavailable"):
        found["report"] = report(event, ctx)
    return found


def _items(items: list[str], language: str, limit: int = 1000) -> str:
    """Lines that fit ``limit`` characters, then "and N more"."""
    out, used = [], 0
    for index, item in enumerate(items):
        if used + len(item) + 2 > limit:
            return ", ".join(out) + " " + t("and {count} more", language, count=len(items) - index)
        out.append(item)
        used += len(item) + 2
    return ", ".join(out) or "—"


def _who(entry) -> str:
    if entry.member_id is not None:
        return f"<@{entry.member_id}>"
    return discord.utils.escape_markdown(entry.name or entry.steam_id or "?")


def report(event: Event, ctx: Context) -> Message:
    """The attendance report (docs/PLAN.md, *Supervisor: attendance*, *Report*)."""
    language, entries = ctx.language, ctx.attendance
    embed = discord.Embed(title=t("Attendance", language), colour=COLOURS["ended"])
    if event.attendance_state == "unavailable":
        embed.description = t("The server's statistics couldn't be read; the organisers can fill the attendance in "
                              "with Edit attendance.", language)
    for status, name in (("attended", t("Attended", language)), ("no_show", t("No-show", language)),
                         ("excused", t("Excused", language)), ("to_confirm", t("To confirm", language))):
        found = [_who(e) for e in entries if e.status == status]
        if found:
            embed.add_field(name=f"{name} ({len(found)})", value=_items(found, language), inline=False)
    unlinked = [e for e in entries if e.status == "not_linked"]
    if unlinked:
        lines = [_who(e) + (" (" + t("probably {name}", language, name=discord.utils.escape_markdown(
            e.probable_name or "")) + ")" if e.probable_steam_id else "") for e in unlinked]
        embed.add_field(name=f"{t('Not linked', language)} ({len(unlinked)})", value=_items(lines, language),
                        inline=False)
    walk_ins = [e for e in entries if e.status == "walk_in"]
    if walk_ins:
        embed.add_field(name=f"{t('Played without a slot', language)} ({len(walk_ins)})",
                        value=_items([_who(e) for e in walk_ins], language), inline=False)
    if event.attendance_guests:
        embed.add_field(name=t("Guests", language), value=t(("{count} player", "{count} players"), language,
                                                            count=event.attendance_guests), inline=False)
    if not embed.fields and not embed.description:
        embed.description = t("Nobody played.", language)
    average, count = ctx.rating
    if event.kind != "guest" and count:
        embed.add_field(name=t("Rating", language), value=t(("{average} ★ ({count} rating)", "{average} ★ ({count} "
                                                             "ratings)"), language, average=f"{average:.1f}",
                                                            count=count), inline=False)
    if event.kind != "guest" and event.attendance_state == "reported":
        embed.set_footer(text=t("Attended: at least {minutes} min of mission time.", language, minutes=ctx.min_minutes))
    return Message(embeds=[embed])


def announcement(event: Event, ctx: Context, channel_id: int | None) -> Message:
    """The notice in the announcements channel; its mentions are in the content (sent once, never re-pinged)."""
    language = ctx.language
    marker = t("CANCELLED", language) + " · " if event.status == "cancelled" else ""
    embed = discord.Embed(title=f"{marker}{event.title}", colour=COLOURS.get(event.status, 0x2ECC71),
                          description=f"{kind_label(event.kind, language)} · {event.mission_type}\n"
                                      f"{ts(event.assembly_at, 'F')} ({ts(event.assembly_at, 'R')})")
    if channel_id:
        embed.add_field(name=t("Details and sign-up", language), value=f"<#{channel_id}>")
    return Message(embeds=[embed])
