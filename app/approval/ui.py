"""Approval requests in the moderation thread (docs/PLAN-front-end.md, *Notices*; how they look: docs/PLAN.md,
*Approval*).

Each request is a message with the operation, what is asked (publishing, holding the host, new times), who asked, and
persistent *Approve* / *Reject* buttons, which call the supervisor in the moderator's name (act-as); it decides who may
(``operations.approve``, never one's own request). *Approve* is off while the supervisor says the request is blocked (a
clash), with the reason shown. A request for new times shows the old and the new ones. *Reject* asks for the reason.
Once decided, withdrawn or replaced, the message shows that, without buttons."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import discord

from app.channel import render
from app.discord_util import reply, services, thinking
from app.i18n import t
from app.signup.ui import act

COLOURS = {"pending": 0xF1C40F, "approved": 0x2ECC71, "rejected": 0xE74C3C, "withdrawn": 0x95A5A6}


def asked(kind: str, language: Any) -> str:
    return {"publish": t("Publish the operation", language), "hold": t("Hold the host", language),
            "times": t("Move the operation", language)}.get(kind) or t("Request", language)


def _times(assembly: datetime, start: datetime, minutes: int, language: Any) -> str:
    return t("{date}, assembly {assembly}, start {start}, {duration}", language, date=render.ts(assembly, "D"),
             assembly=render.ts(assembly, "t"), start=render.ts(start, "t"), duration=render.duration(minutes, language))


def request_message(request: dict[str, Any], language: Any, channel_id: int | None = None,
                    kind_name: str | None = None, state: str | None = None) -> tuple[discord.Embed, discord.ui.View | None]:
    """The embed and, while pending, the buttons. ``state`` overrides the request's (``withdrawn`` for one that a
    newer request replaced or whose operation was deleted)."""
    op = request.get("operation") or {}
    state = state or request.get("state") or "pending"
    title = render.plain(op.get("title") or request.get("title") or "?")
    embed = discord.Embed(title=f"{asked(request.get('kind', ''), language)}: {title}"[:256],
                          colour=COLOURS.get(state, 0x95A5A6))
    embed.add_field(name=t("Kind", language), value=kind_name or op.get("kind") or "—")
    embed.add_field(name=t("Requested by", language), value=render.plain(request.get("requested_by") or "?"))
    if channel_id:
        embed.add_field(name=t("Channel", language), value=f"<#{channel_id}>")
    assembly, start = render.when(op, "assembly_at"), render.when(op, "start_at")
    if request.get("kind") == "times" and assembly and start:
        proposed = request.get("proposed") or {}
        embed.add_field(name=t("Now", language), value=_times(assembly, start, int(op.get("duration_minutes") or 0),
                                                             language), inline=False)
        if proposed.get("assembly_at") and proposed.get("start_at"):
            embed.add_field(name=t("New", language), value=_times(
                datetime.fromisoformat(proposed["assembly_at"]), datetime.fromisoformat(proposed["start_at"]),
                int(proposed.get("duration_minutes") or 0), language), inline=False)
    elif assembly:
        embed.add_field(name=t("Date", language), value=render.ts(assembly, "F"), inline=False)
    if state == "pending":
        if request.get("blocked"):
            embed.add_field(name=t("Blocked", language), value=str(request["blocked"])[:1024], inline=False)
        view = discord.ui.View(timeout=None)
        approve = ApprovalAction(int(request["id"]), "approve", t("Approve", language))
        approve.item.disabled = bool(request.get("blocked"))
        view.add_item(approve)
        view.add_item(ApprovalAction(int(request["id"]), "reject", t("Reject", language)))
        return embed, view
    who = render.plain(request.get("decided_by") or "?")
    decided = {"approved": t("Approved by {member}", language, member=who),
               "rejected": t("Rejected by {member}: {reason}", language, member=who,
                             reason=render.plain(request.get("reason") or "—")),
               "withdrawn": t("Withdrawn", language)}.get(state, state)
    embed.add_field(name=t("Decision", language), value=decided, inline=False)
    return embed, None


class ApprovalAction(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"approval:(?P<request>\d+):(?P<action>approve|reject)"):
    def __init__(self, request_id: int, action: str, label: str = "") -> None:
        style = {"approve": discord.ButtonStyle.success, "reject": discord.ButtonStyle.danger}[action]
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"approval:{request_id}:{action}"))
        self.request_id, self.action = request_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> ApprovalAction:
        return cls(int(match["request"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.action == "reject":
            await interaction.response.send_modal(RejectModal(interaction, self.request_id))
            return
        await thinking(interaction)  # approving publishes: the channel turns public, the announcement goes out
        client = services(interaction).supervisor
        await act(interaction, lambda me: client.approve_request(me, self.request_id))
        await reply(interaction, t("Approved.", interaction.locale))


class RejectModal(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, request_id: int) -> None:
        super().__init__(title=t("Reject", interaction.locale)[:45], timeout=15 * 60)
        self.request_id = request_id
        self.reason = discord.ui.TextInput(label=t("Reason", interaction.locale), style=discord.TextStyle.paragraph,
                                           max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await thinking(interaction)
        client, reason = services(interaction).supervisor, self.reason.value
        await act(interaction, lambda me: client.reject_request(me, self.request_id, reason))
        await reply(interaction, t("Rejected.", interaction.locale))
