"""``/slot take slot`` (autocomplete: the free slots of this channel's operation) and ``/slot leave``, typed in an
operation's channel or its thread (docs/PLAN-front-end.md, *Commands and buttons*). Both act for the member
(act-as); the supervisor decides, and a refusal is answered in words."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from app.discord_util import reply, thinking
from app.i18n import t
from app.services import Services
from app.signup import ui


class SlotCog(commands.Cog):
    slot = app_commands.Group(name="slot", description="Take or leave a slot in this channel's operation",
                              guild_only=True)

    def __init__(self, services: Services) -> None:
        self.services = services

    async def _operation(self, interaction: discord.Interaction) -> int | None:
        row = await self.services.channels.by_channel(interaction.channel_id)
        if row is None:
            await reply(interaction, t("Use this in an operation's channel or its thread.", interaction.locale))
            return None
        return row.operation_id

    @slot.command(name="take", description="Take a free slot in this channel's operation")
    @app_commands.describe(slot="The slot (start typing its name or squad)")
    async def take(self, interaction: discord.Interaction, slot: str) -> None:
        op_id = await self._operation(interaction)
        if op_id is None:
            return
        if not slot.isdigit():
            await reply(interaction, t("Pick a slot from the list.", interaction.locale))
            return
        await thinking(interaction)
        client = self.services.supervisor
        body = await ui.act(interaction, lambda me: client.take_slot(me, op_id, int(slot)))
        await ui.answer(interaction, body, t("You have the slot. See you there!", interaction.locale), ask_link=True)

    @take.autocomplete("slot")
    async def free_slots(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """The free slots this member's roles allow, from the operation as last drawn (the supervisor checks the
        pick again)."""
        row = await self.services.channels.by_channel(interaction.channel_id)
        if row is None:
            return []
        typed = current.casefold()
        choices = [app_commands.Choice(name=f"{squad['name']} · {number}. {slot['name']}"[:100], value=str(slot["id"]))
                   for squad, slots in ui.free(row.operation or {}, ui.role_ids(interaction)) for number, slot in slots]
        return [c for c in choices if typed in c.name.casefold()][:25]

    @slot.command(name="leave", description="Leave your slot (or the lists) in this channel's operation")
    async def leave(self, interaction: discord.Interaction) -> None:
        op_id = await self._operation(interaction)
        if op_id is None:
            return
        await thinking(interaction)
        await ui.press(interaction, op_id, "leave")
