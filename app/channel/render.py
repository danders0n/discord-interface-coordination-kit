"""The operation channel's messages (docs/PLAN-front-end.md, *Sync and drawing*, *Render*), drawn from the operation
as the supervisor returns it (its JSON, ``GET /api/operations/changes``).

Pure functions: an operation and a :class:`Context` in, :class:`Message` payloads out, keyed in channel order:
``info`` (an embed), the OPORD's paragraphs (``situation``, ``mission``, ``execution``, ``sustainment``, ``command``;
each its own message, continued in ``<paragraph>:1`` ... past Discord's 2000 characters; the banner and the map go
with the first), ``preset`` (the launcher file), then ``orbat:0`` ... (the ORBAT, or a Zeus or training operation's
Coming list; the sign-up buttons go on the last, see ``app.signup.ui``), and the link to the discussion thread last
(Discord's own "started a thread" card while it is still last, see ``sync``).
The paragraphs are the organisers' Markdown, shown as Discord renders it; an unwritten one is left out. Everything is
in the public language; mentions are sent without pinging, and edits never ping."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import discord

from app.i18n import t

MAX_CONTENT = 2000
MARKDOWN = re.compile(r"([\\*_`~|>#])")
SPACER = "​\n"  # an empty first line: Discord trims a plain one
COLOURS = {"draft": 0x95A5A6, "pending": 0xF1C40F, "published": 0x2ECC71, "ended": 0x3498DB, "cancelled": 0xE74C3C}
PARAGRAPHS = ("situation", "mission", "execution", "sustainment", "command")
OPEN = ("draft", "pending", "published")
COMING = ("zeus", "training")  # no ORBAT: Coming / Not coming


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
    """What the messages need besides the operation."""

    language: str
    now: datetime
    files: dict[str, Path] = field(default_factory=dict)  # slot (preset, map, banner) -> the file on disk
    thread_id: int | None = None


def when(op: dict[str, Any], name: str) -> datetime | None:
    """One of the operation's times (ISO, zone-aware) as a datetime."""
    value = op.get(name)
    return datetime.fromisoformat(value) if value else None


def ts(value: datetime, style: str) -> str:
    return discord.utils.format_dt(value, style)  # type: ignore[arg-type]


def duration(minutes: int, language: str) -> str:
    hours, rest = divmod(minutes, 60)
    if not rest:
        return t("{hours} h", language, hours=hours)
    if not hours:
        return t("{minutes} min", language, minutes=rest)
    return t("{hours} h {minutes} min", language, hours=hours, minutes=rest)


def plain(text: str) -> str:
    """Typed names shown as typed: markdown characters escaped."""
    return MARKDOWN.sub(r"\\\1", text or "")


def person(entry: dict[str, Any] | None) -> str:
    """A person of the operation: their mention when the supervisor gave the Discord id, else their name."""
    if not entry:
        return "—"
    discord_id = str(entry.get("discord_id") or "")
    return f"<@{discord_id}>" if discord_id.isdigit() else plain(entry.get("display_name") or entry.get("username") or "?")


def organisers(op: dict[str, Any]) -> list[int]:
    """The Discord ids of the operation's author and co-organisers (the creator is the author unless changed)."""
    found = []
    for entry in [op.get("author"), *(op.get("organisers") or [])]:
        discord_id = str((entry or {}).get("discord_id") or "")
        if discord_id.isdigit():
            found.append(int(discord_id))
    return list(dict.fromkeys(found))


def signup_state(op: dict[str, Any], now: datetime, language: str) -> str:
    start, opens = when(op, "start_at"), when(op, "signup_opens_at")
    if op["status"] in ("draft", "pending"):
        return t("Opens when the operation is published", language)
    if op["status"] != "published" or (start is not None and now >= start):
        return t("Closed", language)
    if opens is not None and now < opens:
        return t("Opens {when}", language, when=ts(opens, "R"))
    return t("Open", language)


def marker(status: str, language: str) -> str | None:
    return {"draft": t("DRAFT", language), "pending": t("WAITING FOR APPROVAL", language),
            "cancelled": t("CANCELLED", language), "ended": t("ENDED", language)}.get(status)


def info(op: dict[str, Any], ctx: Context) -> Message:
    language, status = ctx.language, op["status"]
    mark = marker(status, language)
    embed = discord.Embed(title=f"{mark} · {op['title']}" if mark else op["title"], colour=COLOURS.get(status, 0x2ECC71))
    if status == "cancelled" and op.get("cancel_reason"):
        embed.description = t("Cancelled: {reason}", language, reason=op["cancel_reason"])
    embed.add_field(name=t("Kind", language), value=op.get("kind_name") or op["kind"])
    if op.get("climate_role_id"):
        embed.add_field(name=t("Climate", language), value=f"<@&{op['climate_role_id']}>")
    elif op.get("climate"):
        embed.add_field(name=t("Climate", language), value=plain(op["climate"]))
    host = op.get("host_group")
    if host:
        embed.add_field(name=t("Host", language), value=f"{plain(host['acronym'])} · {plain(host['name'])}")
    if op.get("groups"):
        embed.add_field(name=t("Invited", language), value=", ".join(plain(g["acronym"]) for g in op["groups"]))
    if op.get("author"):
        embed.add_field(name=t("Author", language), value=person(op["author"]))
    assembly, start = when(op, "assembly_at"), when(op, "start_at")
    embed.add_field(name=t("Date", language), value=ts(assembly, "D"), inline=False)
    embed.add_field(name=t("Assembly", language), value=f"{ts(assembly, 't')} ({ts(assembly, 'R')})")
    embed.add_field(name=t("Start", language), value=ts(start, "t"))
    embed.add_field(name=t("Duration", language), value=duration(op["duration_minutes"], language))
    embed.add_field(name=t("Sign-up", language), value=signup_state(op, ctx.now, language), inline=False)
    preset = (op.get("files") or {}).get("preset")
    if preset:
        modset = t(("{name}: {count} mod", "{name}: {count} mods"), language,
                   name=plain(preset.get("preset_name") or t("Preset", language)), count=len(preset.get("mods") or ()))
        if preset.get("cdlc_names"):
            modset += "\n" + t("Creator DLC: {dlc}", language, dlc=", ".join(preset["cdlc_names"]))
        embed.add_field(name=t("Modset", language), value=modset[:1024], inline=False)
    mission = op.get("mission") or {}
    if mission.get("name"):
        embed.add_field(name=t("Mission", language), value=plain(mission["name"]), inline=False)
    if op["kind"] == "guest" and op.get("how_to_join"):
        embed.add_field(name=t("How to join", language), value=op["how_to_join"][:1024], inline=False)
    return Message(embeds=[embed])


def texts(blocks: list[str], separator: str = "\n", limit: int = MAX_CONTENT) -> list[str]:
    """``blocks`` (a squad, a paragraph) one after another in messages of at most ``limit`` characters. A block that
    doesn't fit goes whole onto the next message; only one longer than a message is cut, at its lines."""
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


def heading(paragraph: str, language: str) -> str:
    return {"situation": t("Situation", language), "mission": t("Mission", language),
            "execution": t("Execution", language), "sustainment": t("Sustainment", language),
            "command": t("Command & signal", language)}[paragraph]


def opord(op: dict[str, Any], ctx: Context) -> dict[str, Message]:
    """One message per written paragraph (``## Heading`` and the text), split past 2000 characters; the banner and
    the map go with the first. Nothing written: one message saying so, under ``situation``."""
    language, written = ctx.language, op.get("opord") or {}
    found: dict[str, Message] = {}
    for paragraph in PARAGRAPHS:
        body = written.get(paragraph)
        if not isinstance(body, str) or not body.strip():
            continue
        lines = [f"{SPACER}## {heading(paragraph, language)}", *body.strip().split("\n")]
        for number, page in enumerate(texts(lines)):
            found[paragraph if number == 0 else f"{paragraph}:{number}"] = Message(content=page)
    if not found:
        found["situation"] = Message(content=f"{SPACER}## {t('OPORD', language)}\n{t('Not written yet.', language)}")
    first = next(iter(found.values()))
    first.files = [ctx.files[slot] for slot in ("banner", "map") if slot in ctx.files and ctx.files[slot].is_file()]
    return found


def phase(op: dict[str, Any], now: datetime) -> str:
    """The sign-up's state now, as the supervisor names it (``not_published``, ``not_open``, ``open``, ``closed``),
    from the times: the operation as last read may be older than the opening or the start."""
    if op["status"] in ("draft", "pending"):
        return "not_published"
    start, opens = when(op, "start_at"), when(op, "signup_opens_at")
    if op["status"] != "published" or (start is not None and now >= start):
        return "closed"
    return "not_open" if opens is not None and now < opens else "open"


def _mentions(entries: list[dict[str, Any]], language: str, glue: str, limit: int) -> str:
    """The persons (mentions or names) that fit ``limit`` characters, then "and N more"."""
    out, used = [], 0
    for index, entry in enumerate(entries):
        text = person(entry)
        if used + len(text) + len(glue) > limit:
            return glue.join(out) + glue + t("and {count} more", language, count=len(entries) - index)
        out.append(text)
        used += len(text) + len(glue)
    return glue.join(out) or "—"


def _slot_line(number: int, slot: dict[str, Any], language: str) -> str:
    """```1.` Name (@Role) — holder``; a free, closed or offered slot in words."""
    role = slot.get("required_role") or {}
    role_text = f" (<@&{role['id']}>)" if str(role.get("id") or "").isdigit() else (
        f" ({plain(role.get('name') or '')})" if role else "")
    if slot.get("holder"):
        holder = person(slot["holder"]) + (" \U0001F512" if slot.get("locked") else "") + (
            " ⚠" if slot.get("flagged") else "")
    elif slot.get("locked"):
        holder = f"\U0001F512 *{t('closed', language)}*"
    elif slot.get("offered"):
        holder = f"*{t('offered', language)}*"
    else:
        holder = f"*{t('free', language)}*"
    return f"`{number}.` {plain(slot['name'])}{role_text} — {holder}"


def _state_line(op: dict[str, Any], now: datetime, language: str) -> str:
    """The sign-up's state with a coloured dot: after publishing, from a time, open until the start, closed."""
    state = phase(op, now)
    if state == "not_published":
        return "⚪ **" + t("Sign-up after publishing", language) + "**"
    if state == "closed":
        return "\U0001F534 **" + t("Sign-up closed", language) + "**"
    if state == "not_open":
        return "\U0001F7E1 **" + t("Sign-up opens {when}", language, when=ts(when(op, "signup_opens_at"), "f")) + "**"
    return "\U0001F7E2 **" + t("Sign-up open", language) + "** – " + t("closes {when}", language,
                                                                       when=ts(when(op, "start_at"), "f"))


def _header(op: dict[str, Any], ctx: Context, counted: str, closed: int = 0) -> str:
    """``## Sign-up: title``, the start, how many are in (``counted``) and the slots closed, the state."""
    line = f"{t('Start: {when}', ctx.language, when=ts(when(op, 'start_at'), 'F'))} • {counted}"
    if closed:
        line += f" • \U0001F512 {closed}"
    return SPACER + "\n".join([f"## {t('Sign-up: {title}', ctx.language, title=plain(op['title']))}", line,
                               _state_line(op, ctx.now, ctx.language)])


def orbat(op: dict[str, Any], ctx: Context) -> list[Message]:
    """The header, a block per squad (its name in bold, its slots numbered), the other answers, and while sign-up
    is open the hint to the slot lists under the last message (docs/PLAN.md, decided 2026-11-01)."""
    language, squads, lists = ctx.language, op.get("orbat") or [], op.get("signups") or {}
    counts = op.get("slots") or {}
    blocks = [_header(op, ctx, t("Taken slots: {taken}", language,
                                 taken=f"**{counts.get('taken', 0)}/{counts.get('total', 0)}**"), counts.get("closed", 0))]
    for squad in squads:
        lines = [_slot_line(n, slot, language) for n, slot in enumerate(squad.get("slots") or [], 1)]
        blocks.append("\n".join([f"**{plain(squad['name'])}**", *lines]))
    if not squads:
        blocks.append(t("Not set yet.", language))
    for status, name in (("maybe", t("Maybe", language)), ("cant", t("Can't come", language)),
                         ("reserve", t("Reserve list", language))):
        if entries := lists.get(status):
            blocks.append(f"**{name}** ({len(entries)})\n{_mentions(entries, language, ', ', 1000)}")
    if squads and phase(op, ctx.now) == "open":
        blocks.append("-# " + t("Pick your squad from the list below, then the slot, and press Sign up. Taking "
                                "another slot moves your sign-up.", language))
    return [Message(content=text) for text in texts(blocks, "\n\n")]


def coming_list(op: dict[str, Any], ctx: Context) -> list[Message]:
    """A Zeus or training operation: the header, then Coming and Not coming, one person per line."""
    language, lists = ctx.language, op.get("signups") or {}
    coming, not_coming = lists.get("coming") or [], lists.get("cant") or []
    limit = f"/{op['signup_limit']}" if op.get("signup_limit") else ""
    blocks = [_header(op, ctx, t("Coming: {count}", language, count=f"**{len(coming)}{limit}**")),
              f"**{t('Coming', language)}** ({len(coming)}{limit})\n{_mentions(coming, language, chr(10), 1500)}",
              f"**{t('Not coming', language)}** ({len(not_coming)})\n{_mentions(not_coming, language, chr(10), 1500)}"]
    return [Message(content=text) for text in texts(blocks, "\n\n")]


def signup_messages(op: dict[str, Any], ctx: Context) -> dict[str, Message]:
    """``orbat:0`` ...: the ORBAT, or a Zeus or training operation's list."""
    pages = coming_list(op, ctx) if op["kind"] in COMING else orbat(op, ctx)
    return {f"orbat:{number}": page for number, page in enumerate(pages)}


def messages(op: dict[str, Any], ctx: Context) -> dict[str, Message]:
    """Every message of the channel, keyed in channel order."""
    found = {"info": info(op, ctx)}
    found |= opord(op, ctx)
    preset = ctx.files.get("preset")
    if preset is not None and preset.is_file():
        name = ((op.get("files") or {}).get("preset") or {}).get("preset_name") or preset.name
        found["preset"] = Message(content=t("Modset for the Arma 3 Launcher: {name}", ctx.language, name=plain(name)),
                                  files=[preset])
    found |= signup_messages(op, ctx)
    if ctx.thread_id:
        found["discussion"] = Message(content=f"💬 <#{ctx.thread_id}>")
    return found


def topic(op: dict[str, Any], tz: Any) -> str:
    assembly = when(op, "assembly_at")
    return f"{op['title']} · {op.get('kind_name') or op['kind']} · {assembly.astimezone(tz):%d.%m %H:%M}"


def announcement(op: dict[str, Any], language: str, channel_id: int | None) -> Message:
    """The notice in the announcements channel; its mentions are in the content (sent once, never re-pinged)."""
    assembly = when(op, "assembly_at")
    mark = t("CANCELLED", language) + " · " if op["status"] == "cancelled" else ""
    embed = discord.Embed(title=f"{mark}{op['title']}", colour=COLOURS.get(op["status"], 0x2ECC71),
                          description=f"{op.get('kind_name') or op['kind']}\n{ts(assembly, 'F')} ({ts(assembly, 'R')})")
    if channel_id:
        embed.add_field(name=t("Details and sign-up", language), value=f"<#{channel_id}>")
    return Message(embeds=[embed])
