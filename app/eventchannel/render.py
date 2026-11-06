"""The event channel's messages (docs/PLAN.md, *Event channel*, *Messages, in order*), drawn from the database.

Pure functions: an event and its board in, :class:`Message` payloads out, keyed in channel order ``info``,
``briefing``, ``briefing:1`` ..., ``preset``, ``orbat:0``, ``orbat:1`` ..., ``report``, ``discussion``. Only the
info and the report are embeds; the briefing and the ORBAT are plain text (decided 2026-11-01), continued in further
messages past Discord's 2000 characters. A Zeus or training event's sign-up list takes the ORBAT's place. The last
message points to the discussion thread (Discord's own "started a thread" card while it is still last, see
``sync``). The briefing, the ORBAT and the report open with an empty line (``SPACER``), so the sections stand apart.
Everything is in the public language; mentions are sent without pinging, and edits never ping."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import discord

from app.db.models import Event, OtherGroup
from app.i18n import t
from app.presets import CDLCS
from app.signup.service import COMING, Board

MAX_CONTENT = 2000
MARKDOWN = re.compile(r"([\\*_`~|>#])")
SPACER = "​\n"  # an empty first line: Discord trims a plain one
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
    kind_names: Any = None  # settings.events.kind_names


def mention(member_id: int) -> str:
    """A member's mention, or the pseudonym of an erased one (``former:<n>``, stored as ``-n``)."""
    return f"<@{member_id}>" if member_id > 0 else f"former:{-member_id}"


def ts(value: datetime, style: str) -> str:
    return discord.utils.format_dt(value, style)  # type: ignore[arg-type]


def duration(minutes: int, language: str) -> str:
    hours, rest = divmod(minutes, 60)
    if not rest:
        return t("{hours} h", language, hours=hours)
    if not hours:
        return t("{minutes} min", language, minutes=rest)
    return t("{hours} h {minutes} min", language, hours=hours, minutes=rest)


def kind_label(kind: str, language: str, names: Any = None) -> str:
    """The kind's name from the settings (``settings.events.kind_names``), else the bot's own in ``language``."""
    return getattr(names, kind, None) or {
        "internal": t("Internal", language), "guest": t("Guest", language), "hosted": t("Hosted", language),
        "zeus": t("Zeus", language), "training": t("Training", language)}[kind]


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
    embed.add_field(name=t("Kind", language), value=kind_label(event.kind, language, ctx.kind_names))
    if event.climate_role_id:
        embed.add_field(name=t("Climate", language), value=f"<@&{event.climate_role_id}>")
    if ctx.host is not None:
        embed.add_field(name=t("Host", language), value=f"{ctx.host.acronym} · {ctx.host.name}")
    if ctx.guests:
        embed.add_field(name=t("Invited", language), value=", ".join(g.acronym for g in ctx.guests))
    if event.author_id:
        embed.add_field(name=t("Author", language), value=mention(event.author_id))
    embed.add_field(name=t("Date", language), value=ts(event.assembly_at, "D"), inline=False)
    embed.add_field(name=t("Assembly", language), value=f"{ts(event.assembly_at, 't')} ({ts(event.assembly_at, 'R')})")
    embed.add_field(name=t("Start", language), value=ts(event.start_at, "t"))
    embed.add_field(name=t("Duration", language), value=duration(event.duration_minutes, language))
    state = signup_state(event, ctx)
    if event.kind in COMING and event.signup_limit:
        state += " · " + t("{count} of {limit} coming", language, count=ctx.coming, limit=event.signup_limit)
    embed.add_field(name=t("Sign-up", language), value=state, inline=False)
    if event.kind in COMING and not ctx.server_confirmed:
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


def texts(blocks: list[str], separator: str = "\n", limit: int = MAX_CONTENT) -> list[str]:
    """``blocks`` (a squad, a briefing section) one after another in messages of at most ``limit`` characters. A
    block that doesn't fit goes whole onto the next message; only one longer than a message is cut, at its lines."""
    pages = [""]
    for block in blocks:
        lines = [block] if len(block) <= limit else [
            line[i:i + limit] for line in block.split("\n") for i in range(0, max(len(line), 1), limit)]
        for number, line in enumerate(lines):
            glue = ("\n" if number else separator) if pages[-1] else ""
            if len(pages[-1]) + len(glue) + len(line) > limit:
                pages.append(line)
            else:
                pages[-1] += glue + line
    return pages


def briefing(event: Event, language: str) -> list[Message]:
    """The briefing's messages; the image goes with the first."""
    parts = [(t("Situation", language), event.briefing_situation), (t("Mission", language), event.briefing_mission),
             (t("Notes", language), event.briefing_notes)]
    blocks = [f"**{name}**\n{body.strip()}" for name, body in parts if body.strip()] or [t("Not written yet.", language)]
    pages = [Message(content=text) for text in texts([f"{SPACER}## {t('Briefing', language)}", *blocks], "\n\n")]
    image = Path(event.briefing_image) if event.briefing_image else None
    if image is not None and image.is_file():
        pages[0].files = [image]
    return pages


def _people(ids: list[int], language: str, limit: int = 1000) -> str:
    """Mentions that fit ``limit`` characters, then "and N more"."""
    out, used = [], 0
    for index, member_id in enumerate(ids):
        text = mention(member_id)
        if used + len(text) + 2 > limit:
            return ", ".join(out) + " " + t("and {count} more", language, count=len(ids) - index)
        out.append(text)
        used += len(text) + 2
    return ", ".join(out) or "—"


def _slot_line(number: int, slot, offered: bool, language: str) -> str:
    """```1.` Name (@Role) — holder``; a free, closed or offered slot in words."""
    role = f" (<@&{slot.required_role_id}>)" if slot.required_role_id else ""
    if slot.holder_id is not None:
        holder = mention(slot.holder_id) + (" \U0001F512" if slot.locked else "")
    elif slot.locked:
        holder = f"\U0001F512 *{t('closed', language)}*"
    elif offered:
        holder = f"*{t('offered', language)}*"
    else:
        holder = f"*{t('free', language)}*"
    return f"`{number}.` {_plain(slot.name)}{role} — {holder}"


def _plain(text: str) -> str:
    """Typed names shown as typed: markdown characters escaped."""
    return MARKDOWN.sub(r"\\\1", text)


def _listed(ids: list[int], language: str, limit: int = 1500) -> str:
    """One mention per line, within ``limit`` characters, then "and N more"."""
    out, used = [], 0
    for index, member_id in enumerate(ids):
        line = mention(member_id)
        if used + len(line) + 1 > limit:
            return "\n".join(out + [t("and {count} more", language, count=len(ids) - index)])
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) or "—"


def _state(event: Event, ctx: Context) -> str:
    """The sign-up's state with a coloured dot: after publishing, from a time, open until the start, closed."""
    language = ctx.language
    if event.status in ("draft", "pending"):
        return "\u26AA **" + t("Sign-up after publishing", language) + "**"
    if event.status != "published" or ctx.now >= event.start_at:
        return "\U0001F534 **" + t("Sign-up closed", language) + "**"
    if event.signup_opens_at is not None and ctx.now < event.signup_opens_at:
        return "\U0001F7E1 **" + t("Sign-up opens {when}", language, when=ts(event.signup_opens_at, "f")) + "**"
    return "\U0001F7E2 **" + t("Sign-up open", language) + "** – " + t("closes {when}", language,
                                                                       when=ts(event.start_at, "f"))


def is_open(event: Event, now: datetime) -> bool:
    return event.status == "published" and now < event.start_at and (
        event.signup_opens_at is None or now >= event.signup_opens_at)


def _header(event: Event, ctx: Context, counted: str, locked: int = 0) -> str:
    """``## Sign-up: title``, the start, how many are in (``counted``) and the slots locked, the state."""
    line = f"{t('Start: {when}', ctx.language, when=ts(event.start_at, 'F'))} • {counted}"
    if locked:
        line += f" • \U0001F512 {locked}"
    return SPACER + "\n".join([f"## {t('Sign-up: {title}', ctx.language, title=_plain(event.title))}", line,
                               _state(event, ctx)])


def orbat(event: Event, board: Board, ctx: Context) -> list[Message]:
    """The header, a block per squad (its name in bold, its slots numbered), the other answers, and while sign-up
    is open the hint to the slot lists under the last message (decided 2026-11-01)."""
    language = ctx.language
    slots = board.slots
    taken = sum(slot.holder_id is not None for slot in slots)
    locked = sum(slot.locked and slot.holder_id is None for slot in slots)
    blocks = [_header(event, ctx, t("Taken slots: {taken}", language, taken=f"**{taken}/{len(slots)}**"), locked)]
    for squad, squad_slots in board.squads:
        lines = [_slot_line(n, slot, slot.id in board.offered, language) for n, slot in enumerate(squad_slots, 1)]
        blocks.append("\n".join([f"**{_plain(squad.name)}**", *lines]))
    if not board.squads:
        blocks.append(t("Not set yet.", language))
    for status, name in (("maybe", t("Maybe", language)), ("cant", t("Can't come", language)),
                         ("reserve", t("Reserve list", language))):
        if members := board.status(status):
            blocks.append(f"**{name}** ({len(members)})\n{_people(members, language)}")
    if board.squads and is_open(event, ctx.now):
        blocks.append("-# " + t("Pick your squad from the list below, then the slot, and press Sign up. Taking another slot moves "
                                "your sign-up.",
                                language))
    return [Message(content=text) for text in texts(blocks, "\n\n")]


def zeus_list(event: Event, board: Board, ctx: Context) -> list[Message]:
    """The header, then Coming and Not coming, one member per line."""
    language = ctx.language
    coming, not_coming = board.status("coming"), board.status("cant")
    limit = f"/{event.signup_limit}" if event.signup_limit else ""
    blocks = [_header(event, ctx, t("Coming: {count}", language, count=f"**{len(coming)}{limit}**")),
              f"**{t('Coming', language)}** ({len(coming)}{limit})\n{_listed(coming, language)}",
              f"**{t('Not coming', language)}** ({len(not_coming)})\n{_listed(not_coming, language)}"]
    return [Message(content=text) for text in texts(blocks, "\n\n")]


def messages(event: Event, board: Board, ctx: Context) -> dict[str, Message]:
    """Every message of the channel, keyed in channel order."""
    found = {"info": info(event, ctx)}
    found |= {"briefing" if i == 0 else f"briefing:{i}": page for i, page in enumerate(briefing(event, ctx.language))}
    preset = Path(event.preset_file) if event.preset_file else None
    if preset is not None and preset.is_file():
        found["preset"] = Message(content=t("Modset for the Arma 3 Launcher: {name}", ctx.language,
                                           name=event.preset_name or preset.name), files=[preset])
    pages = zeus_list(event, board, ctx) if event.kind in COMING else orbat(event, board, ctx)
    found |= {f"orbat:{i}": page for i, page in enumerate(pages)}
    if event.attendance_state in ("reported", "unavailable"):
        found["report"] = report(event, ctx)
    if event.thread_id:
        found["discussion"] = Message(content=f"💬 <#{event.thread_id}>")
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
        return mention(entry.member_id)
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
    return Message(content="​", embeds=[embed])  # the empty line above it


def announcement(event: Event, ctx: Context, channel_id: int | None) -> Message:
    """The notice in the announcements channel; its mentions are in the content (sent once, never re-pinged)."""
    language = ctx.language
    marker = t("CANCELLED", language) + " · " if event.status == "cancelled" else ""
    embed = discord.Embed(title=f"{marker}{event.title}", colour=COLOURS.get(event.status, 0x2ECC71),
                          description=f"{kind_label(event.kind, language, ctx.kind_names)}\n"
                                      f"{ts(event.assembly_at, 'F')} ({ts(event.assembly_at, 'R')})")
    if channel_id:
        embed.add_field(name=t("Details and sign-up", language), value=f"<#{channel_id}>")
    return Message(embeds=[embed])
