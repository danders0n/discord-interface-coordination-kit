"""Creating and organising events on Discord (docs/PLAN.md, *Creating an event*, *Organiser panel*).

- *Create event* in the header channel opens the **wizard**: kind, mission type, climate and the other groups in
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

from app.discord_util import reply, services
from app.events import times as event_times
from app.events.groups import GroupError, Groups
from app.events.service import KINDS, Draft, EventError, Problem
from app.events.words import event_problem, template_problem, time_problem
from app.i18n import t
from app.orbat.parser import OrbatError, ParsedSlot, parse, render
from app.orbat.templates import TemplateError, Templates
from app.permissions import Perm
from app.signup.service import Member

log = logging.getLogger(__name__)
LIVE: set[str] = set()  # the wizards open in this run (their custom ids start with wizard:<token>)
TIMEOUT = 15 * 60


def member_of(interaction: discord.Interaction) -> Member:
    return Member(interaction.user.id, frozenset(role.id for role in getattr(interaction.user, "roles", ())))


async def guarded(interaction: discord.Interaction, action: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``action``; a refusal from the services is answered in the member's words."""
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
    except GroupError:
        await reply(interaction, t("Give the group an acronym of up to 10 letters or digits and a name.",
                                   interaction.locale))
    return None


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
    """Kind, mission type, climate and other groups: the wizard's first step, and the panel's *Type and groups*."""

    def __init__(self, interaction: discord.Interaction, groups: list, *, event: Any = None) -> None:
        super().__init__(timeout=TIMEOUT)
        self.token = secrets.token_hex(6)
        LIVE.add(self.token)
        self.locale, self.event, self.groups = interaction.locale, event, groups
        s = services(interaction)
        self.member = member_of(interaction)
        may_guest = s.events.access(self.member).publishes_without_approval
        self.kind = event.kind if event else None
        self.mission_type = event.mission_type if event else None
        self.climate = event.climate_role_id if event else None
        self.host = event.host_group_id if event else None
        self.guests: list[int] = []
        labels = {"internal": t("Internal", self.locale), "guest": t("Guest", self.locale),
                  "hosted": t("Hosted", self.locale), "zeus": t("Zeus", self.locale)}
        if event is None or event.status == "draft":
            self.add_item(self._select("kind", t("Kind", self.locale),
                                       [(labels[k], k) for k in KINDS if k != "guest" or may_guest], self.kind))
        self.add_item(self._select("type", t("Mission type", self.locale),
                                   [(m, m) for m in s.settings.events.mission_types], self.mission_type))
        climates = s.ids.climates
        if climates:
            self.add_item(self._select("climate", t("Climate", self.locale),
                                       [(name, str(role)) for role, name in climates.items()],
                                       str(self.climate) if self.climate else None))
        self._group_items()

    def _select(self, part: str, placeholder: str, options: list[tuple[str, str]], current: str | None,
                many: bool = False) -> discord.ui.Select:
        select = discord.ui.Select(custom_id=f"wizard:{self.token}:{part}", placeholder=placeholder,
                                   min_values=0 if many else 1, max_values=min(len(options), 25) if many else 1,
                                   options=[discord.SelectOption(label=label[:100], value=value,
                                                                 default=value == current) for label, value in options[:25]])
        select.callback = lambda interaction, s=select, p=part: self._chosen(interaction, p, s.values)
        return select

    def _group_items(self) -> None:
        for item in [i for i in self.children if getattr(i, "custom_id", "").split(":")[-1] in ("groups", "newgroup", "go")]:
            self.remove_item(item)
        if self.kind in ("guest", "hosted") and self.groups:
            options = [(f"{g.acronym} · {g.name}", str(g.id)) for g in self.groups]
            label = t("Host group", self.locale) if self.kind == "guest" else t("Invited groups", self.locale)
            current = str(self.host) if self.kind == "guest" and self.host else None
            self.add_item(self._select("groups", label, options, current, many=self.kind == "hosted"))
        if self.kind in ("guest", "hosted"):
            button = discord.ui.Button(custom_id=f"wizard:{self.token}:newgroup", label=t("New group", self.locale))
            button.callback = self._new_group
            self.add_item(button)
        go = discord.ui.Button(custom_id=f"wizard:{self.token}:go", style=discord.ButtonStyle.primary,
                               label=t("Continue", self.locale) if self.event is None else t("Save", self.locale))
        go.callback = self._go
        self.add_item(go)

    async def _chosen(self, interaction: discord.Interaction, part: str, values: list[str]) -> None:
        if part == "kind":
            self.kind = values[0]
            self._group_items()
        elif part == "type":
            self.mission_type = values[0]
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

    async def _go(self, interaction: discord.Interaction) -> None:
        if not self.kind or not self.mission_type:
            await reply(interaction, t("Pick the kind and the mission type first.", self.locale))
            return
        if self.kind == "guest" and self.host is None:
            await reply(interaction, event_problem(Problem.NEEDS_HOST, self.locale))
            return
        if self.event is None:
            await interaction.response.send_modal(DetailsModal(interaction, choices=self))
            return
        s = services(interaction)
        changes: dict[str, Any] = {"mission_type": self.mission_type, "climate_role_id": self.climate}
        if self.event.status == "draft":
            changes["kind"] = self.kind
        if self.kind == "guest":
            changes["host_group_id"] = self.host
        if self.kind == "hosted" and self.guests:
            changes["guest_group_ids"] = self.guests
        await guarded(interaction, lambda: s.events.update(self.event.id, member_of(interaction), **changes))
        if not interaction.response.is_done():  # (a refusal was answered already)
            await interaction.response.edit_message(content=t("Saved.", self.locale), view=None)

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
        group = await guarded(interaction, lambda: groups.add(self.acronym.value, self.name.value, interaction.user.id))
        if group is None:
            return
        self.choices.groups = await groups.all()
        if self.choices.kind == "guest":
            self.choices.host = group.id
        else:
            self.choices.guests = sorted({*self.choices.guests, group.id})
        self.choices._group_items()
        await interaction.response.edit_message(view=self.choices)


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
            event = await s.events.create(Draft(c.kind, self.name.value, c.mission_type, parsed, c.climate,
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
        view = Choices(interaction, await Groups(s.db).all())
        await reply(interaction, t("New event: pick the kind, the mission type and the climate, then Continue.",
                                   interaction.locale), view=view)


# ---------------------------------------------------------------------------- the organiser panel

ACTIONS = ("details", "choices", "briefing", "orbat", "join", "signup", "limit", "organisers", "publish", "withdraw",
           "server", "retry", "endres", "cancel", "end")


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
    event = await guarded(interaction, lambda: s.events.get(event_id))
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
    view = discord.ui.View(timeout=None)
    shown = ["details", "choices", "briefing"]
    shown += ["join"] if event.kind == "guest" else ["limit"] if event.kind == "zeus" else ["orbat"]
    shown += ["signup", "organisers"]
    if event.status == "draft":
        shown.append("publish")
    if event.status == "pending":
        shown.append("withdraw")
    if event.kind == "zeus" and not access.publishes_without_approval and (booked is None or booked.state != "booked"):
        shown.append("server")
    if booked is not None and booked.state in ("failed", "retrying"):
        shown.append("retry")
    if booked is not None and booked.state == "booked" and s.events.clock() >= event.assembly_at:
        shown.append("endres")
    shown.append("end" if s.events.clock() >= event.start_at else "cancel")
    publish_label = t("Publish", locale) if access.publishes_without_approval or event.kind == "zeus" else t("Send for approval", locale)
    labels = {"details": t("Details", locale), "choices": t("Type and groups", locale), "briefing": t("Briefing", locale),
              "orbat": t("ORBAT", locale), "join": t("How to join", locale), "signup": t("Sign-up start", locale),
              "limit": t("Sign-up limit", locale), "organisers": t("Co-organisers", locale), "publish": publish_label,
              "withdraw": t("Withdraw request", locale), "server": t("Request the server", locale),
              "retry": t("Retry reservation", locale), "endres": t("End reservation", locale),
              "cancel": t("Cancel event", locale), "end": t("End event", locale)}
    for action in shown:
        style = discord.ButtonStyle.danger if action in ("cancel", "end", "endres") else \
            discord.ButtonStyle.success if action == "publish" else discord.ButtonStyle.secondary
        view.add_item(PanelAction(event.id, action, labels[action], style))
    return "\n".join(lines), view


class PanelAction(discord.ui.DynamicItem[discord.ui.Button], template=r"event:act:(?P<event>\d+):(?P<action>[a-z]+)"):
    def __init__(self, event_id: int, action: str, label: str = "", style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=f"event:act:{event_id}:{action}"))
        self.event_id, self.action = event_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> PanelAction:
        return cls(int(match["event"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        s, locale = services(interaction), interaction.locale
        event = await guarded(interaction, lambda: s.events.get(self.event_id))
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
        await reply(interaction, t("Type and groups", interaction.locale),
                    view=Choices(interaction, await Groups(services(interaction).db).all(), event=event))

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
        state = await guarded(interaction, lambda: services(interaction).events.publish(event.id, member))
        if state == "published":
            await reply(interaction, t("Published.", interaction.locale))
        elif state == "pending":
            await reply(interaction, t("Sent to the moderators for approval.", interaction.locale))

    async def _withdraw(self, interaction, event, member) -> None:
        if await guarded(interaction, lambda: services(interaction).events.withdraw(event.id, member)) is None and \
                not interaction.response.is_done():
            await reply(interaction, t("The request is withdrawn; the event is a draft again.", interaction.locale))

    async def _server(self, interaction, event, member) -> None:
        if await guarded(interaction, lambda: services(interaction).events.request_server(event.id, member)):
            await reply(interaction, t("The server is requested; a moderator decides.", interaction.locale))

    async def _retry(self, interaction, event, member) -> None:
        row = await services(interaction).reservations.retry(event.id)
        await reply(interaction, t("Server reserved.", interaction.locale) if row and row.state == "booked" else
                    t("No reservation: {reason}", interaction.locale, reason=row.error if row else "—"))

    async def _endres(self, interaction, event, member) -> None:
        ended = await services(interaction).reservations.end(event.id)
        await reply(interaction, t("The reservation has ended.", interaction.locale) if ended else
                    t("The reservation isn't under way.", interaction.locale))

    async def _cancel(self, interaction, event, member) -> None:
        await interaction.response.send_modal(TextModal(
            interaction, t("Cancel event", interaction.locale), "", 500,
            lambda reason: services(interaction).events.cancel(event.id, member, reason),
            label=t("Reason", interaction.locale), done=t("Cancelled.", interaction.locale)))

    async def _end(self, interaction, event, member) -> None:
        if await guarded(interaction, lambda: services(interaction).events.end(event.id, member)) is None and \
                not interaction.response.is_done():
            await reply(interaction, t("The event has ended.", interaction.locale))


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
        if await guarded(interaction, lambda: self._save(self.text.value)) is not None:
            await reply(interaction, self.done or t("Saved.", interaction.locale))

    async def _save(self, text: str) -> bool:
        await self.save(text)
        return True


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
    """Paste or edit the ORBAT, take a template, save one, and the organisers' slot tools."""

    def __init__(self, interaction: discord.Interaction, event: Any) -> None:
        super().__init__(timeout=TIMEOUT)
        self.event, locale = event, interaction.locale
        for action, label in (("paste", t("Write or paste", locale)), ("template", t("From a template", locale)),
                              ("save", t("Save as template", locale)), ("slots", t("Lock, assign, remove", locale))):
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
            current = await s.events.get(event.id)
            if not await s.events.can_organise(current, member) or current.status not in ("draft", "published"):
                raise EventError(Problem.NOT_DRAFT if current.status != "pending" else Problem.PENDING)
            displaced = await s.signups.set_orbat(event.id, squads)
            if displaced:
                log.info("ORBAT of event %s edited: %s moved to the reserve list", event.id, displaced)

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
                await s.signups.set_orbat(self.event.id, parse(template.text, roles))
                return True

            if await guarded(i, save):
                await reply(i, t("Saved.", i.locale))

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

    async def _slots(self, interaction: discord.Interaction) -> None:
        board = await services(interaction).signups.board(self.event.id)
        if not board.squads:
            await reply(interaction, t("There is no ORBAT yet.", interaction.locale))
            return
        await reply(interaction, t("Pick the squad, then the slot.", interaction.locale),
                    view=SlotTools(interaction, self.event, board))


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
                                 description=("🔒 " if slot.locked else "") + (t("taken", locale) if slot.holder_id else t("free", locale)))
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
        signups = services(interaction).signups
        if action == "lock":
            await signups.lock(self.slot_id, True)
        elif action == "unlock":
            await signups.lock(self.slot_id, False)
        elif action == "remove":
            await signups.remove(self.slot_id)
        else:
            await signups.assign(self.slot_id, member_id)
        await reply(interaction, t("Saved.", interaction.locale))
