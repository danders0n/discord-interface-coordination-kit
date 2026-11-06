"""Creating and organising events on Discord (docs/PLAN.md, *Creating an event*, *Organiser panel*).

- *Create event* in the header channel opens the **wizard**: kind, climate and the other groups in
  selects, then title, date and times in a modal. It lives in memory; after a restart its buttons say the session
  expired (:func:`expired`), and the member starts again. Its last step makes the draft and its channel.
- *Organiser panel* under an event's info opens the **panel** privately: a button per section. These buttons
  carry the event id (:class:`PanelAction`), so they keep working after a restart.

Everything here only gathers input and words the answers; the rules are in the services."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import discord

from app.discord_util import finish, reply, services, thinking
from app.eventchannel.render import kind_label
from app.events import times as event_times
from app.events.groups import GroupError, Groups
from app.events.service import KINDS, Draft, EventError, Problem
from app.events.words import event_problem, template_problem, time_problem
from app.i18n import t
from app.orbat.parser import OrbatError, ParsedSlot, parse, render
from app.orbat.templates import TemplateError, Templates
from app.permissions import Perm
from app.signup.service import COMING, Member

log = logging.getLogger(__name__)
LIVE: set[str] = set()  # the wizards open in this run (their custom ids start with wizard:<token>)
TIMEOUT = 15 * 60


def member_of(interaction: discord.Interaction) -> Member:
    return Member(interaction.user.id, frozenset(role.id for role in getattr(interaction.user, "roles", ())))


async def guarded(interaction: discord.Interaction, action: Callable[[], Awaitable[Any]], *, slow: bool = True) -> Any:
    """Run ``action``; a refusal from the services is answered in the member's words. ``slow``: answer
    "thinking..." first (:func:`thinking`), as the services may touch channels or the supervisor; False only for a
    quick read that a modal or a message edit follows, as neither can come after it."""
    if slow:
        await thinking(interaction)
    try:
        return await action()
    except EventError as exc:
        await reply(interaction, event_problem(exc.problem, interaction.locale, exc.missing))
    except event_times.TimeError as exc:
        await reply(interaction, time_problem(exc.problem, interaction.locale))
    except TemplateError as exc:
        await reply(interaction, template_problem(exc.problem, interaction.locale))
    except OrbatError as exc:
        await reply(interaction, "\n".join(exc.problems)[:1900])
    except GroupError as exc:
        await reply(interaction, t("Another group has that acronym.", interaction.locale) if str(exc) == "acronym_taken"
                    else t("Give the group an acronym of up to 10 letters or digits and a name.", interaction.locale))
    return None


def orbat_summary(squads: list, locale: Any) -> str:
    """What was saved, squad by squad, so a squad line missing its ``#`` shows at once."""
    listed = ", ".join(f"{squad.name} ({len(squad.slots)})" for squad in squads)
    return t(("Saved: {count} squad: {squads}.", "Saved: {count} squads: {squads}."), locale, count=len(squads),
             squads=listed)[:1900]


async def _done(work: Awaitable[Any]) -> bool:
    """For :func:`guarded`: True once ``work`` is done, as its None result would read as a refusal."""
    await work
    return True


async def expired(interaction: discord.Interaction) -> bool:
    """A wizard button from before a restart: say so. True when it answered."""
    custom_id = (interaction.data or {}).get("custom_id", "") if interaction.type == discord.InteractionType.component else ""
    if custom_id.startswith("wizard:") and custom_id.split(":")[1] not in LIVE:
        await reply(interaction, t("This wizard has expired (the bot restarted). Press Create event again.",
                                   interaction.locale))
        return True
    return False


# ---------------------------------------------------------------------------- the wizard and its choices

class Choices(discord.ui.View):
    """Kind, climate and other groups: the wizard's first step, and the panel's *Type and groups*."""

    def __init__(self, interaction: discord.Interaction, groups: list, *, event: Any = None,
                 guests: list[int] | None = None) -> None:
        """``guests``: a hosted event's invited groups now (the panel)."""
        super().__init__(timeout=TIMEOUT)
        self.token = secrets.token_hex(6)
        LIVE.add(self.token)
        self.locale, self.event, self.groups = interaction.locale, event, groups
        s = services(interaction)
        self.member = member_of(interaction)
        may_guest = s.events.access(self.member).publishes_without_approval
        self.kind = event.kind if event else None
        self.climate = event.climate_role_id if event else None
        self.host = event.host_group_id if event else None
        self.guests: list[int] = list(guests or [])
        self.may_correct = s.events.access(self.member).has(Perm.MANAGE_ANY_EVENT)
        if event is None or event.status == "draft":
            self.add_item(self._select("kind", t("Kind", self.locale),
                                       [(kind_label(k, self.locale, s.settings.events.kind_names), k) for k in KINDS
                                        if k != "guest" or may_guest],
                                       self.kind))
        climates = s.ids.climates
        if climates:
            self.add_item(self._select("climate", t("Climate", self.locale),
                                       [(name, str(role)) for role, name in climates.items()],
                                       str(self.climate) if self.climate else None))
        self._group_items()

    def _select(self, part: str, placeholder: str, options: list[tuple[str, str]], current: str | set[str] | None,
                many: bool = False) -> discord.ui.Select:
        chosen = current if isinstance(current, set) else {current}
        select = discord.ui.Select(custom_id=f"wizard:{self.token}:{part}", placeholder=placeholder,
                                   min_values=0 if many else 1, max_values=min(len(options), 25) if many else 1,
                                   options=[discord.SelectOption(label=label[:100], value=value,
                                                                 default=value in chosen) for label, value in options[:25]])
        select.callback = lambda interaction, s=select, p=part: self._chosen(interaction, p, s.values)
        return select

    def _group_items(self) -> None:
        for item in [i for i in self.children
                     if getattr(i, "custom_id", "").split(":")[-1] in ("groups", "newgroup", "fixgroup", "go")]:
            self.remove_item(item)
        if self.kind in ("guest", "hosted") and self.groups:
            options = [(f"{g.acronym} · {g.name}", str(g.id)) for g in self.groups]
            label = t("Host group", self.locale) if self.kind == "guest" else \
                t("Invited groups: any number; New group adds another", self.locale)
            current = (str(self.host) if self.host else None) if self.kind == "guest" else {str(g) for g in self.guests}
            self.add_item(self._select("groups", label, options, current, many=self.kind == "hosted"))
        if self.kind in ("guest", "hosted"):
            button = discord.ui.Button(custom_id=f"wizard:{self.token}:newgroup", label=t("New group", self.locale))
            button.callback = self._new_group
            self.add_item(button)
            if self.may_correct and self.groups:
                fix = discord.ui.Button(custom_id=f"wizard:{self.token}:fixgroup", label=t("Correct a group", self.locale))
                fix.callback = self._fix_group
                self.add_item(fix)
        go = discord.ui.Button(custom_id=f"wizard:{self.token}:go", style=discord.ButtonStyle.primary,
                               label=t("Continue", self.locale) if self.event is None else t("Save", self.locale))
        go.callback = self._go
        self.add_item(go)

    async def _chosen(self, interaction: discord.Interaction, part: str, values: list[str]) -> None:
        for item in self.children:  # redrawn without defaults, Discord would show the select empty again
            if getattr(item, "custom_id", "") == f"wizard:{self.token}:{part}":
                for option in item.options:
                    option.default = option.value in values
        if part == "kind":
            self.kind = values[0]
            self._group_items()
        elif part == "climate":
            self.climate = int(values[0])
        elif part == "groups":
            if self.kind == "guest":
                self.host = int(values[0]) if values else None
            else:
                self.guests = [int(v) for v in values]
        await interaction.response.edit_message(view=self)

    async def _new_group(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NewGroup(self))

    async def _fix_group(self, interaction: discord.Interaction) -> None:
        """*Manage any event* only: pick a group, then correct its acronym or name in a modal."""
        select = discord.ui.Select(placeholder=t("Correct a group", self.locale), options=[
            discord.SelectOption(label=f"{g.acronym} · {g.name}"[:100], value=str(g.id)) for g in self.groups[:25]])

        async def chosen(i: discord.Interaction) -> None:
            group = next(g for g in self.groups if g.id == int(select.values[0]))
            await i.response.send_modal(CorrectGroup(self, group))

        select.callback = chosen
        view = discord.ui.View(timeout=TIMEOUT)
        view.add_item(select)
        await reply(interaction, t("Correct a group", self.locale), view=view)

    async def _go(self, interaction: discord.Interaction) -> None:
        if not self.kind:
            await reply(interaction, t("Pick the kind first.", self.locale))
            return
        if self.kind == "guest" and self.host is None:
            await reply(interaction, event_problem(Problem.NEEDS_HOST, self.locale))
            return
        if self.event is None:
            await interaction.response.send_modal(DetailsModal(interaction, choices=self))
            return
        s = services(interaction)
        changes: dict[str, Any] = {"climate_role_id": self.climate}
        if self.event.status == "draft":
            changes["kind"] = self.kind
        if self.kind == "guest":
            changes["host_group_id"] = self.host
        if self.kind == "hosted":  # (all of them unticked: no invited groups)
            changes["guest_group_ids"] = self.guests
        await interaction.response.defer()  # (saving may move the reservation; finish() edits these choices)

        async def save() -> bool:
            await s.events.update(self.event.id, member_of(interaction), **changes)
            return True

        if await guarded(interaction, save):
            await finish(interaction, t("Saved.", self.locale))

    def stop(self) -> None:
        LIVE.discard(self.token)
        super().stop()

    async def on_timeout(self) -> None:
        LIVE.discard(self.token)


class NewGroup(discord.ui.Modal):
    def __init__(self, choices: Choices) -> None:
        super().__init__(title=t("New group", choices.locale)[:45], timeout=TIMEOUT)
        self.choices = choices
        self.acronym = discord.ui.TextInput(label=t("Acronym", choices.locale), max_length=10)
        self.name = discord.ui.TextInput(label=t("Full name", choices.locale), max_length=100)
        self.add_item(self.acronym)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        groups = Groups(services(interaction).db)
        group = await guarded(interaction, lambda: groups.add(self.acronym.value, self.name.value, interaction.user.id),
                              slow=False)
        if group is None:
            return
        self.choices.groups = await groups.all()
        if self.choices.kind == "guest":
            self.choices.host = group.id
        else:
            self.choices.guests = sorted({*self.choices.guests, group.id})
        self.choices._group_items()
        await interaction.response.edit_message(view=self.choices)


class CorrectGroup(discord.ui.Modal):
    """A group's acronym or name corrected; the channels and messages of its events follow."""

    def __init__(self, choices: Choices, group: Any) -> None:
        super().__init__(title=t("Correct a group", choices.locale)[:45], timeout=TIMEOUT)
        self.choices, self.group = choices, group
        self.acronym = discord.ui.TextInput(label=t("Acronym", choices.locale), max_length=10, default=group.acronym)
        self.name = discord.ui.TextInput(label=t("Full name", choices.locale), max_length=100, default=group.name)
        self.add_item(self.acronym)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s = services(interaction)
        if not s.events.access(member_of(interaction)).has(Perm.MANAGE_ANY_EVENT):
            await reply(interaction, t("You don't have the permission for this.", interaction.locale))
            return
        groups = Groups(s.db)
        before = f"{self.group.acronym} · {self.group.name}"
        group = await guarded(interaction, lambda: groups.correct(self.group.id, self.acronym.value, self.name.value))
        if group is None:
            return
        self.choices.groups = await groups.all()
        await s.channels.group_changed(group.id)
        s.staff.post(t("{member} corrected the group {before} to {after}.", s.settings.language,
                       member=interaction.user.mention, before=before, after=f"{group.acronym} · {group.name}"))
        await reply(interaction, t("Saved.", interaction.locale))


class DetailsModal(discord.ui.Modal):
    """Title, date and times: the wizard's last step, and the panel's *Details*."""

    def __init__(self, interaction: discord.Interaction, *, choices: Choices | None = None, event: Any = None) -> None:
        locale = interaction.locale
        super().__init__(title=t("Event details", locale)[:45], timeout=TIMEOUT)
        self.choices, self.event = choices, event
        s = services(interaction)
        tz = s.settings.tz
        local = event.assembly_at.astimezone(tz) if event else None
        self.name = discord.ui.TextInput(label=t("Title", locale), max_length=100, default=event.title if event else None)
        self.date = discord.ui.TextInput(label=t("Date (dd.mm)", locale), max_length=10,
                                         default=f"{local:%d.%m.%Y}" if local else None)
        self.assembly = discord.ui.TextInput(label=t("Assembly (HH:MM)", locale), max_length=5,
                                             default=f"{local:%H:%M}" if local else None)
        self.start = discord.ui.TextInput(label=t("Start (HH:MM)", locale), max_length=5,
                                          default=f"{event.start_at.astimezone(tz):%H:%M}" if event else None)
        minutes = event.duration_minutes if event else s.settings.times.default_duration_minutes
        self.duration = discord.ui.TextInput(label=t("Duration (e.g. 3h, 2h30)", locale), max_length=10,
                                             default=f"{minutes // 60}h{minutes % 60:02d}" if minutes % 60 else f"{minutes // 60}h")
        for item in (self.name, self.date, self.assembly, self.start, self.duration):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s = services(interaction)
        locale = interaction.locale

        async def save() -> Any:
            parsed = event_times.parse(self.date.value, self.assembly.value, self.start.value, self.duration.value,
                                       s.events.clock(), s.settings.tz, s.settings.times.max_days_ahead)
            member = member_of(interaction)
            if self.event is not None:
                return await s.events.update(self.event.id, member, title=self.name.value, times=parsed) or "saved"
            c = self.choices
            event = await s.events.create(Draft(c.kind, self.name.value, parsed, c.climate,
                                                c.host if c.kind == "guest" else None,
                                                c.guests if c.kind == "hosted" else []), member)
            c.stop()
            return await s.channels.open_draft(event.id)

        result = await guarded(interaction, save)
        if result is None:
            return
        if self.event is None:
            await reply(interaction, t("Your draft is ready: {channel}. Finish it there with Organiser panel.",
                                       locale, channel=result.mention))
        elif result == "saved":
            await reply(interaction, t("Saved.", locale))
        else:
            await reply(interaction, t("New times sent to the moderators; nothing changes until they approve.", locale))


class CreateEvent(discord.ui.DynamicItem[discord.ui.Button], template=r"event:create"):
    """*Create event* in the header channel."""

    def __init__(self, label: str = "Create event") -> None:
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.success, custom_id="event:create"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> CreateEvent:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        s = services(interaction)
        if not s.events.access(member_of(interaction)).may_create_events:
            await reply(interaction, t("Creating events is for members.", interaction.locale))
            return
        full = s.channels.full()
        if full is not None:  # Discord's 50 channels per category
            await reply(interaction, t("The category {category} is full (50 channels): staff must make room before "
                                       "a new event can be made.", interaction.locale, category=full))
            return
        view = Choices(interaction, await Groups(s.db).all())
        await reply(interaction, t("New event: pick the kind and the climate, then Continue.",
                                   interaction.locale), view=view)


# ---------------------------------------------------------------------------- the organiser panel

ACTIONS = ("details", "choices", "briefing", "orbat", "join", "signup", "limit", "organisers", "publish", "withdraw",
           "server", "retry", "endres", "autoend", "profile", "ratings", "cancel", "end")


class PanelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"event:panel:(?P<event>\d+)"):
    """*Organiser panel* under the event's info."""

    def __init__(self, event_id: int, label: str = "Organiser panel") -> None:
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, emoji="🛠️",
                                           custom_id=f"event:panel:{event_id}"))
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> PanelButton:
        return cls(int(match["event"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_panel(interaction, self.event_id)


async def open_panel(interaction: discord.Interaction, event_id: int) -> None:
    s = services(interaction)
    event = await guarded(interaction, lambda: s.events.get(event_id), slow=False)
    if event is None:
        return
    if not await s.events.can_organise(event, member_of(interaction)):
        await reply(interaction, t("The panel is for this event's organisers.", interaction.locale))
        return
    content, view = await panel(interaction, event)
    await reply(interaction, content, view=view)


async def panel(interaction: discord.Interaction, event: Any) -> tuple[str, discord.ui.View]:
    s, locale = services(interaction), interaction.locale
    member = member_of(interaction)
    access = s.events.access(member)
    status = {"draft": t("Draft", locale), "pending": t("Waiting for approval", locale), "published": t("Published", locale),
              "ended": t("Ended", locale), "cancelled": t("Cancelled", locale)}.get(event.status, event.status)
    lines = [f"**{event.title}** · {status}"]
    missing = await s.events.checklist(event)
    if event.status == "draft":
        lines.append(event_problem(Problem.INCOMPLETE, locale, missing) if missing else t("Ready to publish.", locale))
    booked = await s.reservations.get(event.id)
    if event.kind != "guest":
        state = booked.state if booked else "none"
        text = {"booked": t("Server reserved.", locale), "retrying": t("Reserving the server: retrying.", locale),
                "failed": t("No reservation: {reason}", locale, reason=booked.error if booked else ""),
                "none": t("No reservation yet.", locale)}.get(state, t("Reservation: {state}", locale, state=state))
        lines.append(text)
    lines += await _attention(s, event, locale)
    view = discord.ui.View(timeout=None)
    shown = ["details", "choices", "briefing"]
    shown += ["limit"] if event.kind in COMING else ["join", "orbat"] if event.kind == "guest" else ["orbat"]
    shown += ["signup", "organisers"]
    if event.status == "draft":
        shown.append("publish")
    if event.status == "pending":
        shown.append("withdraw")
    if event.kind in COMING and not access.publishes_without_approval and (booked is None or booked.state != "booked"):
        shown.append("server")
    if booked is not None and booked.state in ("failed", "retrying"):
        shown.append("retry")
    if booked is not None and booked.state == "booked" and s.events.clock() >= event.assembly_at:
        shown.append("endres")
    if event.kind != "guest" and s.profiles is not None and s.profiles.may_manage(member):
        shown.append("profile")  # the server profile: mission makers and manage any event
    if event.kind in ("internal", "hosted") and event.status in ("draft", "pending", "published"):
        shown.append("autoend")  # an evening of several missions: a finished one doesn't end it
    if event.attendance_state is not None and (event.kind != "guest" or access.is_administration):
        shown.append("ratings")  # a guest event's cooperation ratings are for staff only
    if event.status in ("draft", "pending", "published"):
        shown.append("end" if s.events.clock() >= event.start_at else "cancel")
    publish_label = t("Publish", locale) if access.publishes_without_approval or event.kind in COMING else t("Send for approval", locale)
    labels = {"details": t("Details", locale), "choices": t("Type and groups", locale), "briefing": t("Briefing", locale),
              "orbat": t("ORBAT", locale), "join": t("How to join", locale), "signup": t("Sign-up start", locale),
              "limit": t("Sign-up limit", locale), "organisers": t("Co-organisers", locale), "publish": publish_label,
              "withdraw": t("Withdraw request", locale), "server": t("Request the server", locale),
              "retry": t("Retry reservation", locale), "endres": t("End reservation", locale),
              "ratings": t("Ratings", locale), "profile": t("Server profile", locale),
              "autoend": t("Don't end automatically: on", locale) if event.dont_end_automatically else
              t("Don't end automatically: off", locale),
              "cancel": t("Cancel event", locale), "end": t("End event", locale)}
    for action in shown:
        style = discord.ButtonStyle.danger if action in ("cancel", "end", "endres") else \
            discord.ButtonStyle.success if action == "publish" else discord.ButtonStyle.secondary
        view.add_item(PanelAction(event.id, action, labels[action], style))
    return "\n".join(lines), view


async def _attention(s: Any, event: Any, locale: Any) -> list[str]:
    """What the organisers should look at: slots flagged ⚠ (the holder lost the slot's role), and the members
    signed up without a Creator DLC the preset needs (going by their DLC roles)."""
    from app.presets import CDLCS

    board = await s.signups.board(event.id)
    lines = [t("⚠ {slot} ({squad}): {member} lost the role it needs.", locale, slot=slot.name, squad=squad.name,
               member=f"<@{slot.holder_id}>")
             for squad, slots in board.squads for slot in slots if slot.flagged and slot.holder_id is not None]
    if event.required_dlc:
        attending = [slot.holder_id for slot in board.slots if slot.holder_id is not None] + board.status("coming")
        without = []
        for member_id in attending:
            missing = s.signups.missing_dlc(event, s.channels.roles_of(member_id) or ())
            if missing:
                without.append(f"<@{member_id}> ({', '.join(CDLCS[k].name for k in missing if k in CDLCS)})")
        if without:
            lines.append(t("Without the Creator DLC: {members}", locale, members=", ".join(without))[:1000])
    return lines


class PanelAction(discord.ui.DynamicItem[discord.ui.Button], template=r"event:act:(?P<event>\d+):(?P<action>[a-z]+)"):
    def __init__(self, event_id: int, action: str, label: str = "", style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"event:act:{event_id}:{action}"))
        self.event_id, self.action = event_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> PanelAction:
        return cls(int(match["event"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        s, locale = services(interaction), interaction.locale
        event = await guarded(interaction, lambda: s.events.get(self.event_id), slow=False)  # (handlers open modals)
        if event is None:
            return
        member = member_of(interaction)
        if not await s.events.can_organise(event, member):
            await reply(interaction, t("The panel is for this event's organisers.", locale))
            return
        handler = getattr(self, f"_{self.action}", None)
        if handler is None:
            await reply(interaction, t("That button is from an older version; open the panel again.", locale))
            return
        await handler(interaction, event, member)

    async def _details(self, interaction, event, member) -> None:
        await interaction.response.send_modal(DetailsModal(interaction, event=event))

    async def _choices(self, interaction, event, member) -> None:
        s = services(interaction)
        guests = [g.id for g in await s.events.guest_groups(event.id)]
        await reply(interaction, t("Type and groups", interaction.locale),
                    view=Choices(interaction, await Groups(s.db).all(), event=event, guests=guests))

    async def _briefing(self, interaction, event, member) -> None:
        await interaction.response.send_modal(BriefingModal(interaction, event))

    async def _orbat(self, interaction, event, member) -> None:
        await reply(interaction, t("ORBAT", interaction.locale), view=OrbatTools(interaction, event))

    async def _join(self, interaction, event, member) -> None:
        await interaction.response.send_modal(TextModal(
            interaction, t("How to join", interaction.locale), event.how_to_join, 1024,
            lambda text: services(interaction).events.set_how_to_join(event.id, member, text)))

    async def _signup(self, interaction, event, member) -> None:
        await interaction.response.send_modal(SignupStartModal(interaction, event))

    async def _limit(self, interaction, event, member) -> None:
        s = services(interaction)

        async def save(text: str) -> None:
            text = text.strip()
            if text and not text.isdigit():
                raise EventError(Problem.NOT_A_NUMBER)
            await s.events.set_limit(event.id, member, int(text) if text else None)

        await interaction.response.send_modal(TextModal(interaction, t("Sign-up limit", interaction.locale),
                                                        str(event.signup_limit or ""), 4, save, required=False))

    async def _organisers(self, interaction, event, member) -> None:
        await reply(interaction, t("Co-organisers", interaction.locale), view=Organisers(interaction, event))

    async def _publish(self, interaction, event, member) -> None:
        s = services(interaction)
        full = s.channels.full("events_category")  # publishing moves the channel there
        if full is not None and (s.events.access(member).publishes_without_approval or event.kind in COMING):
            await reply(interaction, t("The category {category} is full (50 channels): staff must make room before "
                                       "a new event can be made.", interaction.locale, category=full))
            return
        state = await guarded(interaction, lambda: services(interaction).events.publish(event.id, member))
        if state == "published":
            await reply(interaction, t("Published.", interaction.locale))
        elif state == "pending":
            await reply(interaction, t("Sent to the moderators for approval.", interaction.locale))

    async def _withdraw(self, interaction, event, member) -> None:
        if await guarded(interaction, lambda: _done(services(interaction).events.withdraw(event.id, member))):
            await reply(interaction, t("The request is withdrawn; the event is a draft again.", interaction.locale))

    async def _server(self, interaction, event, member) -> None:
        if await guarded(interaction, lambda: services(interaction).events.request_server(event.id, member)):
            await reply(interaction, t("The server is requested; a moderator decides.", interaction.locale))

    async def _retry(self, interaction, event, member) -> None:
        await thinking(interaction)
        row = await services(interaction).reservations.retry(event.id)  # (its listener redraws the requests)
        await reply(interaction, t("Server reserved.", interaction.locale) if row and row.state == "booked" else
                    t("No reservation: {reason}", interaction.locale, reason=row.error if row else "—"))

    async def _endres(self, interaction, event, member) -> None:
        s = services(interaction)
        await thinking(interaction)
        ended = await s.reservations.end(event.id)
        await reply(interaction, t("The reservation has ended.", interaction.locale) if ended else
                    t("The reservation isn't under way.", interaction.locale))
        if ended:
            s.staff.post(t("{member} ended the reservation of {title}.", s.settings.language,
                           member=interaction.user.mention, title=event.title))

    async def _autoend(self, interaction, event, member) -> None:
        value = not event.dont_end_automatically
        if await guarded(interaction, lambda: _done(services(interaction).events.set_dont_end_automatically(
                event.id, member, value))):
            await reply(interaction, t("A finished mission no longer ends this event; the reservation's end or End "
                                       "event does.", interaction.locale) if value else
                        t("A finished mission ends this event again.", interaction.locale))

    async def _profile(self, interaction, event, member) -> None:
        content, view = await profile_panel(interaction, event)
        await reply(interaction, content, view=view)

    async def _ratings(self, interaction, event, member) -> None:
        s, locale = services(interaction), interaction.locale
        if event.kind == "guest" and not s.events.access(member).is_administration:
            await reply(interaction, t("You don't have the permission for this.", locale))
            return
        ratings = await s.feedback.ratings(event.id)
        if not ratings:
            await reply(interaction, t("No ratings yet.", locale))
            return
        average = sum(r.stars for r in ratings) / len(ratings)
        from app.eventchannel.render import mention

        lines = [f"{'★' * r.stars}{'☆' * (5 - r.stars)} {mention(r.member_id)}" + (f": {r.comment}" if r.comment else "")
                 for r in ratings]
        embed = discord.Embed(title=t(("{average} ★ ({count} rating)", "{average} ★ ({count} ratings)"), locale,
                                      average=f"{average:.1f}", count=len(ratings)),
                              description="\n".join(lines)[:4096])
        await reply(interaction, embed=embed)

    async def _cancel(self, interaction, event, member) -> None:
        await interaction.response.send_modal(TextModal(
            interaction, t("Cancel event", interaction.locale), "", 500,
            lambda reason: services(interaction).events.cancel(event.id, member, reason),
            label=t("Reason", interaction.locale), done=t("Cancelled.", interaction.locale)))

    async def _end(self, interaction, event, member) -> None:
        s = services(interaction)
        if await guarded(interaction, lambda: _done(s.events.end(event.id, member))):
            await reply(interaction, t("The event has ended.", interaction.locale))
            s.staff.post(t("{member} ended {title}.", s.settings.language, member=interaction.user.mention,
                           title=event.title))


class TextModal(discord.ui.Modal):
    """One text field, saved by ``save(text)``."""

    def __init__(self, interaction: discord.Interaction, title: str, current: str, limit: int,
                 save: Callable[[str], Awaitable[Any]], *, label: str | None = None, required: bool = True,
                 done: str | None = None) -> None:
        super().__init__(title=title[:45], timeout=TIMEOUT)
        self.save, self.done = save, done
        self.text = discord.ui.TextInput(label=(label or title)[:45], style=discord.TextStyle.paragraph,
                                         default=current or None, max_length=limit, required=required)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        result = await guarded(interaction, lambda: self._save(self.text.value))
        if result is not None:
            await reply(interaction, result if isinstance(result, str) else self.done or t("Saved.", interaction.locale))

    async def _save(self, text: str) -> str | bool:
        """``save``'s answer when it gives one in words (the ORBAT's summary), else True."""
        result = await self.save(text)
        return result if isinstance(result, str) else True


class BriefingModal(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, event: Any) -> None:
        locale = interaction.locale
        super().__init__(title=t("Briefing", locale)[:45], timeout=TIMEOUT)
        self.event = event
        self.situation = discord.ui.TextInput(label=t("Situation", locale), style=discord.TextStyle.paragraph,
                                              default=event.briefing_situation or None, required=False, max_length=4000)
        self.mission = discord.ui.TextInput(label=t("Mission", locale), style=discord.TextStyle.paragraph,
                                            default=event.briefing_mission or None, required=False, max_length=4000)
        self.notes = discord.ui.TextInput(label=t("Notes", locale), style=discord.TextStyle.paragraph,
                                          default=event.briefing_notes or None, required=False, max_length=4000)
        for item in (self.situation, self.mission, self.notes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s = services(interaction)

        async def save() -> bool:
            await s.events.set_briefing(self.event.id, member_of(interaction), self.situation.value,
                                        self.mission.value, self.notes.value)
            return True

        if await guarded(interaction, save):
            await reply(interaction, t("Saved.", interaction.locale))


class SignupStartModal(discord.ui.Modal):
    """The date and time sign-up opens; empty: at publishing."""

    def __init__(self, interaction: discord.Interaction, event: Any) -> None:
        locale = interaction.locale
        super().__init__(title=t("Sign-up start", locale)[:45], timeout=TIMEOUT)
        self.event = event
        local = event.signup_opens_at.astimezone(services(interaction).settings.tz) if event.signup_opens_at else None
        self.date = discord.ui.TextInput(label=t("Date (dd.mm), empty: at publishing", locale)[:45], required=False,
                                         max_length=10, default=f"{local:%d.%m.%Y}" if local else None)
        self.time = discord.ui.TextInput(label=t("Time (HH:MM)", locale), required=False, max_length=5,
                                         default=f"{local:%H:%M}" if local else "18:00")
        self.add_item(self.date)
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s = services(interaction)

        async def save() -> bool:
            opens = None
            if self.date.value.strip():
                day = event_times.parse_date(self.date.value, s.events.clock(), s.settings.tz,
                                             s.settings.times.max_days_ahead)
                clock = event_times.parse_time(self.time.value or "00:00")
                opens = datetime.combine(day, clock, s.settings.tz)
            await s.events.set_signup_start(self.event.id, member_of(interaction), opens)
            return True

        if await guarded(interaction, save):
            await reply(interaction, t("Saved.", interaction.locale))


class Organisers(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, event: Any) -> None:
        super().__init__(timeout=TIMEOUT)
        self.event = event
        add = discord.ui.UserSelect(placeholder=t("Add a co-organiser", interaction.locale), max_values=5)
        add.callback = lambda i, select=add: self._change(i, [u.id for u in select.values], True)
        remove = discord.ui.UserSelect(placeholder=t("Remove a co-organiser", interaction.locale), max_values=5)
        remove.callback = lambda i, select=remove: self._change(i, [u.id for u in select.values], False)
        self.add_item(add)
        self.add_item(remove)

    async def _change(self, interaction: discord.Interaction, ids: list[int], add: bool) -> None:
        s, member = services(interaction), member_of(interaction)

        async def save() -> bool:
            for member_id in ids:
                if add:
                    await s.events.add_organiser(self.event.id, member, member_id)
                else:
                    await s.events.remove_organiser(self.event.id, member, member_id)
            return True

        if await guarded(interaction, save):
            await reply(interaction, t("Saved.", interaction.locale))


class OrbatTools(discord.ui.View):
    """Paste or edit the ORBAT, take a template, save, rename or delete one, and the organisers' slot tools."""

    def __init__(self, interaction: discord.Interaction, event: Any) -> None:
        super().__init__(timeout=TIMEOUT)
        self.event, locale = event, interaction.locale
        for action, label in (("paste", t("Write or paste", locale)), ("template", t("From a template", locale)),
                              ("save", t("Save as template", locale)), ("manage", t("Rename or delete templates", locale)),
                              ("slots", t("Lock, assign, remove", locale))):
            button = discord.ui.Button(label=label)
            button.callback = lambda i, a=action: getattr(self, f"_{a}")(i)
            self.add_item(button)

    async def _text(self, interaction: discord.Interaction) -> str:
        s = services(interaction)
        board = await s.signups.board(self.event.id)
        names = {role.id: role.name for role in getattr(interaction.guild, "roles", ())}
        squads = [(squad.name, [ParsedSlot(slot.name, slot.required_role_id,
                                           slot.locked and slot.holder_id is None) for slot in slots])
                  for squad, slots in board.squads]
        return render(squads, names) if squads else ""

    async def _paste(self, interaction: discord.Interaction) -> None:
        s, member = services(interaction), member_of(interaction)
        event = self.event

        async def save(text: str) -> None:
            roles = [(role.id, role.name) for role in getattr(interaction.guild, "roles", ())]
            squads = parse(text, roles, interaction.locale)
            await s.events.editable(event.id, member)
            displaced = await s.signups.set_orbat(event.id, squads)
            if displaced:
                log.info("ORBAT of event %s edited: %s moved to the reserve list", event.id, displaced)
            return orbat_summary(squads, interaction.locale)

        await interaction.response.send_modal(TextModal(interaction, t("ORBAT", interaction.locale),
                                                        await self._text(interaction), 4000, save))

    async def _template(self, interaction: discord.Interaction) -> None:
        templates = await Templates(services(interaction).db).all()
        if not templates:
            await reply(interaction, t("No templates yet: save one first.", interaction.locale))
            return
        select = discord.ui.Select(placeholder=t("From a template", interaction.locale),
                                   options=[discord.SelectOption(label=x.name[:100], value=str(x.id)) for x in templates[:25]])

        async def chosen(i: discord.Interaction) -> None:
            s = services(i)
            template = await Templates(s.db).get(int(select.values[0]))

            async def save() -> bool:
                roles = [(role.id, role.name) for role in getattr(i.guild, "roles", ())]
                squads = parse(template.text, roles, i.locale)
                await s.events.editable(self.event.id, member_of(i))  # locked while waiting for approval
                await s.signups.set_orbat(self.event.id, squads)
                return orbat_summary(squads, i.locale)

            summary = await guarded(i, save)
            if summary:
                await reply(i, summary)

        select.callback = chosen
        view = discord.ui.View(timeout=TIMEOUT)
        view.add_item(select)
        await reply(interaction, t("From a template", interaction.locale), view=view)

    async def _save(self, interaction: discord.Interaction) -> None:
        s, member = services(interaction), member_of(interaction)
        text = await self._text(interaction)
        if not text:
            await reply(interaction, t("There is no ORBAT to save yet.", interaction.locale))
            return
        manage_any = s.events.access(member).has(Perm.MANAGE_ANY_EVENT)
        await interaction.response.send_modal(TextModal(
            interaction, t("Save as template", interaction.locale), self.event.title, 100,
            lambda name: Templates(s.db).save(name, text, member.id, manage_any),
            label=t("Template name", interaction.locale)))

    async def _manage(self, interaction: discord.Interaction) -> None:
        s, member = services(interaction), member_of(interaction)
        manage_any = s.events.access(member).has(Perm.MANAGE_ANY_EVENT)
        mine = [x for x in await Templates(s.db).all() if manage_any or x.owner_id == member.id]
        if not mine:
            await reply(interaction, t("You have no templates to change.", interaction.locale))
            return
        await reply(interaction, t("Pick a template, then rename or delete it.", interaction.locale),
                    view=TemplateTools(interaction, mine, manage_any))

    async def _slots(self, interaction: discord.Interaction) -> None:
        board = await services(interaction).signups.board(self.event.id)
        if not board.squads:
            await reply(interaction, t("There is no ORBAT yet.", interaction.locale))
            return
        await reply(interaction, t("Pick the squad, then the slot.", interaction.locale),
                    view=SlotTools(interaction, self.event, board))


class TemplateTools(discord.ui.View):
    """The templates the member may change (their own; every one for *manage any event*): pick, then rename or
    delete. Overwriting is *Save as template* under the same name."""

    def __init__(self, interaction: discord.Interaction, templates: list, manage_any: bool) -> None:
        super().__init__(timeout=TIMEOUT)
        self.templates, self.manage_any, self.template_id = templates, manage_any, None
        pick = discord.ui.Select(placeholder=t("Template", interaction.locale), options=[
            discord.SelectOption(label=x.name[:100], value=str(x.id)) for x in templates[:25]])
        pick.callback = lambda i, s=pick: self._picked(i, int(s.values[0]))
        self.add_item(pick)

    async def _picked(self, interaction: discord.Interaction, template_id: int) -> None:
        self.template_id, locale = template_id, interaction.locale
        for item in list(self.children)[1:]:
            self.remove_item(item)
        for action, label, style in (("rename", t("Rename", locale), discord.ButtonStyle.secondary),
                                     ("delete", t("Delete", locale), discord.ButtonStyle.danger)):
            button = discord.ui.Button(label=label, style=style, row=1)
            button.callback = lambda i, a=action: getattr(self, f"_{a}")(i)
            self.add_item(button)
        await interaction.response.edit_message(view=self)

    async def _rename(self, interaction: discord.Interaction) -> None:
        s, member_id = services(interaction), interaction.user.id
        template = next(x for x in self.templates if x.id == self.template_id)
        await interaction.response.send_modal(TextModal(
            interaction, t("Rename", interaction.locale), template.name, 100,
            lambda name: Templates(s.db).rename(template.id, name, member_id, self.manage_any),
            label=t("Template name", interaction.locale)))

    async def _delete(self, interaction: discord.Interaction) -> None:
        s, template_id = services(interaction), self.template_id

        async def delete() -> bool:
            await Templates(s.db).delete(template_id, interaction.user.id, self.manage_any)
            return True

        if await guarded(interaction, delete, slow=False):
            self.templates = [x for x in self.templates if x.id != template_id]
            await finish(interaction, t("Template deleted.", interaction.locale))


class SlotTools(discord.ui.View):
    """Squad, then slot (selects take 25 options), then lock / unlock, assign a member, or remove the holder."""

    def __init__(self, interaction: discord.Interaction, event: Any, board: Any) -> None:
        super().__init__(timeout=TIMEOUT)
        self.event, self.board, self.slot_id, locale = event, board, None, interaction.locale
        squads = discord.ui.Select(placeholder=t("Squad", locale), options=[
            discord.SelectOption(label=squad.name[:100], value=str(squad.id)) for squad, _ in board.squads[:25]])
        squads.callback = lambda i, s=squads: self._squad(i, int(s.values[0]))
        self.add_item(squads)

    async def _squad(self, interaction: discord.Interaction, squad_id: int) -> None:
        locale = interaction.locale
        slots = next(slots for squad, slots in self.board.squads if squad.id == squad_id)
        for item in list(self.children)[1:]:
            self.remove_item(item)
        pick = discord.ui.Select(placeholder=t("Slot", locale), options=[
            discord.SelectOption(label=f"{n + 1}. {slot.name}"[:100], value=str(slot.id),
                                 description=("⚠ " if slot.flagged else "") + ("🔒 " if slot.locked else "") +
                                 (t("taken", locale) if slot.holder_id else t("free", locale)))
            for n, slot in enumerate(slots[:25])])
        pick.callback = lambda i, s=pick: self._slot(i, int(s.values[0]))
        self.add_item(pick)
        await interaction.response.edit_message(view=self)

    async def _slot(self, interaction: discord.Interaction, slot_id: int) -> None:
        self.slot_id, locale = slot_id, interaction.locale
        for item in list(self.children)[2:]:
            self.remove_item(item)
        for action, label in (("lock", t("Lock", locale)), ("unlock", t("Unlock", locale)), ("remove", t("Remove holder", locale))):
            button = discord.ui.Button(label=label, row=2)
            button.callback = lambda i, a=action: self._act(i, a)
            self.add_item(button)
        assign = discord.ui.UserSelect(placeholder=t("Assign a member (locked)", locale), row=3)
        assign.callback = lambda i, s=assign: self._act(i, "assign", s.values[0].id)
        self.add_item(assign)
        await interaction.response.edit_message(view=self)

    async def _act(self, interaction: discord.Interaction, action: str, member_id: int | None = None) -> None:
        s = services(interaction)

        async def save() -> bool:
            await s.events.editable(self.event.id, member_of(interaction))  # locked while waiting for approval
            if action == "lock":
                await s.signups.lock(self.slot_id, True)
            elif action == "unlock":
                await s.signups.lock(self.slot_id, False)
            elif action == "remove":
                await s.signups.remove(self.slot_id)
            else:
                await s.signups.assign(self.slot_id, member_id)
            return True

        if await guarded(interaction, save):
            await reply(interaction, t("Saved.", interaction.locale))


# ---------------------------------------------------------------------------- the server profile

async def profile_panel(interaction: discord.Interaction, event: Any) -> tuple[str, discord.ui.View]:
    """The mission, its report and warnings, the settings and the build state (docs/PLAN.md, *Supervisor: event
    profile*); *Settings*, *Build profile*, *Keep*."""
    from app.profiles.words import state, warning

    s, locale = services(interaction), interaction.locale
    profiles = s.profiles
    report = event.mission_report or {}
    lines = [f"**{t('Server profile', locale)}** · {event.title}"]
    if event.mission_name:
        lines.append(t("Mission: {name} · {title} by {author}, {terrain}, {players} playable, status {status}.", locale,
                       name=event.mission_name, title=report.get("title") or "?", author=report.get("author") or "?",
                       terrain=report.get("terrain") or "?", players=report.get("playable") or "?",
                       status=report.get("status") or "?"))
    else:
        lines.append(t("No mission yet: upload it with /event mission file, or pick one with /event mission on_server.",
                       locale))
    hostname, players = await profiles.effective(event)
    lines.append(t("Hostname: {hostname} · max players: {players} · signatures: {signatures}", locale,
                   hostname=hostname, players=players or "?",
                   signatures=t("on", locale) if event.verify_signatures else t("off", locale)))
    lines.append(t("Profile: {state}", locale, state=state(event, locale)))
    if event.profile_keep:
        lines.append(t("Kept after the event.", locale))
    lines += [warning(code, detail, locale) for code, detail in await profiles.warnings(event)]
    return "\n".join(lines)[:1900], ProfileTools(interaction, event)


class ProfileTools(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, event: Any) -> None:
        super().__init__(timeout=TIMEOUT)
        self.event, locale = event, interaction.locale
        for action, label, style, disabled in (
                ("settings", t("Settings", locale), discord.ButtonStyle.secondary, False),
                ("build", t("Build profile", locale), discord.ButtonStyle.success, not event.mission_name),
                ("keep", t("Keep after the event: on", locale) if event.profile_keep else
                 t("Keep after the event: off", locale), discord.ButtonStyle.secondary, False)):
            button = discord.ui.Button(label=label, style=style, disabled=disabled)
            button.callback = lambda i, a=action: getattr(self, f"_{a}")(i)
            self.add_item(button)

    async def _settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ProfileSettings(interaction, self.event))

    async def _build(self, interaction: discord.Interaction) -> None:
        from app.profiles.service import ProfileError
        from app.profiles.words import problem, state

        s, locale = services(interaction), interaction.locale
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            event = await s.profiles.build(self.event.id, member_of(interaction))
        except ProfileError as exc:
            await reply(interaction, problem(exc.problem, locale, exc.message))
            return
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, locale, exc.missing))
            return
        await reply(interaction, t("Profile: {state}", locale, state=state(event, locale)))

    async def _keep(self, interaction: discord.Interaction) -> None:
        from app.profiles.service import ProfileError
        from app.profiles.words import problem

        s, locale = services(interaction), interaction.locale
        keep = not self.event.profile_keep
        try:
            await s.profiles.set_keep(self.event.id, member_of(interaction), keep)
        except ProfileError as exc:
            await reply(interaction, problem(exc.problem, locale, exc.message))
            return
        await reply(interaction, t("The profile is kept after the event.", locale) if keep else
                    t("The profile is deleted some days after the event.", locale))


class ProfileSettings(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, event: Any) -> None:
        locale = interaction.locale
        super().__init__(title=t("Server profile", locale)[:45], timeout=TIMEOUT)
        self.event = event
        self.hostname = discord.ui.TextInput(label=t("Hostname (empty: the default)", locale)[:45], required=False,
                                             max_length=200, default=event.hostname)
        self.players = discord.ui.TextInput(label=t("Max players (empty: the default)", locale)[:45], required=False,
                                            max_length=3, default=str(event.max_players) if event.max_players else None)
        self.signatures = discord.ui.TextInput(label=t("Signatures (on / off)", locale)[:45], max_length=5,
                                               default=t("on", locale) if event.verify_signatures else t("off", locale))
        for item in (self.hostname, self.players, self.signatures):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        from app.profiles.service import ProfileError
        from app.profiles.words import problem

        s, locale = services(interaction), interaction.locale
        off = self.signatures.value.strip().casefold() in ("off", "no", "0", t("off", locale).casefold())
        try:
            await s.profiles.set_settings(self.event.id, member_of(interaction), self.hostname.value,
                                          self.players.value, not off)
        except ProfileError as exc:
            await reply(interaction, problem(exc.problem, locale, exc.message))
            return
        except EventError as exc:
            await reply(interaction, event_problem(exc.problem, locale, exc.missing))
            return
        await reply(interaction, t("Saved.", locale))
