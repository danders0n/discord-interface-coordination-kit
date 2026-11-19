"""An operation's preset and mission from Discord (docs/PLAN-front-end.md, *Commands and buttons*), shared by the
commands (``/operation preset | mission``) and the organiser panel's *Preset* and *Mission* buttons, which open a
modal with a file field. The attachment is downloaded from Discord and uploaded in the member's name; the supervisor
decides and reports what it found (a member's files wait for approval before reaching the host). A mission over
Discord's upload limit goes onto the server another way and is then picked by name."""

from __future__ import annotations

from typing import Any

import discord

from app.discord_util import reply, services, thinking
from app.i18n import t
from app.signup.ui import act

MAX_PRESET = 2 * 1024 * 1024  # the supervisor's limits: refused here before a download
MAX_IMAGE = 8 * 1024 * 1024
TIMEOUT = 15 * 60


async def upload(interaction: discord.Interaction, op_id: int, slot: str, file: Any) -> dict[str, Any]:
    """The attachment sent on to the supervisor's file route for ``slot``; the operation it answers with."""
    await thinking(interaction)  # (downloaded from Discord, then sent on)
    content, client = await file.read(), services(interaction).supervisor
    return await act(interaction, lambda me: client.upload(me, op_id, slot, file.filename, content))


async def set_preset(interaction: discord.Interaction, op_id: int, file: Any) -> None:
    locale = interaction.locale
    if not file.filename.lower().endswith(".html") or file.size > MAX_PRESET:
        await reply(interaction, t("Send the .html preset exported from the Arma 3 Launcher.", locale))
        return
    op = await upload(interaction, op_id, "preset", file)
    preset = (op.get("files") or {}).get("preset") or {}
    lines = [t(("Preset saved: {count} mod.", "Preset saved: {count} mods."), locale,
               count=len(preset.get("mods") or ()))]
    if preset.get("staged"):
        lines.append(t("It reaches the server once the operation is approved.", locale))
    await reply(interaction, "\n".join(lines))


async def set_mission(interaction: discord.Interaction, op_id: int, file: Any | None, on_server: str | None) -> None:
    """An uploaded ``.pbo``, else a mission already on the server by name; the answer gives its report and the
    supervisor's warnings."""
    from app.operations.ui import warning_words  # (the panel's module; it opens the modals below)

    locale = interaction.locale
    on_server = (on_server or "").strip()
    if file is None and not on_server:
        await reply(interaction, t("Attach the mission's .pbo, or pick a mission already on the server.", locale))
        return
    if file is not None:
        op = await upload(interaction, op_id, "mission", file)
    else:
        await thinking(interaction)
        client = services(interaction).supervisor
        op = await act(interaction, lambda me: client.pick_mission(me, op_id, on_server))
    mission = op.get("mission") or {}
    report = mission.get("report") or {}
    lines = [t("Mission set: {name} ({terrain}, {players} playable).", locale, name=mission.get("name") or "?",
               terrain=report.get("terrain") or "?", players=report.get("playable") or "?")]
    if mission.get("staged"):
        lines.append(t("It reaches the server once the operation is approved.", locale))
    lines += ["⚠ " + warning_words(w, locale) for w in op.get("warnings") or []]
    await reply(interaction, "\n".join(lines)[:1900])


def _file_field(text: str, description: str, *, required: bool) -> tuple[discord.ui.Label, discord.ui.FileUpload]:
    field = discord.ui.FileUpload(required=required, min_values=1 if required else 0, max_values=1)
    return discord.ui.Label(text=text[:45], description=description[:100], component=field), field


class PresetModal(discord.ui.Modal):
    """The panel's *Preset*: the launcher preset as a file."""

    def __init__(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        locale = interaction.locale
        super().__init__(title=t("Preset", locale)[:45], timeout=TIMEOUT)
        self.op_id = int(op["id"])
        label, self.file = _file_field(t("Preset (.html)", locale),
                                       t("Exported from the Arma 3 Launcher (Mods, Preset, Export).", locale),
                                       required=True)
        self.add_item(label)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not self.file.values:  # (Discord requires one; a client that sends none is answered all the same)
            await reply(interaction, t("Send the .html preset exported from the Arma 3 Launcher.", interaction.locale))
            return
        await set_preset(interaction, self.op_id, self.file.values[0])


class MissionModal(discord.ui.Modal):
    """The panel's *Mission*: a ``.pbo`` as a file, or the name of a mission already on the server (one over
    Discord's upload limit goes there another way first)."""

    def __init__(self, interaction: discord.Interaction, op: dict[str, Any]) -> None:
        locale = interaction.locale
        super().__init__(title=t("Mission", locale)[:45], timeout=TIMEOUT)
        self.op_id = int(op["id"])
        label, self.file = _file_field(t("Mission (.pbo)", locale), t("Up to Discord's upload limit.", locale),
                                       required=False)
        self.on_server = discord.ui.TextInput(required=False, max_length=200,
                                              placeholder="ibis.Altis.pbo")
        self.add_item(label)
        self.add_item(discord.ui.Label(
            text=t("Or a mission on the server", locale)[:45],
            description=t("A bigger mission goes onto the server another way; then type its file name here.",
                          locale)[:100],
            component=self.on_server))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        file = self.file.values[0] if self.file.values else None
        await set_mission(interaction, self.op_id, file, None if file is not None else self.on_server.value)
