"""Sign-up on Discord (docs/PLAN.md, *ORBAT and slots*, *Picking*, *Zeus sign-up*).

The squad lists and buttons under the ORBAT (or a Zeus event's list) and on an offer carry their ids in
``custom_id``, so they keep working after a restart. Picking a squad under the ORBAT opens a private picker (decided
2026-11-01): the squad (to change it), that squad's free slots, and *Sign up* to confirm. A list under a shared message
is the same for everyone, which is why the slots, depending on the squad picked, are private. Taking another slot moves
the sign-up. Answers are the services' refusals in the member's words, and a missing Creator DLC is a warning, never a
refusal."""

from __future__ import annotations

from typing import Any

import discord

from app.discord_util import fleeting, reply, services
from app.events.words import refusal
from app.i18n import t
from app.links.ui import link_view
from app.presets import CDLCS
from app.signup.service import Member, Outcome, Refusal

TIMEOUT = 15 * 60
ORBAT_ACTIONS = ("reserve", "maybe", "cant", "leave")  # (an older message's "take" still opens the picker)
LISTS = 4  # squad lists under the ORBAT; the fifth row holds the buttons
PER_LIST = 25  # Discord's options per list
ZEUS_ACTIONS = ("coming", "notcoming", "leave")


def member_of(interaction: discord.Interaction) -> Member:
    return Member(interaction.user.id, frozenset(role.id for role in getattr(interaction.user, "roles", ())))


async def words(interaction: discord.Interaction, outcome: Outcome, done: str,
                ask_link: bool = False) -> tuple[str, discord.ui.View | None]:
    """``done`` on success (with the DLC warning), the refusal otherwise; with *Link now* when ``ask_link`` (a slot
    or *Coming*) and it's the member's first without a Steam link (docs/PLAN.md, *Supervisor: attendance*)."""
    locale = interaction.locale
    if not outcome.ok:
        other = None
        if outcome.refusal == Refusal.OVERLAP and outcome.other_event:
            other = (await services(interaction).events.get(outcome.other_event)).channel_id
        return refusal(outcome.refusal, locale, other), None
    text = done
    if outcome.missing_dlc:
        names = ", ".join(CDLCS[k].name for k in outcome.missing_dlc if k in CDLCS)
        text += "\n" + t("⚠ This mission needs Creator DLC you have no role for: {dlc}.", locale, dlc=names)
    links = services(interaction).links
    if ask_link and links is not None and await links.ask_at_signup(interaction.user.id):
        text += "\n" + t("Link your Steam account so attendance counts you: press Link now, or use /link.", locale)
        return text, link_view(locale)
    return text, None


async def answer(interaction: discord.Interaction, outcome: Outcome, done: str, ask_link: bool = False) -> None:
    """:func:`words`, as a private reply."""
    text, view = await words(interaction, outcome, done, ask_link)
    await reply(interaction, text, **({"view": view} if view is not None else {}))


def free(board: Any, member: Member | None = None) -> list[tuple[Any, list[tuple[int, Any]]]]:
    """Each squad with its free slots as ``(number as the ORBAT shows it, slot)``, without the squads that have
    none; with ``member``, only the slots their roles allow."""
    found = []
    for squad, slots in board.squads:
        open_slots = [(number, slot) for number, slot in enumerate(slots, 1)
                      if slot.holder_id is None and not slot.locked and slot.id not in board.offered
                      and (member is None or slot.required_role_id is None or slot.required_role_id in member.role_ids)]
        if open_slots:
            found.append((squad, open_slots))
    return found


def buttons(event_id: int, zeus: bool, language: str, *, closed: bool = False, not_open: bool = False,
            full: bool = False, board: Any = None) -> discord.ui.View:
    """``closed``: the event has started, nothing works. ``not_open``: before sign-up opens only Maybe, Can't come
    (Not coming) and Leave work. ``full``: a Zeus event's limit is reached, Coming is off until someone leaves.
    ``board``: an ORBAT event's, for the slot lists."""
    labels = {"take": t("Sign up", language), "reserve": t("Reserve", language), "maybe": t("Maybe", language),
              "cant": t("Can't come", language), "leave": t("Leave", language), "coming": t("Coming", language),
              "notcoming": t("Not coming", language)}
    styles = {"take": discord.ButtonStyle.success, "coming": discord.ButtonStyle.success,
              "leave": discord.ButtonStyle.danger}
    view = discord.ui.View(timeout=None)
    if not zeus and board is not None:
        squads = free(board)
        chunks = [squads[i:i + PER_LIST] for i in range(0, len(squads), PER_LIST)][:LISTS] or [[]]
        for number, chunk in enumerate(chunks):
            view.add_item(SquadList(event_id, number, chunk, language, disabled=closed or not_open))
    for action in ZEUS_ACTIONS if zeus else ORBAT_ACTIONS:
        button = SignupAction(event_id, action, labels[action], styles.get(action, discord.ButtonStyle.secondary))
        button.item.disabled = closed or (not_open and action in ("take", "reserve", "coming")) or \
            (full and action == "coming")
        button.item.row = LISTS
        view.add_item(button)
    return view


class SquadList(discord.ui.DynamicItem[discord.ui.Select], template=r"squad:(?P<event>\d+):(?P<number>\d)"):
    """The squads with a free slot, under the ORBAT: picking one opens the private picker on it."""

    def __init__(self, event_id: int, number: int, squads: list | None = None, language: Any = None,
                 *, disabled: bool = False) -> None:
        squads = squads or []
        if squads:
            placeholder = t("Pick a squad to sign up", language)
            options = [discord.SelectOption(label=f"{squad.name}"[:100], value=str(squad.id),
                                            description=t(("{count} free slot", "{count} free slots"), language,
                                                          count=len(slots))) for squad, slots in squads]
        else:
            placeholder = t("No slot is free: press Reserve to queue for one.", language)
            options = [discord.SelectOption(label="—", value="0")]
        super().__init__(discord.ui.Select(custom_id=f"squad:{event_id}:{number}", placeholder=placeholder[:150],
                                           options=options, disabled=disabled or not squads, row=number))
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match: Any) -> SquadList:
        return cls(int(match["event"]), int(match["number"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or ["0"]
        await pick(interaction, self.event_id, int(values[0]))


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


async def pick(interaction: discord.Interaction, event_id: int, squad_id: int | None = None) -> None:
    """The private picker, on ``squad_id`` if it has a slot this member may take."""
    s, member = services(interaction), member_of(interaction)
    squads = free(await s.signups.board(event_id), member)
    if not squads:
        await reply(interaction, t("No slot is free for you; press Reserve to queue for one.", interaction.locale))
        return
    picker = SlotPicker(interaction, event_id, squads, squad_id)
    await reply(interaction, picker.prompt(), view=picker)


class SlotPicker(discord.ui.View):
    """Squad, slot, *Sign up*: lives in memory for 15 minutes, like any private picker."""

    def __init__(self, interaction: discord.Interaction, event_id: int, squads: list, squad_id: int | None) -> None:
        super().__init__(timeout=TIMEOUT)
        self.event_id, self.squads, self.locale = event_id, squads, interaction.locale
        self.squad_id = squad_id if any(squad.id == squad_id for squad, _ in squads) else None
        self.slot_id: int | None = None
        self._draw()

    def prompt(self) -> str:
        return t("Pick the slot, then Sign up.", self.locale) if self.squad_id else t("Pick the squad, then the slot.",
                                                                                     self.locale)

    def _draw(self) -> None:
        self.clear_items()
        squad = discord.ui.Select(placeholder=t("Squad", self.locale), row=0, options=[
            discord.SelectOption(label=s.name[:100], value=str(s.id), default=s.id == self.squad_id,
                                 description=t(("{count} free slot", "{count} free slots"), self.locale, count=len(slots)))
            for s, slots in self.squads[:PER_LIST]])
        squad.callback = lambda i, select=squad: self._squad(i, int(select.values[0]))
        self.add_item(squad)
        slots = next((slots for s, slots in self.squads if s.id == self.squad_id), [])
        if slots:
            slot = discord.ui.Select(placeholder=t("Slot", self.locale), row=1, options=[
                discord.SelectOption(label=f"{number}. {item.name}"[:100], value=str(item.id),
                                     default=item.id == self.slot_id) for number, item in slots[:PER_LIST]])
            slot.callback = lambda i, select=slot: self._slot(i, int(select.values[0]))
            self.add_item(slot)
        confirm = discord.ui.Button(label=t("Sign up", self.locale), style=discord.ButtonStyle.success, row=2,
                                    disabled=self.slot_id is None)
        confirm.callback = self._confirm
        self.add_item(confirm)

    async def _squad(self, interaction: discord.Interaction, squad_id: int) -> None:
        self.squad_id, self.slot_id = squad_id, None
        self._draw()
        await interaction.response.edit_message(content=self.prompt(), view=self)

    async def _slot(self, interaction: discord.Interaction, slot_id: int) -> None:
        self.slot_id = slot_id
        self._draw()
        await interaction.response.edit_message(view=self)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        """Taken: the picker becomes the answer. Refused (say someone was faster): the answer above fresh lists."""
        s, member = services(interaction), member_of(interaction)
        outcome = await s.signups.take(self.event_id, self.slot_id, member)
        text, link = await words(interaction, outcome, t("You have the slot. See you there!", self.locale), ask_link=True)
        if outcome.ok:
            self.stop()
            await interaction.response.edit_message(content=text, view=link,
                                                    delete_after=fleeting(interaction) if link is None else None)
            return
        self.squads = free(await s.signups.board(self.event_id), member) or self.squads
        self.slot_id = None
        self._draw()
        await interaction.response.edit_message(content=text, view=self)


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
