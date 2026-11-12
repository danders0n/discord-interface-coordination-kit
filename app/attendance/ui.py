"""The attendance report's buttons (docs/PLAN.md, *Supervisor: attendance*, *Corrections*).

*Edit attendance* (organisers only; it carries the event id, so it works after a restart) opens a private editor:
pick a status (attended, no-show, excused), then the members (a member picker, so the 25-option limit of a list
doesn't matter, and someone missing from the report can be added). When the report offers *probably <member>*
matches, *Confirm links* turns the chosen ones into Steam links and counts their time.

*Rate the mission* (or in a guest event *Rate the cooperation*) opens a modal with the stars and a comment, filled
with the member's rating so far; it is on the report while the rating window is open."""

from __future__ import annotations

from typing import Any

import discord

from app.discord_util import reply, services
from app.events.ui import guarded, member_of
from app.i18n import t
from app.feedback.service import MAX_COMMENT, FeedbackError, Problem as FeedbackProblem

TIMEOUT = 15 * 60


def report_buttons(event_id: int, language: str, rate: str | None = None) -> discord.ui.View:
    """``rate``: the rating button's label, while ratings are open."""
    view = discord.ui.View(timeout=None)
    if rate:
        view.add_item(ReportAction(event_id, "rate", rate, discord.ButtonStyle.primary, "⭐"))
    view.add_item(ReportAction(event_id, "edit", t("Edit attendance", language)))
    return view


def feedback_problem(code: FeedbackProblem, locale: Any) -> str:
    return {
        FeedbackProblem.NOT_OPEN: t("Ratings open with the attendance report.", locale),
        FeedbackProblem.CLOSED: t("Ratings for this event have closed.", locale),
        FeedbackProblem.NOT_ATTENDED: t("Only those who attended rate the mission.", locale),
        FeedbackProblem.BAD_STARS: t("Give 1 to 5 stars.", locale),
        FeedbackProblem.TOO_LONG: t("That is too long.", locale),
    }[code]


def status_names(locale: Any) -> dict[str, str]:
    return {"attended": t("Attended", locale), "no_show": t("No-show", locale), "excused": t("Excused", locale)}


class ReportAction(discord.ui.DynamicItem[discord.ui.Button], template=r"attendance:(?P<event>\d+):(?P<action>[a-z]+)"):
    def __init__(self, event_id: int, action: str, label: str = "",
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary, emoji: str = "📋") -> None:
        super().__init__(discord.ui.Button(label=label or action, style=style, emoji=emoji,
                                           custom_id=f"attendance:{event_id}:{action}"))
        self.event_id, self.action = event_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> ReportAction:
        return cls(int(match["event"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        s, locale = services(interaction), interaction.locale
        event = await guarded(interaction, lambda: s.events.get(self.event_id), slow=False)  # (a modal may follow)
        if event is None:
            return
        if self.action == "rate":
            problem = await s.feedback.may_rate(event, interaction.user.id)
            if problem is not None:
                await reply(interaction, feedback_problem(problem, locale))
                return
            await interaction.response.send_modal(RateModal(interaction, event, await s.feedback.mine(
                event.id, interaction.user.id)))
            return
        if not await s.events.can_organise(event, member_of(interaction)):
            await reply(interaction, t("Only the event's organisers edit the attendance.", locale))
            return
        await reply(interaction, t("Pick the status, then the members.", locale), view=Editor(interaction, self.event_id))


class RateModal(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, event: Any, current: Any = None) -> None:
        locale = interaction.locale
        title = t("Rate the cooperation", locale) if event.kind == "guest" else t("Rate the mission", locale)
        super().__init__(title=title[:45], timeout=TIMEOUT)
        self.event = event
        self.stars = discord.ui.TextInput(label=t("Stars (1 to 5)", locale), max_length=2,
                                          default=str(current.stars) if current else None)
        self.comment = discord.ui.TextInput(label=t("Comment (optional)", locale), style=discord.TextStyle.paragraph,
                                            required=False, max_length=MAX_COMMENT,
                                            default=current.comment if current and current.comment else None)
        self.add_item(self.stars)
        self.add_item(self.comment)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s, locale = services(interaction), interaction.locale
        try:
            await s.feedback.rate(self.event, interaction.user.id, self.stars.value, self.comment.value)
        except FeedbackError as exc:
            await reply(interaction, feedback_problem(exc.problem, locale))
            return
        if self.event.kind != "guest":  # the report shows the average (a guest event's ratings are for staff)
            s.channels.redrawer.request(self.event.id)
        await reply(interaction, t("Thank you: your rating is saved. You can change it until the ratings close.", locale))


class Editor(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, event_id: int) -> None:
        super().__init__(timeout=TIMEOUT)
        self.event_id, self.status, locale = event_id, None, interaction.locale
        pick = discord.ui.Select(placeholder=t("Status", locale), options=[
            discord.SelectOption(label=name, value=value) for value, name in status_names(locale).items()])
        pick.callback = lambda i, select=pick: self._status(i, select.values[0])
        self.add_item(pick)
        who = discord.ui.UserSelect(placeholder=t("Members", locale), max_values=25)
        who.callback = lambda i, select=who: self._members(i, [u.id for u in select.values])
        self.add_item(who)

    async def _status(self, interaction: discord.Interaction, status: str) -> None:
        self.status = status
        await interaction.response.edit_message(view=self)

    async def _members(self, interaction: discord.Interaction, member_ids: list[int]) -> None:
        locale = interaction.locale
        if self.status is None:
            await reply(interaction, t("Pick the status first.", locale))
            return
        s = services(interaction)
        changed = await guarded(interaction, lambda: s.attendance.correct(self.event_id, member_of(interaction),
                                                                          member_ids, self.status))
        if changed is not None:
            await reply(interaction, t(("{count} member is now {status}.", "{count} members are now {status}."), locale,
                                       count=changed, status=status_names(locale)[self.status]))
