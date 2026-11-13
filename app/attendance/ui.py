"""The attendance report's buttons (docs/PLAN-front-end.md, *Commands and buttons*; how they look: docs/PLAN.md,
*Supervisor: attendance*, *Corrections*, *Mission feedback*).

*Rate the mission* (a guest operation: *Rate the cooperation*) is on the report while the supervisor's rating window
is open; *Edit attendance* is always there. Both carry the operation id (they work after a restart), read the
operation in the member's name and go only as far as its ``my.actions`` allow (``rate``, ``correct``): the
supervisor decides who attended, who may rate and who corrects. The editor picks a status, then the members (a
member picker, so the 25-option limit of a list doesn't matter, and someone missing from the report can be added)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import discord

from app.channel import render
from app.discord_util import reply, services, thinking
from app.i18n import t
from app.operations.ui import TIMEOUT, person_ids, read
from app.signup.ui import act

CORRECTABLE = ("attended", "no_show", "excused")


def report_buttons(op: dict[str, Any], language: Any, now: datetime) -> discord.ui.View:
    """*Rate* while the rating window is open (from its end and the clock: the operation as last read may be older
    than its closing), *Edit attendance* always."""
    view = discord.ui.View(timeout=None)
    closes = render.when(op.get("rating") or {}, "closes_at")
    if closes is not None and now < closes:
        label = t("Rate the cooperation", language) if op["kind"] == "guest" else t("Rate the mission", language)
        view.add_item(ReportAction(int(op["id"]), "rate", label, discord.ButtonStyle.primary, "⭐"))
    view.add_item(ReportAction(int(op["id"]), "edit", t("Edit attendance", language)))
    return view


def why_not_rate(op: dict[str, Any], locale: Any) -> str:
    rating = op.get("rating") or {}
    if not rating.get("closes_at"):
        return t("Ratings open with the attendance report.", locale)
    if not rating.get("open"):
        return t("Ratings for this operation have closed.", locale)
    return t("Only those who attended rate the mission.", locale)


class ReportAction(discord.ui.DynamicItem[discord.ui.Button], template=r"attendance:(?P<op>\d+):(?P<action>[a-z]+)"):
    def __init__(self, op_id: int, action: str, label: str = "",
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, emoji: str = "📋") -> None:
        super().__init__(discord.ui.Button(label=label or action, style=style, emoji=emoji,
                                           custom_id=f"attendance:{op_id}:{action}"))
        self.op_id, self.action = op_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> ReportAction:
        return cls(int(match["op"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        locale = interaction.locale
        op = await read(interaction, self.op_id)  # (a modal may follow: no "thinking" first)
        actions = (op.get("my") or {}).get("actions") or []
        if self.action == "rate":
            if "rate" not in actions:
                await reply(interaction, why_not_rate(op, locale))
                return
            await interaction.response.send_modal(RateModal(interaction, op))
            return
        if "correct" not in actions:
            await reply(interaction, t("Only the operation's organisers edit the attendance.", locale))
            return
        await reply(interaction, t("Pick the status, then the members.", locale), view=Editor(interaction, self.op_id))


class RateModal(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        locale = interaction.locale
        title = t("Rate the cooperation", locale) if op["kind"] == "guest" else t("Rate the mission", locale)
        super().__init__(title=title[:45], timeout=TIMEOUT)
        self.op_id = int(op["id"])
        current = (op.get("my") or {}).get("rating") or {}
        self.stars = discord.ui.TextInput(label=t("Stars (1 to 5)", locale), max_length=1,
                                          default=str(current["stars"]) if current.get("stars") else None)
        self.comment = discord.ui.TextInput(label=t("Comment (optional)", locale), style=discord.TextStyle.paragraph,
                                            required=False, max_length=1000, default=current.get("comment") or None)
        self.add_item(self.stars)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        locale, typed = interaction.locale, self.stars.value.strip()
        if not typed.isdigit() or not 1 <= int(typed) <= 5:
            await reply(interaction, t("Give 1 to 5 stars.", locale))
            return
        await thinking(interaction)
        client, comment = services(interaction).supervisor, self.comment.value.strip()
        await act(interaction, lambda me: client.rate(me, self.op_id, int(typed), comment))
        await reply(interaction, t("Thank you: your rating is saved. You can change it until the ratings close.",
                                   locale))


class Editor(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, op_id: int) -> None:
        super().__init__(timeout=TIMEOUT)
        self.op_id, self.status, locale = op_id, None, interaction.locale
        pick = discord.ui.Select(placeholder=t("Status", locale), options=[
            discord.SelectOption(label=render.status_name(value, locale), value=value) for value in CORRECTABLE])
        pick.callback = lambda i, select=pick: self._status(i, select.values[0])
        self.add_item(pick)
        who = discord.ui.UserSelect(placeholder=t("Members", locale), max_values=25)
        who.callback = lambda i, select=who: self._members(i, list(select.values))
        self.add_item(who)

    async def _status(self, interaction: discord.Interaction, status: str) -> None:
        self.status = status
        await interaction.response.edit_message(view=self)

    async def _members(self, interaction: discord.Interaction, members: list[Any]) -> None:
        locale = interaction.locale
        if self.status is None:
            await reply(interaction, t("Pick the status first.", locale))
            return
        await thinking(interaction)
        persons = await person_ids(interaction, members)
        if persons is None:
            return
        client, status = services(interaction).supervisor, self.status
        await act(interaction, lambda me: client.correct_attendance(me, self.op_id, persons, status))
        await reply(interaction, t(("{count} member is now {status}.", "{count} members are now {status}."), locale,
                                   count=len(persons), status=render.status_name(status, locale)))
