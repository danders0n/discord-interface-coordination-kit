"""`/archive event` (docs/PLAN.md, *Long-term archive*, *Reading it*): an event moved into the database, privately
as an HTML file. Images are inlined when the file fits the server's upload limit; otherwise it is a ZIP with the
HTML and the attachments; if even that is too big, the HTML lists the attachments by name (they stay on the host).
It is open to the event's participants and organisers, and to *manage any event* and *bot admin* roles;
autocomplete offers only those events."""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from app.archive.html import render
from app.discord_util import access, reply
from app.i18n import t
from app.services import Services

DEFAULT_LIMIT = 10 * 1024 * 1024  # Discord's upload limit without boosts


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:50] or "event"


def _zip(html: str, messages: list) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html)
        for message in messages:
            for item in message.attachments or ():
                path = Path(item["path"])
                if path.is_file():
                    archive.write(path, f"attachments/{path.name}")
    return out.getvalue()


class ArchiveCog(commands.Cog):
    archive = app_commands.Group(name="archive", description="Events moved into the database archive",
                                 guild_only=True)

    def __init__(self, services: Services) -> None:
        self.services = services

    def _names(self, guild: discord.Guild | None, ids: set[int]) -> dict[int, str]:
        found = {}
        for member_id in ids:
            member = guild.get_member(member_id) if guild is not None and member_id > 0 else None
            if member is not None:
                found[member_id] = member.display_name
        return found

    @archive.command(name="event", description="An archived event as an HTML file: info, ORBAT, attendance, discussion")
    @app_commands.describe(event="The event (start typing its title or date)")
    async def event(self, interaction: discord.Interaction, event: str) -> None:
        s, locale = self.services, interaction.locale
        found = await s.archive.may_read(int(event), interaction.user.id, access(interaction)) if event.isdigit() else None
        if found is None:
            await reply(interaction, t("No archived event of yours by that name: pick one from the list.", locale))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        board = await s.signups.board(found.id)
        entries = await s.attendance.entries(found.id) if s.attendance is not None else []
        messages = await s.archive.messages(found.id)
        ids = {slot.holder_id for slot in board.slots if slot.holder_id} | {x.member_id for x in board.signups}
        ids |= {e.member_id for e in entries if e.member_id} | {found.author_id or 0}
        names = self._names(interaction.guild, ids)
        language, tz = locale, s.settings.tz
        limit = getattr(interaction.guild, "filesize_limit", DEFAULT_LIMIT)
        slug = _slug(found.title)

        def build() -> discord.File:
            html = render(found, board, entries, messages, names, "inline", language, tz, s.settings.events.kind_names).encode("utf-8")
            if len(html) <= limit:
                return discord.File(io.BytesIO(html), f"{slug}.html")
            packed = _zip(render(found, board, entries, messages, names, "zip", language, tz, s.settings.events.kind_names), messages)
            if len(packed) <= limit:
                return discord.File(io.BytesIO(packed), f"{slug}.zip")
            listed = render(found, board, entries, messages, names, "listed", language, tz, s.settings.events.kind_names).encode("utf-8")
            return discord.File(io.BytesIO(listed), f"{slug}.html")

        await reply(interaction, t("{title}, from the archive.", locale, title=found.title),
                    file=await asyncio.to_thread(build))

    @event.autocomplete("event")
    async def event_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        tz = self.services.settings.tz
        found = await self.services.archive.readable(interaction.user.id, access(interaction), current)
        return [app_commands.Choice(name=f"{e.start_at.astimezone(tz):%d.%m.%Y} · {e.title}"[:100], value=str(e.id))
                for e in found]
