"""Steam links on Discord (docs/PLAN.md, *Steam link and squad.xml*): the *Link now* button (in reminders, the
rollout announcement and a first sign-up's answer; it keeps working after a restart), its modal, and
:func:`save`, which ``/link``, the modal and ``/links set`` share. The reply says what squad.xml publishes and where
it is; every change goes to the staff log, and so does a refused duplicate."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from app.discord_util import reply, services
from app.i18n import t
from app.links.service import MAX_NICK, LinkError, Problem
from app.supervisor_client import SupervisorError

log = logging.getLogger(__name__)
TIMEOUT = 15 * 60
LOOKUP_SECONDS = 5  # the last in-game name is a courtesy: never hold up the answer for it


def problem(code: Problem, locale: Any) -> str:
    return {
        Problem.BAD_ID: t("Give your Steam64 ID (17 digits, starting with 7656119) or your "
                          "steamcommunity.com/profiles/… address.", locale),
        Problem.CUSTOM_URL: t("The bot can't read a custom address (steamcommunity.com/id/…). In Steam, open Account "
                              "details: your Steam ID (17 digits) is under your account name. Use that, or a "
                              "steamcommunity.com/profiles/… address.", locale),
        Problem.BAD_NICK: t("Give a nick of 1 to 50 characters.", locale),
        Problem.TAKEN: t("That Steam ID is linked to another member. Staff have been told; ask them if it is yours.",
                         locale),
    }[code]


def has_rank(s: Any, member_id: int) -> bool:
    return not (s.roles_of(member_id) or frozenset()).isdisjoint(s.ids.rank)


def link_view(language: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(LinkNow(t("Link now", language)))
    return view


async def last_name(s: Any, steam_id: str) -> str | None:
    """The name a server last saw this Steam ID under, if the supervisor answers in time."""
    try:
        player = await asyncio.wait_for(s.supervisor.player_by_uid(steam_id), LOOKUP_SECONDS)
    except (SupervisorError, TimeoutError) as exc:
        log.info("No player lookup for %s: %s", steam_id, exc)
        return None
    return player.get("name") if player else None


async def save(interaction: discord.Interaction, member_id: int, steam_text: str, nick_text: str) -> bool:
    """Link ``member_id`` (the member themselves, or staff for them) and answer. True when linked."""
    s, locale, language = services(interaction), interaction.locale, services(interaction).settings.language
    own = member_id == interaction.user.id
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)  # the supervisor may take a moment
    try:
        linked = await s.links.link(member_id, steam_text, nick_text, interaction.user.id, has_rank(s, member_id))
    except LinkError as exc:
        if exc.problem == Problem.TAKEN:
            s.staff.post(t("Duplicate Steam ID: {member} tried {steam}, which is linked to {owner}.", language,
                           member=f"<@{member_id}>", steam=steam_text.strip(), owner=f"<@{exc.owner_id}>"))
        await reply(interaction, problem(exc.problem, locale))
        return False
    if linked.changed:
        s.squadxml.request()
        before = f"{linked.before[0]} ({linked.before[1]})" if linked.before else "—"
        s.staff.post(t("Steam link of {member}: {before} → {after}, by {who}.", language, member=f"<@{member_id}>",
                       before=before, after=f"{linked.steam_id} ({linked.nick})", who=interaction.user.mention))
    lines = [t("Linked: Steam ID {steam}, nick {nick}.", locale, steam=linked.steam_id, nick=linked.nick)]
    seen = await last_name(s, linked.steam_id)
    lines.append(t("Our servers last saw this account as {name}.", locale, name=seen) if seen else
                 t("Our servers haven't seen this account yet; it is linked all the same.", locale))
    if own:
        url = s.settings.squad_xml.url
        if url:
            lines.append(t("Put this squad.xml address in your Arma 3 profile: {url}", locale, url=url))
        lines.append(t("squad.xml is public: it shows your Steam ID, nick, Discord username and remarks. /unlink "
                       "removes the link; /privacy tells what else the bot keeps.", locale))
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
        await interaction.response.send_modal(LinkModal(interaction, await services(interaction).links.get(
            interaction.user.id)))


class LinkModal(discord.ui.Modal):
    def __init__(self, interaction: discord.Interaction, current: Any = None, steam: str | None = None,
                 nick: str | None = None) -> None:
        locale = interaction.locale
        super().__init__(title=t("Link your Steam account", locale)[:45], timeout=TIMEOUT)
        self.steam = discord.ui.TextInput(label=t("Steam64 ID or profile address", locale)[:45], max_length=120,
                                          default=steam or (current.steam_id if current else None))
        self.nick = discord.ui.TextInput(label=t("Nick in squad.xml", locale)[:45], max_length=MAX_NICK,
                                         default=nick or (current.nick if current else
                                                          getattr(interaction.user, "display_name", None)))
        self.add_item(self.steam)
        self.add_item(self.nick)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await save(interaction, interaction.user.id, self.steam.value, self.nick.value)
