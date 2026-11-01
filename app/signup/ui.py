"""Sign-up on Discord (docs/PLAN.md, *ORBAT and slots*, *Picking*, *Zeus sign-up*).

The buttons under the ORBAT (or a Zeus event's list) and on an offer carry their ids in ``custom_id``, so they keep
working after a restart. *Sign up* opens a private two-step pick, squad then free slot, because a select takes 25
options. Answers are the services' refusals in the member's words, and a missing Creator DLC is a warning, never a
refusal."""

from __future__ import annotations

from typing import Any

import discord

from app.discord_util import reply, services
from app.events.words import refusal
from app.i18n import t
from app.links.ui import link_view
from app.presets import CDLCS
from app.signup.service import Member, Outcome, Refusal

TIMEOUT = 15 * 60
ORBAT_ACTIONS = ("take", "reserve", "maybe", "cant", "leave")
ZEUS_ACTIONS = ("coming", "notcoming", "leave")


def member_of(interaction: discord.Interaction) -> Member:
    return Member(interaction.user.id, frozenset(role.id for role in getattr(interaction.user, "roles", ())))


async def answer(interaction: discord.Interaction, outcome: Outcome, done: str, ask_link: bool = False) -> None:
    """``done`` on success (with the DLC warning), the refusal otherwise. ``ask_link``: a slot or *Coming*, so a
    member's first one without a Steam link asks for it (docs/PLAN.md, *Supervisor: attendance*, *Identity*)."""
    locale = interaction.locale
    if not outcome.ok:
        other = None
        if outcome.refusal == Refusal.OVERLAP and outcome.other_event:
            other = (await services(interaction).events.get(outcome.other_event)).channel_id
        await reply(interaction, refusal(outcome.refusal, locale, other))
        return
    text = done
    if outcome.missing_dlc:
        names = ", ".join(CDLCS[k].name for k in outcome.missing_dlc if k in CDLCS)
        text += "\n" + t("⚠ This mission needs Creator DLC you have no role for: {dlc}.", locale, dlc=names)
    links = services(interaction).links
    if ask_link and links is not None and await links.ask_at_signup(interaction.user.id):
        text += "\n" + t("Link your Steam account so attendance counts you: press Link now, or use /link.", locale)
        await reply(interaction, text, view=link_view(locale))
        return
    await reply(interaction, text)


def buttons(event_id: int, zeus: bool, language: str, *, closed: bool = False, not_open: bool = False,
            full: bool = False) -> discord.ui.View:
    """``closed``: the event has started, nothing works. ``not_open``: before sign-up opens only Maybe, Can't come
    (Not coming) and Leave work. ``full``: a Zeus event's limit is reached, Coming is off until someone leaves."""
    labels = {"take": t("Sign up", language), "reserve": t("Reserve", language), "maybe": t("Maybe", language),
              "cant": t("Can't come", language), "leave": t("Leave", language), "coming": t("Coming", language),
              "notcoming": t("Not coming", language)}
    styles = {"take": discord.ButtonStyle.success, "coming": discord.ButtonStyle.success,
              "leave": discord.ButtonStyle.danger}
    view = discord.ui.View(timeout=None)
    for action in ZEUS_ACTIONS if zeus else ORBAT_ACTIONS:
        button = SignupAction(event_id, action, labels[action], styles.get(action, discord.ButtonStyle.secondary))
        button.item.disabled = closed or (not_open and action in ("take", "reserve", "coming")) or \
            (full and action == "coming")
        view.add_item(button)
    return view


class SignupAction(discord.ui.DynamicItem[discord.ui.Button], template=r"signup:(?P<event>\d+):(?P<action>[a-z]+)"):
    def __init__(self, event_id: int, action: str, label: str = "",
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"signup:{event_id}:{action}"))
        self.event_id, self.action = event_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> SignupAction:
        return cls(int(match["event"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        signups, member, locale = services(interaction).signups, member_of(interaction), interaction.locale
        if self.action == "take":
            await pick(interaction, self.event_id)
        elif self.action == "reserve":
            await answer(interaction, await signups.reserve(self.event_id, member),
                         t("You are on the reserve list; you'll be offered a freed slot.", locale))
        elif self.action in ("maybe", "cant", "notcoming"):
            status = "maybe" if self.action == "maybe" else "cant"
            await answer(interaction, await signups.answer(self.event_id, member, status), t("Noted.", locale))
        elif self.action == "coming":
            await answer(interaction, await signups.coming(self.event_id, member), t("You are coming. See you there!", locale),
                         ask_link=True)
        elif self.action == "leave":
            await answer(interaction, await signups.leave(self.event_id, member), t("You are off this event.", locale))


async def pick(interaction: discord.Interaction, event_id: int) -> None:
    """Squad, then free slot: only the squads with a slot this member may take."""
    s, member = services(interaction), member_of(interaction)
    board = await s.signups.board(event_id)
    open_squads = [(squad, [slot for slot in slots if slot.holder_id is None and not slot.locked
                            and slot.id not in board.offered
                            and (slot.required_role_id is None or slot.required_role_id in member.role_ids)])
                   for squad, slots in board.squads]
    open_squads = [(squad, slots) for squad, slots in open_squads if slots]
    if not open_squads:
        await reply(interaction, t("No slot is free for you; press Reserve to queue for one.", interaction.locale))
        return
    await reply(interaction, t("Pick the squad, then the slot.", interaction.locale),
                view=SlotPicker(interaction, event_id, open_squads))


class SlotPicker(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, event_id: int, squads: list) -> None:
        super().__init__(timeout=TIMEOUT)
        self.event_id, self.squads = event_id, squads
        choose = discord.ui.Select(placeholder=t("Squad", interaction.locale), options=[
            discord.SelectOption(label=f"{squad.name} ({len(slots)})"[:100], value=str(squad.id))
            for squad, slots in squads[:25]])
        choose.callback = lambda i, select=choose: self._squad(i, int(select.values[0]))
        self.add_item(choose)

    async def _squad(self, interaction: discord.Interaction, squad_id: int) -> None:
        slots = next(slots for squad, slots in self.squads if squad.id == squad_id)
        for item in list(self.children)[1:]:
            self.remove_item(item)
        choose = discord.ui.Select(placeholder=t("Slot", interaction.locale), options=[
            discord.SelectOption(label=slot.name[:100], value=str(slot.id)) for slot in slots[:25]])
        choose.callback = lambda i, select=choose: self._slot(i, int(select.values[0]))
        self.add_item(choose)
        await interaction.response.edit_message(view=self)

    async def _slot(self, interaction: discord.Interaction, slot_id: int) -> None:
        outcome = await services(interaction).signups.take(self.event_id, slot_id, member_of(interaction))
        await answer(interaction, outcome, t("You have the slot. See you there!", interaction.locale), ask_link=True)
        if outcome.ok:
            self.stop()


class OfferAction(discord.ui.DynamicItem[discord.ui.Button], template=r"offer:(?P<offer>\d+):(?P<action>take|decline)"):
    """*Take it* / *No thanks* on an offered slot, in a DM or in the event's thread."""

    def __init__(self, offer_id: int, action: str, label: str = "") -> None:
        style = discord.ButtonStyle.success if action == "take" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"offer:{offer_id}:{action}"))
        self.offer_id, self.action = offer_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> OfferAction:
        return cls(int(match["offer"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        signups, locale = services(interaction).signups, interaction.locale
        roles = services(interaction).channels.roles_of(interaction.user.id)  # (in a DM the user is no Member)
        member = Member(interaction.user.id, roles if roles is not None else frozenset())
        if self.action == "take":
            await answer(interaction, await signups.accept(self.offer_id, member),
                         t("You have the slot. See you there!", locale), ask_link=True)
        else:
            await answer(interaction, await signups.decline(self.offer_id, member), t("Noted.", locale))


def offer_buttons(offer_id: int, language: Any, everyone: bool = False) -> discord.ui.View:
    """*Take it*, and for an offer to one member *No thanks* (an offer to all just waits for the first)."""
    view = discord.ui.View(timeout=None)
    view.add_item(OfferAction(offer_id, "take", t("Take it", language)))
    if not everyone:
        view.add_item(OfferAction(offer_id, "decline", t("No thanks", language)))
    return view
