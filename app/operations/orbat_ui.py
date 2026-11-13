"""The panel's ORBAT section (docs/PLAN-front-end.md, *Commands and buttons*; docs/PLAN.md, *ORBAT format*): write
or paste the ORBAT in its text form, take a shared template, save one, rename or delete one's own, and the
organisers' slot tools (lock, unlock, remove the holder, assign a member). The supervisor parses the text and keeps
holders in matching slots (the others go to the top of the reserve list); its problems come back by line."""

from __future__ import annotations

from typing import Any

import discord

from app.discord_util import finish, reply, services, thinking
from app.i18n import t
from app.operations.ui import TIMEOUT, TextModal, person_ids, read, write
from app.signup.ui import act


def summary(answer: dict[str, Any], locale: Any) -> str:
    """What was saved, squad by squad, so a squad line missing its ``#`` shows at once; and who lost a slot."""
    outcome = answer.get("outcome") or {}
    squads = outcome.get("squads") or []
    listed = ", ".join(f"{s.get('name')} ({s.get('slots')})" for s in squads)
    text = t(("Saved: {count} squad: {squads}.", "Saved: {count} squads: {squads}."), locale, count=len(squads),
             squads=listed or "—")
    displaced = int(outcome.get("displaced") or 0)
    if displaced:
        text += "\n" + t(("{count} member lost their slot and is first on the reserve list.",
                          "{count} members lost their slot and are first on the reserve list."), locale, count=displaced)
    return text[:1900]


class OrbatTools(discord.ui.View):
    def __init__(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        super().__init__(timeout=TIMEOUT)
        self.op_id, locale = int(op["id"]), interaction.locale
        actions = set((op.get("my") or {}).get("actions") or [])
        buttons = []
        if "orbat" in actions:
            buttons += [("paste", t("Write or paste", locale)), ("template", t("From a template", locale)),
                        ("save", t("Save as template", locale)), ("manage", t("Rename or delete templates", locale))]
        if "slots" in actions and op.get("orbat"):
            buttons.append(("slots", t("Lock, assign, remove", locale)))
        for action, label in buttons:
            button = discord.ui.Button(label=label)
            button.callback = lambda i, a=action: getattr(self, f"_{a}")(i)
            self.add_item(button)

    async def _orbat(self, interaction: discord.Interaction) -> dict[str, Any]:
        client = services(interaction).supervisor
        return await act(interaction, lambda me: client.orbat(me, self.op_id))

    async def _set(self, interaction: discord.Interaction, text: str, revision: int | None) -> str | None:
        client = services(interaction).supervisor
        answer = await write(interaction, self.op_id, "orbat",
                             lambda me: client.set_orbat(me, self.op_id, text, revision))
        return summary(answer, interaction.locale) if answer is not None else None

    async def _paste(self, interaction: discord.Interaction) -> None:
        current = await self._orbat(interaction)

        async def save(modal_interaction: discord.Interaction, text: str) -> str | None:
            return await self._set(modal_interaction, text, current.get("revision"))

        await interaction.response.send_modal(TextModal(interaction, t("ORBAT", interaction.locale),
                                                        current.get("text") or "", 4000, save))

    async def _template(self, interaction: discord.Interaction) -> None:
        client, locale = services(interaction).supervisor, interaction.locale
        templates = await act(interaction, lambda me: client.templates(me))
        if not templates:
            await reply(interaction, t("No templates yet: save one first.", locale))
            return
        select = discord.ui.Select(placeholder=t("From a template", locale), options=[
            discord.SelectOption(label=str(x["name"])[:100], value=str(x["id"])) for x in templates[:25]])

        async def chosen(i: discord.Interaction) -> None:
            await thinking(i)
            template = next(x for x in templates if str(x["id"]) == select.values[0])
            current = await self._orbat(i)
            answer = await self._set(i, template["text"], current.get("revision"))
            if answer is not None:
                await reply(i, answer)

        select.callback = chosen
        view = discord.ui.View(timeout=TIMEOUT)
        view.add_item(select)
        await reply(interaction, t("From a template", locale), view=view)

    async def _save(self, interaction: discord.Interaction) -> None:
        client, locale = services(interaction).supervisor, interaction.locale
        current = await self._orbat(interaction)
        text = current.get("text") or ""
        if not text.strip():
            await reply(interaction, t("There is no ORBAT to save yet.", locale))
            return
        op = await read(interaction, self.op_id)

        async def save(modal_interaction: discord.Interaction, name: str) -> str | None:
            await act(modal_interaction, lambda me: client.save_template(me, name.strip(), text))
            return t("Template saved.", locale)

        await interaction.response.send_modal(TextModal(interaction, t("Save as template", locale), op["title"], 100,
                                                        save, label=t("Template name", locale), long=False))

    async def _manage(self, interaction: discord.Interaction) -> None:
        client, locale = services(interaction).supervisor, interaction.locale
        mine = [x for x in await act(interaction, lambda me: client.templates(me)) if x.get("mine")]
        if not mine:
            await reply(interaction, t("You have no templates to change.", locale))
            return
        await reply(interaction, t("Pick a template, then rename or delete it.", locale),
                    view=TemplateTools(interaction, mine))

    async def _slots(self, interaction: discord.Interaction) -> None:
        op = await read(interaction, self.op_id)
        if not op.get("orbat"):
            await reply(interaction, t("There is no ORBAT yet.", interaction.locale))
            return
        await reply(interaction, t("Pick the squad, then the slot.", interaction.locale), view=SlotTools(interaction, op))


class TemplateTools(discord.ui.View):
    """The member's own templates (the supervisor says which, ``mine``): pick, then rename or delete. Overwriting is
    *Save as template* under the same name."""

    def __init__(self, interaction: discord.Interaction, templates: list[dict[str, Any]]) -> None:
        super().__init__(timeout=TIMEOUT)
        self.templates, self.template_id = templates, None
        pick = discord.ui.Select(placeholder=t("Template", interaction.locale), options=[
            discord.SelectOption(label=str(x["name"])[:100], value=str(x["id"])) for x in templates[:25]])
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
        client, locale, template_id = services(interaction).supervisor, interaction.locale, self.template_id
        template = next(x for x in self.templates if x["id"] == template_id)

        async def save(modal_interaction: discord.Interaction, name: str) -> str | None:
            await act(modal_interaction, lambda me: client.rename_template(me, template_id, name.strip()))
            return t("Saved.", locale)

        await interaction.response.send_modal(TextModal(interaction, t("Rename", locale), template["name"], 100, save,
                                                        label=t("Template name", locale), long=False))

    async def _delete(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        client, template_id = services(interaction).supervisor, self.template_id
        await act(interaction, lambda me: client.delete_template(me, template_id))
        self.templates = [x for x in self.templates if x["id"] != template_id]
        await finish(interaction, t("Template deleted.", interaction.locale))


class SlotTools(discord.ui.View):
    """Squad, then slot (selects take 25 options), then lock / unlock, remove the holder, or assign a member (who
    then holds it locked)."""

    def __init__(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        super().__init__(timeout=TIMEOUT)
        self.op_id, self.squads, self.slot_id = int(op["id"]), op.get("orbat") or [], None
        squads = discord.ui.Select(placeholder=t("Squad", interaction.locale), options=[
            discord.SelectOption(label=str(squad["name"])[:100], value=str(squad["id"])) for squad in self.squads[:25]])
        squads.callback = lambda i, s=squads: self._squad(i, int(s.values[0]))
        self.add_item(squads)

    async def _squad(self, interaction: discord.Interaction, squad_id: int) -> None:
        locale = interaction.locale
        slots = next((squad.get("slots") or [] for squad in self.squads if squad["id"] == squad_id), [])
        for item in list(self.children)[1:]:
            self.remove_item(item)
        pick = discord.ui.Select(placeholder=t("Slot", locale), options=[
            discord.SelectOption(label=f"{n}. {slot['name']}"[:100], value=str(slot["id"]),
                                 description=("⚠ " if slot.get("flagged") else "") + ("🔒 " if slot.get("locked") else "")
                                 + (t("taken", locale) if slot.get("holder") else t("free", locale)))
            for n, slot in enumerate(slots[:25], 1)])
        pick.callback = lambda i, s=pick: self._slot(i, int(s.values[0]))
        self.add_item(pick)
        await interaction.response.edit_message(view=self)

    async def _slot(self, interaction: discord.Interaction, slot_id: int) -> None:
        self.slot_id, locale = slot_id, interaction.locale
        for item in list(self.children)[2:]:
            self.remove_item(item)
        for action, label in (("lock", t("Lock", locale)), ("unlock", t("Unlock", locale)),
                              ("remove", t("Remove holder", locale))):
            button = discord.ui.Button(label=label, row=2)
            button.callback = lambda i, a=action: self._act(i, a)
            self.add_item(button)
        assign = discord.ui.UserSelect(placeholder=t("Assign a member (locked)", locale), row=3)
        assign.callback = lambda i, s=assign: self._act(i, "assign", s.values[0])
        self.add_item(assign)
        await interaction.response.edit_message(view=self)

    async def _act(self, interaction: discord.Interaction, action: str, member: Any = None) -> None:
        await thinking(interaction)
        person = None
        if member is not None:
            found = await person_ids(interaction, [member])
            if found is None:
                return
            person = found[0]
        client, slot_id = services(interaction).supervisor, self.slot_id
        await act(interaction, lambda me: client.slot_action(me, self.op_id, slot_id, action, person))
        await reply(interaction, t("Saved.", interaction.locale))
