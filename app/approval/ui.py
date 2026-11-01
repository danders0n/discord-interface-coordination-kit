"""Approval requests in the moderation thread (docs/PLAN.md, *Approval*).

Each request is a message with the event, what is asked (publishing, the server for a Zeus event, or new times),
the creator, the reservation, and persistent *Approve* / *Reject* buttons. *Approve* is disabled while the request's
reservation failed; after *Retry reservation* the message is drawn again. *Reject* asks for the reason. Once decided,
the message shows who decided and why, without buttons."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import discord

from app.discord_util import reply, services
from app.eventchannel.render import kind_label, ts
from app.events.service import EventError
from app.events.words import event_problem
from app.i18n import t
from app.signup.service import Member

COLOURS = {"pending": 0xF1C40F, "approved": 0x2ECC71, "rejected": 0xE74C3C, "withdrawn": 0x95A5A6}


async def request_message(s: Any, request: Any) -> tuple[discord.Embed, discord.ui.View | None]:
    language = s.settings.language
    event = await s.events.get(request.event_id)
    asked = {"publish": t("Publish the event", language), "server": t("Reserve the server for a Zeus event", language),
             "times": t("Move the event", language)}[request.kind]
    embed = discord.Embed(title=f"{asked}: {event.title}", colour=COLOURS.get(request.state, 0x95A5A6))
    embed.add_field(name=t("Kind", language), value=kind_label(event.kind, language))
    embed.add_field(name=t("Requested by", language), value=f"<@{request.requested_by}>")
    if event.channel_id:
        embed.add_field(name=t("Channel", language), value=f"<#{event.channel_id}>")
    embed.add_field(name=t("Date", language), value=f"{ts(event.assembly_at, 'F')}", inline=False)
    if request.kind == "times":
        new = datetime.fromisoformat(request.proposed["assembly_at"])
        embed.add_field(name=t("New date", language), value=ts(new, "F"), inline=False)
    booked = await s.reservations.get(event.id)
    if request.kind != "times" and event.kind != "guest":
        state = t("Server reserved.", language) if booked and booked.state == "booked" else \
            t("No reservation: {reason}", language, reason=booked.error if booked and booked.error else "—")
        embed.add_field(name=t("Server", language), value=state, inline=False)
    if request.state != "pending":
        decided = {"approved": t("Approved by {member}", language, member=f"<@{request.decided_by}>"),
                   "rejected": t("Rejected by {member}: {reason}", language, member=f"<@{request.decided_by}>",
                                 reason=request.reason or "—"),
                   "withdrawn": t("Withdrawn", language)}[request.state]
        embed.add_field(name=t("Decision", language), value=decided, inline=False)
        return embed, None
    view = discord.ui.View(timeout=None)
    approve = ApprovalAction(request.id, "approve", t("Approve", language))
    approve.item.disabled = await s.events.approval_blocked(request)
    view.add_item(approve)
    view.add_item(ApprovalAction(request.id, "reject", t("Reject", language)))
    return embed, view


class ApprovalAction(discord.ui.DynamicItem[discord.ui.Button], template=r"approval:(?P<request>\d+):(?P<action>approve|reject)"):
    def __init__(self, request_id: int, action: str, label: str = "") -> None:
        style = discord.ButtonStyle.success if action == "approve" else discord.ButtonStyle.danger
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"approval:{request_id}:{action}"))
        self.request_id, self.action = request_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> ApprovalAction:
        return cls(int(match["request"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        moderator = Member(interaction.user.id, frozenset(r.id for r in getattr(interaction.user, "roles", ())))
        if self.action == "reject":
            await interaction.response.send_modal(RejectModal(interaction, self.request_id))
            return
        try:
            await services(interaction).events.approve(self.request_id, moderator)
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, interaction.locale, exc.missing))
            return
        await reply(interaction, t("Approved.", interaction.locale))


class RejectModal(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, request_id: int) -> None:
        super().__init__(title=t("Reject", interaction.locale)[:45], timeout=15 * 60)
        self.request_id = request_id
        self.reason = discord.ui.TextInput(label=t("Reason", interaction.locale), style=discord.TextStyle.paragraph,
                                           max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        moderator = Member(interaction.user.id, frozenset(r.id for r in getattr(interaction.user, "roles", ())))
        try:
            await services(interaction).events.reject(self.request_id, moderator, self.reason.value)
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, interaction.locale, exc.missing))
            return
        await reply(interaction, t("Rejected.", interaction.locale))
