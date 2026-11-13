"""`/archive event` (docs/PLAN.md, *Long-term archive*, *Reading it*): an operation moved into the database,
privately as an HTML file. Images are inlined when the file fits the server's upload limit; otherwise it is a ZIP
with the HTML and the attachments; if even that is too big, the HTML lists the attachments by name (they stay on the
host). It is open to the operation's organisers and to *bot admin* roles; autocomplete offers only those."""

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
    return re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")[:50] or "operation"


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
    archive = app_commands.Group(name="archive", description="Operations moved into the database archive",
                                 guild_only=True)

    def __init__(self, services: Services) -> None:
        self.services = services

    @archive.command(name="event", description="An archived operation as an HTML file: info, OPORD, discussion")
    @app_commands.describe(event="The operation (start typing its title or date)")
    async def event(self, interaction: discord.Interaction, event: str) -> None:
        s, locale = self.services, interaction.locale
        found = await s.archive.may_read(int(event), interaction.user.id, access(interaction)) if event.isdigit() else None
        if found is None:
            await reply(interaction, t("No archived operation of yours by that name: pick one from the list.", locale))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        messages = await s.archive.messages(found.operation_id)
        op, language, tz = found.operation, locale, s.settings.tz
        limit = getattr(interaction.guild, "filesize_limit", DEFAULT_LIMIT)
        slug = _slug(found.title)

        def build() -> discord.File:
            html = render(op, messages, "inline", language, tz).encode("utf-8")
            if len(html) <= limit:
                return discord.File(io.BytesIO(html), f"{slug}.html")
            packed = _zip(render(op, messages, "zip", language, tz), messages)
            if len(packed) <= limit:
                return discord.File(io.BytesIO(packed), f"{slug}.zip")
            listed = render(op, messages, "listed", language, tz).encode("utf-8")
            return discord.File(io.BytesIO(listed), f"{slug}.html")

        await reply(interaction, t("{title}, from the archive.", locale, title=found.title),
                    file=await asyncio.to_thread(build))

    @event.autocomplete("event")
    async def event_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        tz = self.services.settings.tz
        found = await self.services.archive.readable(interaction.user.id, access(interaction), current)
        return [app_commands.Choice(name=f"{r.start_at.astimezone(tz):%d.%m.%Y} · {r.title}"[:100] if r.start_at
                                    else r.title[:100], value=str(r.operation_id)) for r in found]
