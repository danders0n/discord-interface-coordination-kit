"""Slash commands around events (docs/PLAN.md, *Organiser panel*, *ORBAT and slots*, *Calendar*):

- `/event preset file`, `/event image file` and `/event mission file | on_server`, used in the event's channel or thread
  by its organisers (the mission: those with the mission maker role);
- `/slot take slot` (autocomplete: the free slots of this channel's event) and `/slot leave`;
- `/events`: the coming events and the member's own sign-ups, privately."""

from __future__ import annotations

from pathlib import Path

import time

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from app.db.models import Event, Signup, Slot
from app.discord_util import reply, thinking
from app.eventchannel.render import kind_label, ts
from app.events.service import EventError
from app.events.ui import member_of
from app.events.words import event_problem
from app.i18n import t
from app.services import Services
from app.signup import ui as signup_ui

MAX_PRESET = 2 * 1024 * 1024
MAX_IMAGE = 8 * 1024 * 1024
MISSIONS_SECONDS = 30  # autocomplete asks at every key: the supervisor's list is read at most this often


class EventCog(commands.Cog):
    event = app_commands.Group(name="event", description="Files for an event: its preset, image and mission",
                               guild_only=True)
    slot = app_commands.Group(name="slot", description="Take or leave a slot in this channel's event", guild_only=True)

    def __init__(self, services: Services) -> None:
        self.services = services
        self._missions: tuple[float, list] = (0.0, [])

    async def _here(self, interaction: discord.Interaction) -> Event | None:
        event = await self.services.events.by_channel(getattr(interaction.channel, "id", None))
        if event is None:
            await reply(interaction, t("Use this in an event's channel or its thread.", interaction.locale))
        return event

    @event.command(name="preset", description="Set the event's Arma 3 Launcher preset (.html)")
    @app_commands.describe(file="The preset exported from the Arma 3 Launcher")
    async def preset(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        event = await self._here(interaction)
        if event is None:
            return
        if not file.filename.lower().endswith(".html") or file.size > MAX_PRESET:
            await reply(interaction, t("Send the .html preset exported from the Arma 3 Launcher.", interaction.locale))
            return
        await thinking(interaction)  # (the file is downloaded, and the event's messages redrawn)
        try:
            preset = await self.services.events.set_preset(event.id, member_of(interaction), await file.read())
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, interaction.locale, exc.missing))
            return
        except ValueError:
            await reply(interaction, t("That file has no mods and no DLC: is it a launcher preset?", interaction.locale))
            return
        await reply(interaction, t(("Preset saved: {count} mod.", "Preset saved: {count} mods."), interaction.locale,
                                   count=len(preset.mods)))

    @event.command(name="image", description="Set the briefing's banner or map image")
    @app_commands.describe(file="A PNG, JPG, WEBP or GIF image")
    async def image(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        event = await self._here(interaction)
        if event is None:
            return
        if file.size > MAX_IMAGE:
            await reply(interaction, t("That is too long.", interaction.locale))
            return
        await thinking(interaction)  # (the file is downloaded, and the event's messages redrawn)
        try:
            await self.services.events.set_image(event.id, member_of(interaction), await file.read(),
                                                 Path(file.filename).suffix)
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, interaction.locale, exc.missing))
            return
        await reply(interaction, t("Saved.", interaction.locale))

    @event.command(name="mission", description="Set the event's mission: upload a .pbo, or pick one on the server")
    @app_commands.describe(file="The mission's .pbo (up to Discord's upload limit)",
                           on_server="A mission already on the server (start typing)")
    async def mission(self, interaction: discord.Interaction, file: discord.Attachment | None = None,
                      on_server: str | None = None) -> None:
        from app.profiles.service import MAX_MISSION, ProfileError
        from app.profiles.words import problem, warning

        event = await self._here(interaction)
        if event is None:
            return
        locale, profiles = interaction.locale, self.services.profiles
        if file is None and not on_server:
            await reply(interaction, t("Attach the mission's .pbo, or pick a mission already on the server.", locale))
            return
        if file is not None and file.size > MAX_MISSION:
            await reply(interaction, t("That is too long.", locale))
            return
        await interaction.response.defer(ephemeral=True, thinking=True)  # an upload takes a moment
        member = member_of(interaction)
        try:
            if file is not None:
                event = await profiles.upload_mission(event.id, member, file.filename, await file.read())
            else:
                event = await profiles.pick_mission(event.id, member, on_server)
        except ProfileError as exc:
            await reply(interaction, problem(exc.problem, locale, exc.message))
            return
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, locale, exc.missing))
            return
        report = event.mission_report or {}
        lines = [t("Mission set: {name} ({terrain}, {players} playable).", locale, name=event.mission_name,
                   terrain=report.get("terrain") or "?", players=report.get("playable") or "?")]
        lines += [warning(code, detail, locale) for code, detail in await profiles.warnings(event)]
        await reply(interaction, "\n".join(lines)[:1900])

    @mission.autocomplete("on_server")
    async def mission_choices(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not self.services.profiles.may_manage(member_of(interaction)):
            return []  # (the server's missions are for those who set them)
        fetched, found = self._missions
        if time.monotonic() - fetched > MISSIONS_SECONDS:
            try:
                found = await self.services.profiles.missions()
            except Exception:  # noqa: BLE001 - autocomplete stays quiet when the supervisor doesn't answer
                return []
            self._missions = (time.monotonic(), found)
        typed = current.casefold()
        return [app_commands.Choice(name=(f"{m['name']} · {m['title']}" if m.get("title") else m["name"])[:100],
                                    value=m["name"]) for m in found if typed in m["name"].casefold()][:25]

    @slot.command(name="take", description="Take a free slot in this channel's event")
    @app_commands.describe(slot="The slot (start typing its name or squad)")
    async def take(self, interaction: discord.Interaction, slot: str) -> None:
        await thinking(interaction)
        event = await self._here(interaction)
        if event is None:
            return
        if not slot.isdigit():
            await reply(interaction, t("Pick a slot from the list.", interaction.locale))
            return
        outcome = await self.services.signups.take(event.id, int(slot), member_of(interaction))
        await signup_ui.answer(interaction, outcome, t("You have the slot. See you there!", interaction.locale),
                               ask_link=True)

    @take.autocomplete("slot")
    async def free_slots(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        event = await self.services.events.by_channel(getattr(interaction.channel, "id", None))
        if event is None:
            return []
        board = await self.services.signups.board(event.id)
        typed = current.casefold()
        choices = [app_commands.Choice(name=f"{squad.name} · {slot.name}"[:100], value=str(slot.id))
                   for squad, slots in board.squads for slot in slots
                   if slot.holder_id is None and not slot.locked and slot.id not in board.offered]
        return [c for c in choices if typed in c.name.casefold()][:25]

    @slot.command(name="leave", description="Leave your slot (or the lists) in this channel's event")
    async def leave(self, interaction: discord.Interaction) -> None:
        await thinking(interaction)
        event = await self._here(interaction)
        if event is None:
            return
        outcome = await self.services.signups.leave(event.id, member_of(interaction))
        await signup_ui.answer(interaction, outcome, t("You are off this event.", interaction.locale))

    @app_commands.command(name="events", description="The coming events, and the ones you signed up for")
    @app_commands.guild_only()
    async def events(self, interaction: discord.Interaction) -> None:
        locale, days = interaction.locale, self.services.settings.times.calendar_days
        coming = await self.services.events.upcoming(days)
        async with self.services.db.session() as session:
            mine_slots = set((await session.execute(select(Slot.event_id).where(
                Slot.holder_id == interaction.user.id))).scalars())
            mine_lists = {row.event_id: row.status for row in (await session.execute(select(Signup).where(
                Signup.member_id == interaction.user.id))).scalars()}
        names = {"coming": t("Coming", locale), "maybe": t("Maybe", locale), "cant": t("Can't come", locale),
                 "reserve": t("Reserve list", locale)}
        lines = []
        for event in coming:
            mine = t("your slot", locale) if event.id in mine_slots else names.get(mine_lists.get(event.id, ""), "")
            link = f" · <#{event.channel_id}>" if event.channel_id else ""
            lines.append(f"{ts(event.assembly_at, 'f')} · {kind_label(event.kind, locale, self.services.settings.events.kind_names)} · **{event.title}**"
                         f"{link}" + (f" · __{mine}__" if mine else ""))
        embed = discord.Embed(title=t("The next {days} days", locale, days=days),
                              description="\n".join(lines)[:4096] if lines else t("Nothing planned yet.", locale))
        await reply(interaction, embed=embed)
