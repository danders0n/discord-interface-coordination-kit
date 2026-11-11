"""`/server start | stop | restart | say` (docs/PLAN.md, *Supervisor: server control*).

The server is picked by autocomplete from the supervisor's list. Stop and restart ask first, showing how many
players are on. A start or restart a reservation refuses shows the reservation and its end; members with *server
override* get *Start anyway?*, which sends it again with ``override``. Every use goes to the staff log, overrides
marked. The confirmations are short-lived private buttons, so they need not survive a restart."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from app.discord_util import access, finish, reply, require
from app.i18n import t
from app.permissions import Perm
from app.services import Services
from app.supervisor_client import ReservationRefused, SupervisorError

CACHE_SECONDS = 30
CONFIRM_SECONDS = 60


class ServerCog(commands.Cog):
    server = app_commands.Group(name="server", description="Start, stop, restart or message the game servers",
                                default_permissions=discord.Permissions(manage_guild=True), guild_only=True)

    def __init__(self, services: Services) -> None:
        self.services = services
        self._servers: tuple[float, list[dict[str, Any]]] = (0.0, [])

    # ---------------------------------------------------------------- commands

    @server.command(name="start", description="Start a game server")
    @app_commands.describe(server="The game server")
    @require(Perm.SERVER_CONTROL)
    async def start(self, interaction: discord.Interaction, server: str) -> None:
        await self.run(interaction, "start", server)

    @server.command(name="stop", description="Stop a game server")
    @app_commands.describe(server="The game server")
    @require(Perm.SERVER_CONTROL)
    async def stop(self, interaction: discord.Interaction, server: str) -> None:
        await self.confirm(interaction, "stop", server)

    @server.command(name="restart", description="Restart a game server")
    @app_commands.describe(server="The game server")
    @require(Perm.SERVER_CONTROL)
    async def restart(self, interaction: discord.Interaction, server: str) -> None:
        await self.confirm(interaction, "restart", server)

    @server.command(name="say", description="Send a chat message to everyone on a game server")
    @app_commands.describe(server="The game server", message="What the players see")
    @require(Perm.SERVER_MESSAGE)
    async def say(self, interaction: discord.Interaction, server: str,
                  message: app_commands.Range[str, 1, 200]) -> None:
        await self.run(interaction, "say", server, message=message)

    @start.autocomplete("server")
    @stop.autocomplete("server")
    @restart.autocomplete("server")
    @say.autocomplete("server")
    async def server_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            servers = await self.servers()
        except SupervisorError:
            return []
        typed = current.casefold()
        choices = [app_commands.Choice(name=f"{s['server'].get('name') or s['server']['slug']} ({s['server']['slug']})"[:100],
                                       value=s["server"]["slug"]) for s in servers]
        return [c for c in choices if typed in c.name.casefold()][:25]

    async def servers(self) -> list[dict[str, Any]]:
        fetched, servers = self._servers
        if time.monotonic() - fetched > CACHE_SECONDS:
            servers = await self.services.supervisor.servers()
            self._servers = (time.monotonic(), servers)
        return servers

    async def known(self, interaction: discord.Interaction, slug: str) -> bool:
        """Autocomplete only suggests: what was typed must be one of the supervisor's servers before it goes into
        a request's path (anything else is answered here). A server added in the last half minute is found by
        reading the list again."""
        try:
            slugs = {s["server"]["slug"] for s in await self.servers()}
            if slug not in slugs:
                self._servers = (0.0, [])
                slugs = {s["server"]["slug"] for s in await self.servers()}
        except SupervisorError as exc:
            await reply(interaction, t("The supervisor refused: {reason}", interaction.locale, reason=exc.message))
            return False
        if slug not in slugs:
            await reply(interaction, t("Pick a server from the list.", interaction.locale))
        return slug in slugs

    # ---------------------------------------------------------------- doing it

    async def confirm(self, interaction: discord.Interaction, action: str, slug: str) -> None:
        """Stop and restart: say how many players are on, and act on the button."""
        if not await self.known(interaction, slug):
            return
        try:
            status = await self.services.supervisor.server_status(slug)
        except SupervisorError as exc:
            await reply(interaction, t("The supervisor refused: {reason}", interaction.locale, reason=exc.message))
            return
        players = len(status.get("players_online") or [])
        question = (t(("Stop {server}? {count} player is on it.", "Stop {server}? {count} players are on it."),
                      interaction.locale, server=slug, count=players) if action == "stop" else
                    t(("Restart {server}? {count} player is on it.", "Restart {server}? {count} players are on it."),
                      interaction.locale, server=slug, count=players))
        await reply(interaction, question, view=Confirm(self, interaction, action, slug))

    async def run(self, interaction: discord.Interaction, action: str, slug: str, *, override: bool = False,
                  message: str | None = None) -> None:
        client, locale = self.services.supervisor, interaction.locale
        if not await self.known(interaction, slug):
            return
        try:
            if action == "start":
                await client.start(slug, override)
            elif action == "restart":
                await client.restart(slug, override)
            elif action == "stop":
                await client.stop(slug)
            else:
                await client.say(slug, message or "")
        except ReservationRefused as exc:
            await self._refused(interaction, action, slug, exc)
            return
        except SupervisorError as exc:
            await reply(interaction, t("The supervisor refused: {reason}", locale, reason=exc.message))
            return
        done = {"start": t("Starting {server}.", locale, server=slug), "stop": t("Stopping {server}.", locale, server=slug),
                "restart": t("Restarting {server}.", locale, server=slug),
                "say": t("Message sent to {server}.", locale, server=slug)}[action]
        await reply(interaction, done)
        language = self.services.settings.language
        note = (t("{member} used /server {action} on {server}, despite the reservation.", language,
                  member=interaction.user.mention, action=action, server=slug) if override else
                t("{member} used /server {action} on {server}.", language, member=interaction.user.mention,
                  action=action, server=slug))
        if action == "say":
            note += f"\n> {message}"
        self.services.staff.post(note)

    async def _refused(self, interaction: discord.Interaction, action: str, slug: str, exc: ReservationRefused) -> None:
        held = exc.reservation
        until = discord.utils.format_dt(datetime.fromisoformat(held["until"]), "t")
        text = t("Refused: the reservation {name} holds the host until {until}.", interaction.locale,
                 name=held.get("name", "?"), until=until)
        extra: dict[str, Any] = {}  # (discord.py takes no view=None)
        if action in ("start", "restart") and access(interaction).has(Perm.SERVER_OVERRIDE):
            extra["view"] = Override(self, interaction, action, slug)
        await reply(interaction, text, **extra)


class _Owned(discord.ui.View):
    """Private buttons for the member who asked; they stop working after a minute."""

    def __init__(self, cog: ServerCog, asked: discord.Interaction, action: str, slug: str) -> None:
        super().__init__(timeout=CONFIRM_SECONDS)
        self.cog, self.owner, self.action, self.slug = cog, asked.user.id, action, slug

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner


class Confirm(_Owned):
    def __init__(self, cog: ServerCog, asked: discord.Interaction, action: str, slug: str) -> None:
        super().__init__(cog, asked, action, slug)
        label = t("Stop", asked.locale) if action == "stop" else t("Restart", asked.locale)
        self.go.label = label
        self.no.label = t("Cancel", asked.locale)

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def go(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)
        await self.cog.run(interaction, self.action, self.slug)
        self.stop()

    @discord.ui.button(style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await finish(interaction, t("Nothing was done.", interaction.locale))
        self.stop()


class Override(_Owned):
    def __init__(self, cog: ServerCog, asked: discord.Interaction, action: str, slug: str) -> None:
        super().__init__(cog, asked, action, slug)
        self.anyway.label = t("Start anyway?", asked.locale)

    @discord.ui.button(style=discord.ButtonStyle.danger)
    async def anyway(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.edit_message(view=None)
        if not access(interaction).has(Perm.SERVER_OVERRIDE):  # roles may have changed in the minute since
            await reply(interaction, t("You don't have the permission for this.", interaction.locale))
            return
        await self.cog.run(interaction, self.action, self.slug, override=True)
        self.stop()
