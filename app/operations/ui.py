"""Creating and organising operations on Discord (docs/PLAN-front-end.md, *Commands and buttons*; how they look:
docs/PLAN.md, *Creating an event*, *Organiser panel*).

- *Create operation* on the header opens the **wizard**: kind, climate and the other groups in selects, then title,
  date and times in a modal; its last step makes the draft in the supervisor (act-as), and the feed brings its
  channel. The wizard lives in memory; after a restart its buttons say it expired (:func:`expired`).
- *Organiser panel* under an operation's info opens the **panel** privately: a button per section the supervisor's
  ``my.actions`` allows (:class:`PanelAction`, the operation id in ``custom_id``, so it outlives a restart). Each
  press reads the operation in the member's name; a write sends ``If-Match`` with the revision read, and a change
  made meanwhile (409 ``stale``) is answered with *Reopen* for that section. Nothing is merged by the bot.

Everything here gathers input and words answers: the supervisor decides (its refusals reach the error handlers,
which word them)."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import discord

from app.channel import render
from app.discord_util import finish, reply, services, thinking
from app.i18n import t
from app.operations import times
from app.operations.words import missing, time_problem
from app.signup.ui import act
from app.supervisor_client import SupervisorError

log = logging.getLogger(__name__)
LIVE: set[str] = set()  # the wizards open in this run (their custom ids start with wizard:<token>)
TIMEOUT = 15 * 60
PUBLISH = "operations.publish"


# ---------------------------------------------------------------------------- reading and writing

async def read(interaction: discord.Interaction, op_id: int) -> dict[str, Any]:
    """The operation as the member sees it (``my``: ``organiser``, ``actions``, their place)."""
    client = services(interaction).supervisor
    return await act(interaction, lambda me: client.operation(op_id, act_as=me))


async def write(interaction: discord.Interaction, op_id: int, section: str,
                call: Callable[[int], Awaitable[Any]]) -> Any | None:
    """An act-as write; a stale one (the operation changed since it was read) is answered with *Reopen* for the
    section and gives None. Other refusals go up to the error handler, which words them."""
    try:
        return await act(interaction, call)
    except SupervisorError as exc:
        if exc.code != "stale":
            raise
    locale = interaction.locale
    view = discord.ui.View(timeout=None)
    view.add_item(PanelAction(op_id, section, t("Reopen", locale)))
    await reply(interaction, t("The operation changed meanwhile; open it again to see the change.", locale), view=view)
    return None


def local(value: str | None, tz: Any) -> datetime | None:
    found = render.when({"x": value}, "x")
    return found.astimezone(tz) if found is not None else None


def duration_text(minutes: int) -> str:
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes % 60 else f"{minutes // 60}h"


# ---------------------------------------------------------------------------- the wizard and its choices

async def expired(interaction: discord.Interaction) -> bool:
    """A wizard's control from before a restart: say so. True when it answered."""
    data = interaction.data or {}
    custom_id = data.get("custom_id", "") if interaction.type == discord.InteractionType.component else ""
    if custom_id.startswith("wizard:") and custom_id.split(":")[1] not in LIVE:
        await reply(interaction, t("This wizard has expired (the bot restarted). Press Create operation again.",
                                   interaction.locale))
        return True
    return False


class Choices(discord.ui.View):
    """Kind, climate and other groups: the wizard's first step, and the panel's *Type and groups* (``op``)."""

    def __init__(self, interaction: discord.Interaction, meta: dict[str, Any], groups: list[dict[str, Any]], *,
                 op: dict[str, Any] | None = None, may_guest: bool = True) -> None:
        super().__init__(timeout=TIMEOUT)
        self.token = secrets.token_hex(6)
        LIVE.add(self.token)
        self.locale, self.op, self.groups, self.meta = interaction.locale, op, groups, meta
        self.kind = op["kind"] if op else None
        self.climate = (op.get("climate") or None) if op else None
        self.host = (op.get("host_group") or {}).get("id") if op else None
        self.guests: list[int] = [g["id"] for g in (op.get("groups") or [])] if op else []
        kinds = [(k.get("name") or k["id"], k["id"]) for k in meta.get("kinds") or []
                 if k["id"] != "guest" or may_guest or (op and op["kind"] == "guest")]
        if op is None or op["status"] == "draft":
            self.add_item(self._select("kind", t("Kind", self.locale), kinds, self.kind))
        climates = [(c["name"], c["name"]) for c in meta.get("climates") or []]
        if climates:
            self.add_item(self._select("climate", t("Climate", self.locale), climates, self.climate))
        self._group_items()

    def _select(self, part: str, placeholder: str, options: list[tuple[str, str]], current: Any,
                many: bool = False) -> discord.ui.Select:
        chosen = {str(c) for c in current} if isinstance(current, (set, list)) else {str(current)}
        select = discord.ui.Select(custom_id=f"wizard:{self.token}:{part}", placeholder=placeholder[:150],
                                   min_values=0 if many else 1, max_values=min(len(options), 25) if many else 1,
                                   options=[discord.SelectOption(label=str(label)[:100], value=str(value),
                                                                 default=str(value) in chosen)
                                            for label, value in options[:25]])
        select.callback = lambda interaction, s=select, p=part: self._chosen(interaction, p, s.values)
        return select

    def _group_items(self) -> None:
        for item in [i for i in self.children if getattr(i, "custom_id", "").split(":")[-1] in ("groups", "newgroup", "go")]:
            self.remove_item(item)
        if self.kind in ("guest", "hosted") and self.groups:
            options = [(f"{g['acronym']} · {g['name']}", g["id"]) for g in self.groups]
            label = t("Host group", self.locale) if self.kind == "guest" else \
                t("Invited groups: any number; New group adds another", self.locale)
            current = self.host if self.kind == "guest" else set(self.guests)
            self.add_item(self._select("groups", label, options, current, many=self.kind == "hosted"))
        if self.kind in ("guest", "hosted"):
            button = discord.ui.Button(custom_id=f"wizard:{self.token}:newgroup", label=t("New group", self.locale))
            button.callback = self._new_group
            self.add_item(button)
        go = discord.ui.Button(custom_id=f"wizard:{self.token}:go", style=discord.ButtonStyle.primary,
                               label=t("Continue", self.locale) if self.op is None else t("Save", self.locale))
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
            self.climate = values[0] if values else None
        elif part == "groups":
            if self.kind == "guest":
                self.host = int(values[0]) if values else None
            else:
                self.guests = [int(v) for v in values]
        await interaction.response.edit_message(view=self)

    async def _new_group(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NewGroup(self))

    def fields(self) -> dict[str, Any]:
        """What the choices set, as the supervisor takes it."""
        found: dict[str, Any] = {"climate": self.climate}
        if self.op is None or self.op["status"] == "draft":
            found["kind"] = self.kind
        if self.kind == "guest":
            found["host_group_id"] = self.host
        if self.kind == "hosted":
            found["group_ids"] = self.guests
        return found

    async def _go(self, interaction: discord.Interaction) -> None:
        if not self.kind:
            await reply(interaction, t("Pick the kind first.", self.locale))
            return
        if self.kind == "guest" and self.host is None:
            await reply(interaction, t("A guest operation needs its host group.", self.locale))
            return
        if self.op is None:
            await interaction.response.send_modal(DetailsModal(interaction, choices=self))
            return
        await interaction.response.defer()  # (finish() edits these choices)
        client, op = services(interaction).supervisor, self.op
        fields = self.fields()
        if await write(interaction, op["id"], "choices",
                       lambda me: client.update_operation(me, op["id"], fields, op["revision"])) is not None:
            self.stop()
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
        client, acronym, name = services(interaction).supervisor, self.acronym.value.strip(), self.name.value.strip()
        group = await act(interaction, lambda me: client.add_group(me, acronym, name))
        self.choices.groups = [*self.choices.groups, group]
        if self.choices.kind == "guest":
            self.choices.host = group["id"]
        else:
            self.choices.guests = sorted({*self.choices.guests, group["id"]})
        self.choices._group_items()
        await interaction.response.edit_message(view=self.choices)


class DetailsModal(discord.ui.Modal):
    """Title, date and times: the wizard's last step, and the panel's *Details* (``op``)."""

    def __init__(self, interaction: discord.Interaction, *, choices: Choices | None = None,
                 op: dict[str, Any] | None = None) -> None:
        locale = interaction.locale
        super().__init__(title=t("Operation details", locale)[:45], timeout=TIMEOUT)
        self.choices, self.op = choices, op
        s = services(interaction)
        tz = s.settings.tz
        assembly = local(op["assembly_at"], tz) if op else None
        start = local(op["start_at"], tz) if op else None
        self.name = discord.ui.TextInput(label=t("Title", locale), max_length=100, default=op["title"] if op else None)
        self.date = discord.ui.TextInput(label=t("Date (dd.mm)", locale), max_length=10,
                                         default=f"{assembly:%d.%m.%Y}" if assembly else None)
        self.assembly = discord.ui.TextInput(label=t("Assembly (HH:MM)", locale), max_length=5,
                                             default=f"{assembly:%H:%M}" if assembly else None)
        self.start = discord.ui.TextInput(label=t("Start (HH:MM)", locale), max_length=5,
                                          default=f"{start:%H:%M}" if start else None)
        minutes = int(op["duration_minutes"]) if op else s.settings.times.default_duration_minutes
        self.duration = discord.ui.TextInput(label=t("Duration (e.g. 3h, 2h30)", locale), max_length=10,
                                             default=duration_text(minutes))
        for item in (self.name, self.date, self.assembly, self.start, self.duration):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s, locale = services(interaction), interaction.locale
        try:
            parsed = times.parse(self.date.value, self.assembly.value, self.start.value, self.duration.value,
                                 s.scheduler.clock(), s.settings.tz, int(s.channels.meta.get("max_days_ahead") or 120))
        except times.TimeError as exc:
            await reply(interaction, time_problem(exc.problem, locale))
            return
        await thinking(interaction)
        when = {"assembly_at": parsed.assembly_at.isoformat(), "start_at": parsed.start_at.isoformat(),
                "duration_minutes": parsed.duration_minutes}
        if self.op is None:
            await self._create(interaction, when)
            return
        op = self.op
        fields: dict[str, Any] = {"title": self.name.value.strip()} if self.name.value.strip() != op["title"] else {}
        if (render.when(op, "assembly_at"), render.when(op, "start_at"), op["duration_minutes"]) != (
                parsed.assembly_at, parsed.start_at, parsed.duration_minutes):
            fields |= when  # (only when they moved: unchanged times of an approved operation would be a proposal)
        if not fields:
            await reply(interaction, t("Nothing changed.", locale))
            return
        answer = await write(interaction, op["id"], "details",
                             lambda me: s.supervisor.update_operation(me, op["id"], fields, op["revision"]))
        if answer is None:
            return
        if answer.get("proposal"):
            await reply(interaction, t("New times sent to the moderators; nothing changes until they approve.", locale))
        else:
            await reply(interaction, t("Saved.", locale))

    async def _create(self, interaction: discord.Interaction, when: dict[str, Any]) -> None:
        s, choices = services(interaction), self.choices
        body = {"title": self.name.value.strip(), **when, **{k: v for k, v in choices.fields().items() if v is not None}}
        op = await act(interaction, lambda me: s.supervisor.create_operation(me, body))
        choices.stop()
        if s.feed is not None:
            s.feed.wake()  # (its channel comes with the feed, drawn like one made in the app)
        await reply(interaction, t("Your draft {key} is made; its channel appears in a moment. Finish it there with "
                                   "Organiser panel.", interaction.locale, key=f"OP-{op['id']}"))


class CreateOperation(discord.ui.DynamicItem[discord.ui.Button], template=r"operation:create"):
    """*Create operation* on the header."""

    def __init__(self, label: str = "Create operation") -> None:
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.success, custom_id="operation:create"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: Any) -> CreateOperation:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        s, locale = services(interaction), interaction.locale
        me = await act(interaction, lambda who: s.supervisor.me(act_as=who))
        permissions = set(me.get("permissions") or [])
        if "operations.create" not in permissions and PUBLISH not in permissions:
            await reply(interaction, t("Creating operations is for members.", locale))
            return
        for name, count in s.channels.crowded():
            if count >= 50:  # Discord's limit per category
                await reply(interaction, t("The category {category} is full (50 channels): staff must make room "
                                           "before a new operation can be made.", locale, category=name))
                return
        groups = await act(interaction, lambda who: s.supervisor.groups(who))
        view = Choices(interaction, s.channels.meta, groups, may_guest=PUBLISH in permissions)
        await reply(interaction, t("New operation: pick the kind and the climate, then Continue.", locale), view=view)


def header_view(language: Any) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(CreateOperation(t("Create operation", language)))
    return view


# ---------------------------------------------------------------------------- the organiser panel

class PanelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"operation:panel:(?P<op>\d+)"):
    """*Organiser panel* under the operation's info."""

    def __init__(self, op_id: int, label: str = "Organiser panel") -> None:
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, emoji="🛠️",
                                           custom_id=f"operation:panel:{op_id}"))
        self.op_id = op_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> PanelButton:
        return cls(int(match["op"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        await thinking(interaction)
        try:
            op = await read(interaction, self.op_id)
        except SupervisorError as exc:
            if exc.code != "not_found":  # (a draft is hidden from all but its organisers and staff)
                raise
            op = {"kind": "", "my": {}}
        if not panel_sections(op):
            await reply(interaction, t("The panel is for this operation's organisers.", interaction.locale))
            return
        content, view = panel(op, interaction.locale)
        await reply(interaction, content, view=view)


def panel_view(op_id: int, language: Any) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(PanelButton(op_id, t("Organiser panel", language)))
    return view


def panel_sections(op: dict[str, Any]) -> list[str]:
    """The sections the member's ``actions`` allow, in the panel's order."""
    actions = set((op.get("my") or {}).get("actions") or [])
    kind, shown = op["kind"], []
    if "edit" in actions:
        shown += ["details", "choices"]
    if "opord" in actions:
        shown.append("opord")
    if "orbat" in actions or ("slots" in actions and kind not in render.COMING):
        shown.append("orbat")
    if "edit" in actions:
        shown += ["signup"] + (["limit"] if kind in render.COMING else []) + (["join"] if kind == "guest" else [])
        shown.append("organisers")
        if kind != "guest":
            shown.append("hold")
        if kind in ("internal", "hosted"):
            shown.append("autoend")
    shown += [a for a in ("publish", "submit", "withdraw", "copy", "delete", "cancel") if a in actions]
    if "end_operation" in actions:
        shown.append("end")
    return shown


def _status(status: str, locale: Any) -> str:
    return {"draft": t("Draft", locale), "pending": t("Waiting for approval", locale),
            "published": t("Published", locale), "ended": t("Ended", locale),
            "cancelled": t("Cancelled", locale)}.get(status, status)


def warning_words(warning: dict[str, Any], locale: Any) -> str:
    detail = str(warning.get("detail") or "")
    return {"missing_dlc": t("The mission needs Creator DLC the preset lacks: {detail}", locale, detail=detail),
            "no_preset": t("The mission needs mods, but there is no preset: {detail}", locale, detail=detail),
            "missing_mods": t("The preset lacks mods the mission needs: {detail}", locale, detail=detail),
            "mission_error": t("The mission has errors: {detail}", locale, detail=detail)}.get(
        str(warning.get("code")), detail)


def panel(op: dict[str, Any], locale: Any) -> tuple[str, discord.ui.View]:
    """The panel's text (state, what is missing, warnings, waiting requests, flagged slots) and its buttons."""
    lines = [f"**{render.plain(op['title'])}** · OP-{op['id']} · {_status(op['status'], locale)}"]
    actions = set((op.get("my") or {}).get("actions") or [])
    if op["status"] == "draft" and actions & {"publish", "submit"}:
        lacking = [f"opord.{p}" for p in ("situation", "mission") if not ((op.get("opord") or {}).get(p) or "").strip()]
        if op["kind"] == "guest" and not (op.get("how_to_join") or "").strip():
            lacking.append("how_to_join")
        lines.append(missing(lacking, locale) if lacking else t("Ready to publish.", locale))
    for request in op.get("requests") or []:
        if request.get("state") == "pending" and request.get("kind") == "times":
            lines.append(t("New times wait for a moderator.", locale))
        elif request.get("state") == "pending" and request.get("kind") == "hold":
            lines.append(t("The host is requested; a moderator decides.", locale))
    lines += ["⚠ " + warning_words(w, locale) for w in op.get("warnings") or []]
    error = (op.get("profile") or {}).get("error")
    if error:
        lines.append("⚠ " + t("The server profile couldn't be built: {error}", locale, error=str(error)[:300]))
    for squad in op.get("orbat") or []:
        for slot in squad.get("slots") or []:
            if slot.get("flagged") and slot.get("holder"):
                lines.append(t("⚠ {slot} ({squad}): {member} lost the role it needs.", locale, slot=slot["name"],
                               squad=squad["name"], member=render.person(slot["holder"])))
    if "files" in actions:
        lines.append("-# " + t("Files: /operation preset, /operation image and /operation mission in the operation's "
                               "channel. The server and the base are set in the app.", locale))
    labels = {"details": t("Details", locale), "choices": t("Type and groups", locale), "opord": t("OPORD", locale),
              "orbat": t("ORBAT", locale), "signup": t("Sign-up start", locale), "limit": t("Sign-up limit", locale),
              "join": t("How to join", locale), "organisers": t("Co-organisers", locale),
              "hold": {"on": t("Hold the host: on", locale), "requested": t("Hold the host: requested", locale)}.get(
                  op.get("hold"), t("Hold the host: off", locale)),
              "autoend": t("Don't end automatically: on", locale) if op.get("dont_end_automatically") else
              t("Don't end automatically: off", locale),
              "publish": t("Publish", locale), "submit": t("Send for approval", locale),
              "withdraw": t("Withdraw request", locale), "copy": t("Copy operation", locale),
              "delete": t("Delete draft", locale), "cancel": t("Cancel operation", locale),
              "end": t("End operation", locale)}
    view = discord.ui.View(timeout=None)
    for section in panel_sections(op):
        style = discord.ButtonStyle.danger if section in ("cancel", "delete", "end") else \
            discord.ButtonStyle.success if section in ("publish", "submit") else discord.ButtonStyle.secondary
        view.add_item(PanelAction(op["id"], section, labels[section], style))
    return "\n".join(lines)[:2000], view


class PanelAction(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"operation:act:(?P<op>\d+):(?P<action>[a-z]+)"):
    def __init__(self, op_id: int, action: str, label: str = "",
                 style: discord.ButtonStyle = discord.ButtonStyle.secondary) -> None:
        super().__init__(discord.ui.Button(label=label or action, style=style,
                                           custom_id=f"operation:act:{op_id}:{action}"))
        self.op_id, self.action = op_id, action

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> PanelAction:
        return cls(int(match["op"]), match["action"])

    async def callback(self, interaction: discord.Interaction) -> None:
        """The section on the operation as it is now (handlers that open a modal answer with it, so no
        "thinking" first: a read on the same machine is quick)."""
        locale = interaction.locale
        op = await read(interaction, self.op_id)
        handler = getattr(self, f"_{self.action}", None)
        if handler is None:
            await reply(interaction, t("That button is from an older version; open the panel again.", locale))
            return
        if self.action not in panel_sections(op):
            await reply(interaction, t("That can't be done now; open the panel again to see what can.", locale))
            return
        await handler(interaction, op)

    # ------------------------------------------------------------ sections

    async def _details(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await interaction.response.send_modal(DetailsModal(interaction, op=op))

    async def _choices(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        s = services(interaction)
        groups = await act(interaction, lambda me: s.supervisor.groups(me))
        await reply(interaction, t("Type and groups", interaction.locale), view=Choices(
            interaction, s.channels.meta, groups, op=op))

    async def _opord(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await interaction.response.send_modal(OpordModal(interaction, op))

    async def _orbat(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        from app.operations.orbat_ui import OrbatTools  # (the ORBAT's own module; it uses this one)

        await reply(interaction, t("ORBAT", interaction.locale), view=OrbatTools(interaction, op))

    async def _signup(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await interaction.response.send_modal(SignupStartModal(interaction, op))

    async def _limit(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        client, locale = services(interaction).supervisor, interaction.locale

        async def save(modal_interaction: discord.Interaction, text: str) -> str | None:
            text = text.strip()
            if text and not text.isdigit():
                return t("Type a whole number, or leave it empty.", locale)
            fields = {"signup_limit": int(text) if text else None}
            done = await write(modal_interaction, op["id"], "limit",
                               lambda me: client.update_operation(me, op["id"], fields, op["revision"]))
            return t("Saved.", locale) if done is not None else None

        await interaction.response.send_modal(TextModal(interaction, t("Sign-up limit", locale),
                                                        str(op.get("signup_limit") or ""), 4, save, required=False,
                                                        long=False))

    async def _join(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        client, locale = services(interaction).supervisor, interaction.locale

        async def save(modal_interaction: discord.Interaction, text: str) -> str | None:
            done = await write(modal_interaction, op["id"], "join", lambda me: client.update_operation(
                me, op["id"], {"how_to_join": text.strip()}, op["revision"]))
            return t("Saved.", locale) if done is not None else None

        await interaction.response.send_modal(TextModal(interaction, t("How to join", locale),
                                                        op.get("how_to_join") or "", 1024, save, required=False))

    async def _organisers(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        names = ", ".join(render.plain(p.get("display_name") or p.get("username") or "?")
                          for p in op.get("organisers") or []) or "—"
        await reply(interaction, t("Co-organisers: {names}", interaction.locale, names=names),
                    view=Organisers(interaction, op["id"]))

    async def _hold(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await thinking(interaction)
        wanted = "off" if op.get("hold") in ("on", "requested") else "on"
        client = services(interaction).supervisor
        answer = await write(interaction, op["id"], "hold",
                             lambda me: client.update_operation(me, op["id"], {"hold": wanted}, op["revision"]))
        if answer is None:
            return
        words = {"on": t("The host is held for this operation.", interaction.locale),
                 "requested": t("The host is requested; a moderator decides.", interaction.locale)}
        await reply(interaction, words.get(answer.get("hold"), t("The host is not held for this operation.",
                                                                 interaction.locale)))

    async def _autoend(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await thinking(interaction)
        value, client = not op.get("dont_end_automatically"), services(interaction).supervisor
        if await write(interaction, op["id"], "autoend", lambda me: client.update_operation(
                me, op["id"], {"dont_end_automatically": value}, op["revision"])) is not None:
            await reply(interaction, t("A finished mission no longer ends this operation; the host's end or End "
                                       "operation does.", interaction.locale) if value else
                        t("A finished mission ends this operation again.", interaction.locale))

    async def _step(self, interaction: discord.Interaction, op: dict[str, Any], step: str, done: str) -> None:
        await thinking(interaction)
        client = services(interaction).supervisor
        await act(interaction, lambda me: client.operation_step(me, op["id"], step))
        await reply(interaction, done)

    async def _publish(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await self._step(interaction, op, "publish", t("Published.", interaction.locale))

    async def _submit(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await self._step(interaction, op, "submit", t("Sent to the moderators for approval.", interaction.locale))

    async def _withdraw(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await self._step(interaction, op, "withdraw",
                         t("The request is withdrawn; the operation is a draft again.", interaction.locale))

    async def _copy(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await thinking(interaction)
        s = services(interaction)
        made = await act(interaction, lambda me: s.supervisor.copy_operation(me, op["id"]))
        if s.feed is not None:
            s.feed.wake()
        await reply(interaction, t("Copied as {key}, a week later; its channel appears in a moment.",
                                   interaction.locale, key=f"OP-{made['id']}"))

    async def _cancel(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        client, locale = services(interaction).supervisor, interaction.locale

        async def save(modal_interaction: discord.Interaction, reason: str) -> str | None:
            await act(modal_interaction, lambda me: client.cancel_operation(me, op["id"], reason.strip()))
            return t("Cancelled.", locale)

        await interaction.response.send_modal(TextModal(interaction, t("Cancel operation", locale), "", 500, save,
                                                        label=t("Reason", locale)))

    async def _end(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        """After the start: the operation ends now and the attendance is read shortly after."""
        await thinking(interaction)
        s = services(interaction)
        await act(interaction, lambda me: s.supervisor.end_operation(me, op["id"]))
        s.staff.post(t("{member} ended {title}.", s.settings.language, member=interaction.user.mention,
                       title=render.plain(op["title"])))
        await reply(interaction, t("The operation has ended; the attendance report follows shortly.",
                                   interaction.locale))

    async def _delete(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        await reply(interaction, t("Delete the draft {title}? This can't be undone.", interaction.locale,
                                   title=render.plain(op["title"])), view=ConfirmDelete(interaction, op["id"]))


class ConfirmDelete(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, op_id: int) -> None:
        super().__init__(timeout=TIMEOUT)
        self.op_id = op_id
        button = discord.ui.Button(label=t("Delete", interaction.locale), style=discord.ButtonStyle.danger)
        button.callback = self._delete
        self.add_item(button)

    async def _delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        client = services(interaction).supervisor
        await act(interaction, lambda me: client.delete_operation(me, self.op_id))
        self.stop()
        await finish(interaction, t("Deleted.", interaction.locale))


class TextModal(discord.ui.Modal):
    """One text field; ``save(interaction, text)`` answers in words (None: it answered itself)."""

    def __init__(self, interaction: discord.Interaction, title: str, current: str, limit: int,
                 save: Callable[[discord.Interaction, str], Awaitable[str | None]], *, label: str | None = None,
                 required: bool = True, long: bool = True) -> None:
        super().__init__(title=title[:45], timeout=TIMEOUT)
        self.save = save
        self.text = discord.ui.TextInput(label=(label or title)[:45], default=current or None, max_length=limit,
                                         style=discord.TextStyle.paragraph if long else discord.TextStyle.short,
                                         required=required)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await thinking(interaction)
        answer = await self.save(interaction, self.text.value)
        if answer is not None:
            await reply(interaction, answer)


class OpordModal(discord.ui.Modal):
    """The five paragraphs, one field each (Discord's five inputs of 4000 characters)."""

    def __init__(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        locale = interaction.locale
        super().__init__(title=t("OPORD", locale)[:45], timeout=TIMEOUT)
        self.op = op
        written = op.get("opord") or {}
        self.fields: dict[str, discord.ui.TextInput] = {}
        for paragraph in render.PARAGRAPHS:
            value = written.get(paragraph)
            field = discord.ui.TextInput(label=render.heading(paragraph, locale)[:45], style=discord.TextStyle.paragraph,
                                         default=value if isinstance(value, str) and value else None, required=False,
                                         max_length=4000)
            self.fields[paragraph] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await thinking(interaction)
        client, op = services(interaction).supervisor, self.op
        written = op.get("opord") or {}
        changed = {p: f.value for p, f in self.fields.items() if f.value != (written.get(p) or "")}
        if not changed:
            await reply(interaction, t("Nothing changed.", interaction.locale))
            return
        if await write(interaction, op["id"], "opord",
                       lambda me: client.set_opord(me, op["id"], changed, op["revision"])) is not None:
            await reply(interaction, t("Saved.", interaction.locale))


class SignupStartModal(discord.ui.Modal):
    """The date and time sign-up opens; empty: at publishing."""

    def __init__(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        locale = interaction.locale
        super().__init__(title=t("Sign-up start", locale)[:45], timeout=TIMEOUT)
        self.op = op
        opens = local(op.get("signup_opens_at"), services(interaction).settings.tz)
        self.date = discord.ui.TextInput(label=t("Date (dd.mm), empty: at publishing", locale)[:45], required=False,
                                         max_length=10, default=f"{opens:%d.%m.%Y}" if opens else None)
        self.time = discord.ui.TextInput(label=t("Time (HH:MM)", locale), required=False, max_length=5,
                                         default=f"{opens:%H:%M}" if opens else "18:00")
        self.add_item(self.date)
        self.add_item(self.time)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        s, op = services(interaction), self.op
        opens = None
        if self.date.value.strip():
            try:
                day = times.parse_date(self.date.value, s.scheduler.clock(), s.settings.tz,
                                       int(s.channels.meta.get("max_days_ahead") or 120))
                clock = times.parse_time(self.time.value or "00:00")
            except times.TimeError as exc:
                await reply(interaction, time_problem(exc.problem, interaction.locale))
                return
            opens = datetime.combine(day, clock, s.settings.tz).isoformat()
        await thinking(interaction)
        if await write(interaction, op["id"], "signup", lambda me: s.supervisor.update_operation(
                me, op["id"], {"signup_opens_at": opens}, op["revision"])) is not None:
            await reply(interaction, t("Saved.", interaction.locale))


async def person_ids(interaction: discord.Interaction, members: list[Any]) -> list[int] | None:
    """The supervisor's persons for Discord members; None (answered) when one isn't on its roster."""
    links = services(interaction).links
    found = []
    for member in members:
        person = await links.person(member.id)
        if person is None:
            await reply(interaction, t("{member} is not on the app's member list yet.", interaction.locale,
                                       member=f"<@{member.id}>"))
            return None
        found.append(int(person["id"]))
    return found


class Organisers(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, op_id: int) -> None:
        super().__init__(timeout=TIMEOUT)
        self.op_id = op_id
        add = discord.ui.UserSelect(placeholder=t("Add a co-organiser", interaction.locale), max_values=5)
        add.callback = lambda i, select=add: self._change(i, list(select.values), True)
        remove = discord.ui.UserSelect(placeholder=t("Remove a co-organiser", interaction.locale), max_values=5)
        remove.callback = lambda i, select=remove: self._change(i, list(select.values), False)
        self.add_item(add)
        self.add_item(remove)

    async def _change(self, interaction: discord.Interaction, members: list[Any], add: bool) -> None:
        await thinking(interaction)
        picked = await person_ids(interaction, members)
        if picked is None:
            return
        op = await read(interaction, self.op_id)  # (the list now, and the revision it is written against)
        now = [int(p["id"]) for p in op.get("organisers") or []]
        wanted = list(dict.fromkeys(now + picked)) if add else [p for p in now if p not in picked]
        client = services(interaction).supervisor
        if await write(interaction, self.op_id, "organisers",
                       lambda me: client.set_organisers(me, self.op_id, wanted, op["revision"])) is not None:
            await reply(interaction, t("Saved.", interaction.locale))
