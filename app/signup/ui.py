"""Sign-up on Discord (docs/PLAN-front-end.md, *Commands and buttons*; how it looks: docs/PLAN.md, *ORBAT and slots*,
*Picking*, *Zeus sign-up*).

Every press is passed to the supervisor in the member's name (act-as, through ``Roster.as_member``), which decides;
a refusal reaching the button's error handler is answered in words (``discord_util.supervisor_words``). The squad
lists and buttons under the ORBAT (or a Zeus or training operation's list) and on an offer carry their ids in
``custom_id``, so they keep working after a restart. Picking a squad opens a private picker (decided 2026-11-01):
the squad (to change it), that squad's free slots, and *Sign up* to confirm. A list under a shared message is the
same for everyone, which is why the slots, depending on the squad picked, are private. Taking another slot moves the
sign-up. A missing Creator DLC is a warning in the answer, never a refusal."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, TypeVar

import discord

from app.channel import render
from app.discord_util import fleeting, reply, services, supervisor_words, thinking
from app.i18n import t
from app.links.ui import link_view
from app.supervisor_client import SupervisorError

T = TypeVar("T")
TIMEOUT = 15 * 60
ORBAT_ACTIONS = ("reserve", "maybe", "cant", "leave")  # (taking a slot goes through the squad lists)
COMING_ACTIONS = ("coming", "cant", "leave")  # (``cant`` is a Zeus operation's Not coming)
LISTS = 4  # squad lists under the ORBAT; the fifth row holds the buttons
PER_LIST = 25  # Discord's options per list


async def act(interaction: discord.Interaction, call: Callable[[int], Awaitable[T]]) -> T:
    """``call(discord id)``, an act-as call in the name of whoever pressed (pushed to the roster first if the
    supervisor doesn't know them yet)."""
    return await services(interaction).roster.as_member(interaction.user.id, call)


def role_ids(interaction: discord.Interaction) -> frozenset[int]:
    """The member's roles now; in a DM the user is no Member, so they come from the guild."""
    roles = getattr(interaction.user, "roles", None)
    if roles is not None:
        return frozenset(role.id for role in roles)
    return services(interaction).roles_of(interaction.user.id) or frozenset()


async def words(interaction: discord.Interaction, body: dict[str, Any], done: str,
                ask_link: bool = False) -> tuple[str, discord.ui.View | None]:
    """``done`` with the supervisor's DLC warning; with *Link now* when ``ask_link`` (a slot or *Coming*) and it is
    the member's first sign-up without a Steam link (docs/PLAN.md, *Supervisor: attendance*)."""
    locale, text = interaction.locale, done
    missing = (body.get("outcome") or {}).get("missing_dlc") or []
    if missing:
        text += "\n" + t("⚠ This mission needs Creator DLC you have no role for: {dlc}.", locale,
                         dlc=", ".join(str(m) for m in missing))
    links = services(interaction).links
    if ask_link and links is not None and await links.ask_at_signup(interaction.user.id):
        text += "\n" + t("Link your Steam account so attendance counts you: press Link now, or use /link.", locale)
        return text, link_view(locale)
    return text, None


async def answer(interaction: discord.Interaction, body: dict[str, Any], done: str, ask_link: bool = False) -> None:
    """:func:`words`, as a private reply."""
    text, view = await words(interaction, body, done, ask_link)
    await reply(interaction, text, **({"view": view} if view is not None else {}))


def free(op: dict[str, Any], roles: frozenset[int] | None = None) -> list[tuple[dict[str, Any], list[tuple[int, Any]]]]:
    """Each squad with its free slots as ``(number as the ORBAT shows it, slot)``, without the squads that have none;
    with ``roles``, only the slots those roles allow (the supervisor checks it again)."""
    found = []
    for squad in op.get("orbat") or []:
        open_slots = []
        for number, slot in enumerate(squad.get("slots") or [], 1):
            if slot.get("holder") or slot.get("locked") or slot.get("offered"):
                continue
            required = str((slot.get("required_role") or {}).get("id") or "")
            if roles is not None and required and (not required.isdigit() or int(required) not in roles):
                continue
            open_slots.append((number, slot))
        if open_slots:
            found.append((squad, open_slots))
    return found


def buttons(op: dict[str, Any], language: Any, now: datetime) -> discord.ui.View | None:
    """Under the last ORBAT message of a published operation (None otherwise: no buttons). Closed (started): all
    off. Not open yet: only Maybe, Can't come (Not coming) and Leave work. Full: Coming is off."""
    if op["status"] != "published":
        return None
    state, op_id = render.phase(op, now), int(op["id"])
    closed, not_open = state == "closed", state == "not_open"
    full = bool((op.get("signup") or {}).get("full"))
    coming = op["kind"] in render.COMING
    labels = {"reserve": t("Reserve", language), "maybe": t("Maybe", language), "leave": t("Leave", language),
              "cant": t("Not coming", language) if coming else t("Can't come", language),
              "coming": t("Coming", language)}
    styles = {"coming": discord.ButtonStyle.success, "leave": discord.ButtonStyle.danger}
    view = discord.ui.View(timeout=None)
    if not coming:
        squads = free(op)
        chunks = [squads[i:i + PER_LIST] for i in range(0, len(squads), PER_LIST)][:LISTS] or [[]]
        for number, chunk in enumerate(chunks):
            view.add_item(SquadList(op_id, number, chunk, language, disabled=closed or not_open))
    for action in COMING_ACTIONS if coming else ORBAT_ACTIONS:
        button = SignupAction(op_id, action, labels[action], styles.get(action, discord.ButtonStyle.secondary))
        button.item.disabled = closed or (not_open and action in ("reserve", "coming")) or (full and action == "coming")
        button.item.row = LISTS
        view.add_item(button)
    return view


class SquadList(discord.ui.DynamicItem[discord.ui.Select], template=r"squad:(?P<op>\d+):(?P<number>\d)"):
    """The squads with a free slot, under the ORBAT: picking one opens the private picker on it."""

    def __init__(self, op_id: int, number: int, squads: list | None = None, language: Any = None,
                 *, disabled: bool = False) -> None:
        squads = squads or []
        if squads:
            placeholder = t("Pick a squad to sign up", language)
            options = [discord.SelectOption(label=str(squad["name"])[:100], value=str(squad["id"]),
                                            description=t(("{count} free slot", "{count} free slots"), language,
                                                          count=len(slots))) for squad, slots in squads]
        else:
            placeholder = t("No slot is free: press Reserve to queue for one.", language)
            options = [discord.SelectOption(label="—", value="0")]
        super().__init__(discord.ui.Select(custom_id=f"squad:{op_id}:{number}", placeholder=placeholder[:150],
                                           options=options, disabled=disabled or not squads, row=number))
        self.op_id = op_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match: Any) -> SquadList:
        return cls(int(match["op"]), int(match["number"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        values = (interaction.data or {}).get("values") or ["0"]
        await thinking(interaction)  # (a rush at sign-up opening: answer within Discord's 3 s)
        await pick(interaction, self.op_id, int(values[0]))


class SignupAction(discord.ui.DynamicItem[discord.ui.Button], template=r"signup:(?P<op>\d+):(?P<action>[a-z]+)"):
    def __init__(self, op_id: int, action: str, label: str = "",
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"signup:{op_id}:{action}"))
        self.op_id, self.action = op_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> SignupAction:
        return cls(int(match["op"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        await thinking(interaction)
        await press(interaction, self.op_id, self.action)


async def press(interaction: discord.Interaction, op_id: int, action: str) -> None:
    """A sign-up button (or ``/slot leave``): the supervisor's answer in words."""
    client, locale = services(interaction).supervisor, interaction.locale
    if action == "take":
        await pick(interaction, op_id)
    elif action == "leave":
        await answer(interaction, await act(interaction, lambda me: client.leave(me, op_id)),
                     t("You are off this operation.", locale))
    elif action == "reserve":
        await answer(interaction, await act(interaction, lambda me: client.answer(me, op_id, "reserve")),
                     t("You are on the reserve list; you'll be offered a freed slot.", locale))
    elif action in ("maybe", "cant", "notcoming"):
        status = "maybe" if action == "maybe" else "cant"
        await answer(interaction, await act(interaction, lambda me: client.answer(me, op_id, status)),
                     t("Noted.", locale))
    elif action == "coming":
        await answer(interaction, await act(interaction, lambda me: client.answer(me, op_id, "coming")),
                     t("You are coming. See you there!", locale), ask_link=True)


def why_not(op: dict[str, Any], now: datetime, locale: Any) -> str:
    """Why the member can't take a slot now (the supervisor's ``actions`` left out ``take``)."""
    state = render.phase(op, now)
    if state == "not_published":
        return t("The operation isn't published yet.", locale)
    if state == "not_open":
        return t("Sign-up isn't open yet.", locale)
    if state == "closed":
        return t("Sign-up has closed.", locale)
    if ((op.get("my") or {}).get("place") or {}).get("locked"):
        return t("An organiser assigned you a slot: ask them to move you.", locale)
    return t("You don't have the permission for this.", locale)


async def pick(interaction: discord.Interaction, op_id: int, squad_id: int | None = None) -> None:
    """The private picker, on ``squad_id`` if it has a slot this member may take; the operation read in the member's
    name, so the picker offers only what the supervisor would allow."""
    s, locale = services(interaction), interaction.locale
    op = await act(interaction, lambda me: s.supervisor.operation(op_id, act_as=me))
    if "take" not in ((op.get("my") or {}).get("actions") or []):
        await reply(interaction, why_not(op, s.scheduler.clock(), locale))
        return
    squads = free(op, role_ids(interaction))
    if not squads:
        await reply(interaction, t("No slot is free for you; press Reserve to queue for one.", locale))
        return
    picker = SlotPicker(interaction, op_id, squads, squad_id)
    await reply(interaction, picker.prompt(), view=picker)


class SlotPicker(discord.ui.View):
    """Squad, slot, *Sign up*: lives in memory for 15 minutes, like any private picker."""

    def __init__(self, interaction: discord.Interaction, op_id: int, squads: list, squad_id: int | None) -> None:
        super().__init__(timeout=TIMEOUT)
        self.op_id, self.squads, self.locale = op_id, squads, interaction.locale
        self.squad_id = squad_id if any(squad["id"] == squad_id for squad, _ in squads) else None
        self.slot_id: int | None = None
        self._draw()

    def prompt(self) -> str:
        return t("Pick the slot, then Sign up.", self.locale) if self.squad_id else t("Pick the squad, then the slot.",
                                                                                     self.locale)

    def _draw(self) -> None:
        self.clear_items()
        squad = discord.ui.Select(placeholder=t("Squad", self.locale), row=0, options=[
            discord.SelectOption(label=str(s["name"])[:100], value=str(s["id"]), default=s["id"] == self.squad_id,
                                 description=t(("{count} free slot", "{count} free slots"), self.locale, count=len(slots)))
            for s, slots in self.squads[:PER_LIST]])
        squad.callback = lambda i, select=squad: self._squad(i, int(select.values[0]))
        self.add_item(squad)
        slots = next((slots for s, slots in self.squads if s["id"] == self.squad_id), [])
        if slots:
            slot = discord.ui.Select(placeholder=t("Slot", self.locale), row=1, options=[
                discord.SelectOption(label=f"{number}. {item['name']}"[:100], value=str(item["id"]),
                                     default=item["id"] == self.slot_id) for number, item in slots[:PER_LIST]])
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
        """Taken: the picker becomes the answer. Refused (say someone was faster): the refusal above fresh lists."""
        client, slot_id = services(interaction).supervisor, self.slot_id
        try:
            body = await act(interaction, lambda me: client.take_slot(me, self.op_id, slot_id))
        except SupervisorError as exc:
            op = await act(interaction, lambda me: client.operation(self.op_id, act_as=me))
            self.squads = free(op, role_ids(interaction)) or self.squads
            self.slot_id = None
            self._draw()
            await interaction.response.edit_message(content=supervisor_words(exc, self.locale), view=self)
            return
        text, link = await words(interaction, body, t("You have the slot. See you there!", self.locale), ask_link=True)
        self.stop()
        await interaction.response.edit_message(content=text, view=link,
                                                delete_after=fleeting(interaction) if link is None else None)


class OfferAction(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"offer:(?P<op>\d+):(?P<offer>\d+):(?P<action>take|decline)"):
    """*Take it* / *No thanks* on an offered slot, in a DM or in the operation's thread."""

    def __init__(self, op_id: int, offer_id: int, action: str, label: str = "") -> None:
        style = discord.ButtonStyle.success if action == "take" else discord.ButtonStyle.secondary
        super().__init__(discord.ui.Button(label=label or action, style=style,
                                           custom_id=f"offer:{op_id}:{offer_id}:{action}"))
        self.op_id, self.offer_id, self.action = op_id, offer_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> OfferAction:
        return cls(int(match["op"]), int(match["offer"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        client, locale = services(interaction).supervisor, interaction.locale
        await thinking(interaction)
        if self.action == "take":
            body = await act(interaction, lambda me: client.accept_offer(me, self.op_id, self.offer_id))
            await answer(interaction, body, t("You have the slot. See you there!", locale), ask_link=True)
        else:
            await act(interaction, lambda me: client.decline_offer(me, self.op_id, self.offer_id))
            await reply(interaction, t("Noted.", locale))


def offer_buttons(op_id: int, offer_id: int, language: Any, everyone: bool = False) -> discord.ui.View:
    """*Take it*, and for an offer to one member *No thanks* (an offer to all just waits for the first)."""
    view = discord.ui.View(timeout=None)
    view.add_item(OfferAction(op_id, offer_id, "take", t("Take it", language)))
    if not everyone:
        view.add_item(OfferAction(op_id, offer_id, "decline", t("No thanks", language)))
    return view
