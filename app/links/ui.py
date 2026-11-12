"""Steam links on Discord (docs/PLAN-front-end.md, *Commands and buttons*): the *Link now* button (in reminders, the
rollout announcement and a first sign-up's answer; it keeps working after a restart), its modal, and :func:`save`,
which ``/link`` and the modal share. The member links their own Steam ID through the supervisor (act-as); the link
waits for staff to confirm it in the app, and the nick in squad.xml is the supervisor's (the player's latest name,
or staff's). The staff log hears of it through the supervisor's ``link_request`` / ``link_duplicate`` notices."""

from __future__ import annotations

import logging
from typing import Any

import discord

from app.discord_util import reply, services, thinking
from app.i18n import t
from app.links.service import LinkError, Problem

log = logging.getLogger(__name__)
TIMEOUT = 15 * 60


def problem(code: Problem, locale: Any) -> str:
    return {
        Problem.BAD_ID: t("Give your Steam64 ID (17 digits, starting with 7656119) or your "
                          "steamcommunity.com/profiles/… address.", locale),
        Problem.CUSTOM_URL: t("The bot can't read a custom address (steamcommunity.com/id/…). In Steam, open Account "
                              "details: your Steam ID (17 digits) is under your account name. Use that, or a "
                              "steamcommunity.com/profiles/… address.", locale),
        Problem.TAKEN: t("That Steam ID is linked to another member. Staff have been told; ask them if it is yours.",
                         locale),
    }[code]


def link_view(language: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LinkNow(t("Link now", language)))
    return view


def squadxml_lines(person: dict[str, Any], locale: Any) -> list[str]:
    lines = []
    if person.get("squadxml_url"):
        lines.append(t("Put this squad.xml address in your Arma 3 profile: {url}", locale, url=person["squadxml_url"]))
    lines.append(t("squad.xml is public: it shows your Steam ID, nick, Discord username and remarks. /unlink "
                   "removes the link; /privacy tells what else the bot keeps.", locale))
    return lines


async def save(interaction: discord.Interaction, steam_text: str) -> bool:
    """Link the member's own Steam ID and answer. True when linked."""
    s, locale = services(interaction), interaction.locale
    await thinking(interaction)  # the supervisor may take a moment
    try:
        person = await s.links.link(interaction.user.id, steam_text)
    except LinkError as exc:
        await reply(interaction, problem(exc.problem, locale))
        return False
    lines = [t("Linked: Steam ID {steam}.", locale, steam=person.get("steam_uid"))]
    if person.get("steam_state") != "confirmed":
        lines.append(t("Staff confirm the link in the app; until then squad.xml and attendance don't count it.",
                       locale))
    lines += squadxml_lines(person, locale)
    await reply(interaction, "\n".join(lines))
    return True


class LinkNow(discord.ui.DynamicItem[discord.ui.Button], template=r"link:now"):
    """*Link now*: the modal, for whoever presses it (in a DM, in the reminder channel, under an answer)."""

    def __init__(self, label: str = "Link now") -> None:
        super().__init__(discord.ui.Button(label=label, style=discord.ButtonStyle.primary, emoji="🔗",
                                           custom_id="link:now"))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: Any) -> LinkNow:
        return cls()

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LinkModal(interaction))


class LinkModal(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, steam: str | None = None) -> None:
        locale = interaction.locale
        super().__init__(title=t("Link your Steam account", locale)[:45], timeout=TIMEOUT)
        self.steam = discord.ui.TextInput(label=t("Steam64 ID or profile address", locale)[:45], max_length=120,
                                          default=steam)
        self.add_item(self.steam)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await save(interaction, self.steam.value)
