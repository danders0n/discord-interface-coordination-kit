"""Slash commands around operations (docs/PLAN-front-end.md, *Commands and buttons*):

- ``/operation preset file``, ``/operation image file slot`` and ``/operation mission file | on_server``, used in the
  operation's channel or thread: the attachment is downloaded and uploaded to the supervisor in the member's name,
  which checks who may and reports what it found (a member's files wait for approval before reaching the host);
- ``/operations``: the coming operations and the member's own place in each, privately."""

from __future__ import annotations

import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from app.channel import render
from app.discord_util import reply, thinking
from app.i18n import t
from app.operations.ui import warning_words
from app.services import Services
from app.signup.ui import act

MAX_PRESET = 2 * 1024 * 1024  # the supervisor's limits: refused here before a download
MAX_IMAGE = 8 * 1024 * 1024
MISSIONS_SECONDS = 30  # autocomplete asks at every key: the supervisor's list is read at most this often


class OperationCog(commands.Cog):
    operation = app_commands.Group(name="operation", description="Files for an operation: its preset, images and "
                                                                 "mission", guild_only=True)

    def __init__(self, services: Services) -> None:
        self.services = services
        self._missions: tuple[float, list[dict[str, Any]]] = (0.0, [])

    async def _here(self, interaction: discord.Interaction) -> int | None:
        row = await self.services.channels.by_channel(getattr(interaction, "channel_id", None))
        if row is None:
            await reply(interaction, t("Use this in an operation's channel or its thread.", interaction.locale))
            return None
        return row.operation_id

    async def _upload(self, interaction: discord.Interaction, op_id: int, slot: str,
                      file: discord.Attachment) -> dict[str, Any]:
        await thinking(interaction)  # (downloaded from Discord, then sent on)
        content, client = await file.read(), self.services.supervisor
        return await act(interaction, lambda me: client.upload(me, op_id, slot, file.filename, content))

    @operation.command(name="preset", description="Set the operation's Arma 3 Launcher preset (.html)")
    @app_commands.describe(file="The preset exported from the Arma 3 Launcher")
    async def preset(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        op_id, locale = await self._here(interaction), interaction.locale
        if op_id is None:
            return
        if not file.filename.lower().endswith(".html") or file.size > MAX_PRESET:
            await reply(interaction, t("Send the .html preset exported from the Arma 3 Launcher.", locale))
            return
        op = await self._upload(interaction, op_id, "preset", file)
        preset = (op.get("files") or {}).get("preset") or {}
        lines = [t(("Preset saved: {count} mod.", "Preset saved: {count} mods."), locale,
                   count=len(preset.get("mods") or ()))]
        if preset.get("staged"):
            lines.append(t("It reaches the server once the operation is approved.", locale))
        await reply(interaction, "\n".join(lines))

    @operation.command(name="image", description="Set the OPORD's banner or map image")
    @app_commands.describe(file="A PNG, JPG or WEBP image", slot="Banner (above the OPORD) or map")
    @app_commands.choices(slot=[app_commands.Choice(name=app_commands.locale_str("Banner"), value="banner"),
                                app_commands.Choice(name=app_commands.locale_str("Map"), value="map")])
    async def image(self, interaction: discord.Interaction, file: discord.Attachment, slot: str = "banner") -> None:
        op_id = await self._here(interaction)
        if op_id is None:
            return
        if file.size > MAX_IMAGE:
            await reply(interaction, t("That file is too large.", interaction.locale))
            return
        await self._upload(interaction, op_id, slot if slot in ("banner", "map") else "banner", file)
        await reply(interaction, t("Saved.", interaction.locale))

    @operation.command(name="mission", description="Set the operation's mission: upload a .pbo, or pick one on the "
                                                   "server")
    @app_commands.describe(file="The mission's .pbo (up to Discord's upload limit)",
                           on_server="A mission already on the server (start typing)")
    async def mission(self, interaction: discord.Interaction, file: discord.Attachment | None = None,
                      on_server: str | None = None) -> None:
        op_id, locale = await self._here(interaction), interaction.locale
        if op_id is None:
            return
        if file is None and not on_server:
            await reply(interaction, t("Attach the mission's .pbo, or pick a mission already on the server.", locale))
            return
        if file is not None:
            op = await self._upload(interaction, op_id, "mission", file)
        else:
            await thinking(interaction)
            client, name = self.services.supervisor, on_server
            op = await act(interaction, lambda me: client.pick_mission(me, op_id, name))
        mission = op.get("mission") or {}
        report = mission.get("report") or {}
        lines = [t("Mission set: {name} ({terrain}, {players} playable).", locale, name=mission.get("name") or "?",
                   terrain=report.get("terrain") or "?", players=report.get("playable") or "?")]
        if mission.get("staged"):
            lines.append(t("It reaches the server once the operation is approved.", locale))
        lines += ["⚠ " + warning_words(w, locale) for w in op.get("warnings") or []]
        await reply(interaction, "\n".join(lines)[:1900])

    @mission.autocomplete("on_server")
    async def mission_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        fetched, found = self._missions
        if time.monotonic() - fetched > MISSIONS_SECONDS:
            try:
                client = self.services.supervisor
                found = await act(interaction, lambda me: client.missions(me))
            except Exception:  # noqa: BLE001 - autocomplete stays quiet when the supervisor refuses or is away
                return []
            self._missions = (time.monotonic(), found)
        typed = current.casefold()
        return [app_commands.Choice(name=(f"{m['name']} · {m['terrain']}" if m.get("terrain") else m["name"])[:100],
                                    value=m["name"]) for m in found if typed in m["name"].casefold()][:25]

    @app_commands.command(name="operations", description="The coming operations, and your place in each")
    @app_commands.guild_only()
    async def operations(self, interaction: discord.Interaction) -> None:
        s, locale = self.services, interaction.locale
        await thinking(interaction)
        days, now = s.settings.times.calendar_days, s.scheduler.clock()
        ops = await act(interaction, lambda me: s.supervisor.operations(me, upcoming=True))
        names = {"coming": t("Coming", locale), "maybe": t("Maybe", locale), "cant": t("Can't come", locale),
                 "reserve": t("Reserve list", locale)}
        lines = []
        for op in ops:
            assembly = render.when(op, "assembly_at")
            if op.get("status") != "published" or assembly is None or (assembly - now).days >= days:
                continue
            place = (op.get("my") or {}).get("place") or {}
            mine = t("your slot: {slot}", locale, slot=render.plain(place["slot"])) if place.get("slot") else \
                names.get(place.get("list") or "", "")
            row = await s.channels.row(int(op["id"]))
            link = f" · <#{row.channel_id}>" if row is not None and row.channel_id else ""
            lines.append(f"{render.ts(assembly, 'f')} · {render.plain(op.get('kind_name') or op['kind'])} · "
                         f"**{render.plain(op['title'])}**{link}" + (f" · __{mine}__" if mine else ""))
        embed = discord.Embed(title=t("The next {days} days", locale, days=days),
                              description="\n".join(lines)[:4096] if lines else t("Nothing planned yet.", locale))
        await reply(interaction, embed=embed)
