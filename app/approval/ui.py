"""Approval requests in the moderation thread (docs/PLAN.md, *Approval*).

Each request is a message with the event, what is asked (publishing, the server for a Zeus or training event, or new times),
the creator, the reservation, and persistent *Approve* / *Reject* buttons. *Approve* is disabled while the request's
reservation failed, and *Retry reservation* sits next to it for the moderator who resolved the clash; whenever the
reservation changes the message is drawn again. A request for new times shows the old and the new ones. *Reject*
asks for the reason. Once decided,
the message shows who decided and why, without buttons."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import discord

from app.discord_util import reply, services, thinking
from app.eventchannel.render import duration, kind_label, ts
from app.events.service import EventError
from app.events.words import event_problem
from app.i18n import t
from app.permissions import Perm
from app.signup.service import Member

COLOURS = {"pending": 0xF1C40F, "approved": 0x2ECC71, "rejected": 0xE74C3C, "withdrawn": 0x95A5A6}


async def request_message(s: Any, request: Any) -> tuple[discord.Embed, discord.ui.View | None]:
    language = s.settings.language
    event = await s.events.get(request.event_id)
    asked = {"publish": t("Publish the event", language), "server": t("Reserve the server for a Zeus or training event", language),
             "times": t("Move the event", language)}[request.kind]
    embed = discord.Embed(title=f"{asked}: {event.title}", colour=COLOURS.get(request.state, 0x95A5A6))
    embed.add_field(name=t("Kind", language), value=kind_label(event.kind, language, s.settings.events.kind_names))
    embed.add_field(name=t("Requested by", language), value=f"<@{request.requested_by}>")
    if event.channel_id:
        embed.add_field(name=t("Channel", language), value=f"<#{event.channel_id}>")
    if request.kind == "times":
        p = request.proposed
        embed.add_field(name=t("Now", language), value=_times(event.assembly_at, event.start_at,
                                                             event.duration_minutes, language), inline=False)
        embed.add_field(name=t("New", language), value=_times(datetime.fromisoformat(p["assembly_at"]),
                                                             datetime.fromisoformat(p["start_at"]),
                                                             int(p["duration_minutes"]), language), inline=False)
    else:
        embed.add_field(name=t("Date", language), value=f"{ts(event.assembly_at, 'F')}", inline=False)
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
    if approve.item.disabled and booked is not None:  # the moderator resolved the clash: try again from here
        view.add_item(ApprovalAction(request.id, "retry", t("Retry reservation", language)))
    return embed, view


def _times(assembly: datetime, start: datetime, minutes: int, language: str) -> str:
    return t("{date}, assembly {assembly}, start {start}, {duration}", language, date=ts(assembly, "D"),
             assembly=ts(assembly, "t"), start=ts(start, "t"), duration=duration(minutes, language))


class ApprovalAction(discord.ui.DynamicItem[discord.ui.Button],
                     template=r"approval:(?P<request>\d+):(?P<action>approve|reject|retry)"):
    def __init__(self, request_id: int, action: str, label: str = "") -> None:
        style = {"approve": discord.ButtonStyle.success, "reject": discord.ButtonStyle.danger}.get(
            action, discord.ButtonStyle.secondary)
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
        if self.action == "retry":
            await self._retry(interaction, moderator)
            return
        await thinking(interaction)  # approving publishes: the channel moves, the announcement goes out
        try:
            await services(interaction).events.approve(self.request_id, moderator)
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, interaction.locale, exc.missing))
            return
        await reply(interaction, t("Approved.", interaction.locale))

    async def _retry(self, interaction: discord.Interaction, moderator: Member) -> None:
        """*Retry reservation*; the reservation's listener draws this message again (Approve turns on)."""
        s, locale = services(interaction), interaction.locale
        if not s.events.access(moderator).has(Perm.APPROVE_EVENTS):
            await reply(interaction, t("You don't have the permission for this.", locale))
            return
        try:
            request = await s.events.request(self.request_id)
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, locale, exc.missing))
            return
        await thinking(interaction)
        row = await s.reservations.retry(request.event_id)
        await reply(interaction, t("Server reserved.", locale) if row and row.state == "booked" else
                    t("No reservation: {reason}", locale, reason=row.error if row and row.error else "—"))


class RejectModal(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, request_id: int) -> None:
        super().__init__(title=t("Reject", interaction.locale)[:45], timeout=15 * 60)
        self.request_id = request_id
        self.reason = discord.ui.TextInput(label=t("Reason", interaction.locale), style=discord.TextStyle.paragraph,
                                           max_length=500)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        moderator = Member(interaction.user.id, frozenset(r.id for r in getattr(interaction.user, "roles", ())))
        await thinking(interaction)
        try:
            await services(interaction).events.reject(self.request_id, moderator, self.reason.value)
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, interaction.locale, exc.missing))
            return
        await reply(interaction, t("Rejected.", interaction.locale))
