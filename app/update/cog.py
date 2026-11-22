"""``/update`` (docs/PLAN-remote-update.md, *Commands*), bot admins only: ``status``, ``check`` (with *Update now*),
``now``, ``restart``, ``back``. While a start waits for the scheduler's running action, *Restart now* and *Cancel*.
The work is :class:`app.update.service.Updater`'s; this module words it. Every start, cancel and outcome also goes to
the staff log (the updater posts it)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from app.discord_util import access, answers, reply, require, services, thinking
from app.i18n import t
from app.permissions import Perm
from app.services import Services
from app.update.service import UpdateError, Updater, short

MAX_LISTED = 15


def words(error: UpdateError, locale: Any) -> str:
    known = {
        "not_checkout": t("The bot's folder is not a git checkout: update it by hand (update.ps1).", locale),
        "no_git": t("git is not installed, or not on the PATH of the account the bot runs as.", locale),
        "no_restarter": t("Nothing would start the bot again: run it under NSSM with BOT_RESTARTER=nssm, or with "
                          "python -m app in a console.", locale),
        "busy": t("An update or restart is already under way.", locale),
        "bad_env": t("The .env file has an error, so the bot would not start again: {detail}", locale,
                     detail=error.detail),
        "bad_branch": t("The branch can't be used: {detail}", locale, detail=error.detail),
        "git": t("git failed: {detail}", locale, detail=error.detail),
        "up_to_date": t("The bot is up to date.", locale),
        "moved": t("The branch has new commits since that check: check again.", locale),
        "nothing_back": t("Nothing to go back to: the last update did not finish, or the checkout moved since.",
                          locale),
        "commit_gone": t("Commit {detail} is no longer in the checkout.", locale, detail=error.detail),
        "no_backup": t("The database backup from before that update is gone.", locale),
        "needs_restore": t("The database changed with that update, so the older version can't open it: go back with "
                           "restore_db (what happened since that update is lost).", locale),
        "nothing_waiting": t("Nothing is waiting.", locale),
        "too_late": t("Too late: the bot is restarting.", locale),
    }
    return known.get(error.code, error.code)


def started(waiting: dict[str, Any], updater: Updater, locale: Any) -> tuple[str, discord.ui.View | None]:
    """The answer to a start: restarting now, or waiting for the running action (with *Restart now* / *Cancel*)."""
    if waiting["kind"] == "restart":
        what = t("Restart", locale)
    else:
        what = t("{kind} to {commit}", locale, kind=updater.kind_word(waiting["kind"]).capitalize(),
                 commit=short(waiting["to"]))
    if not waiting.get("action"):
        return t("{what}: the bot restarts now and says in the staff log how it went.", locale, what=what), None
    text = t("{what}: waiting for the scheduled action {action} to finish (at most {minutes} min).", locale,
             what=what, action=waiting["action"], minutes=waiting["minutes"])
    view = discord.ui.View(timeout=None)
    view.add_item(UpdateButton("hurry", label=t("Restart now", locale)))
    view.add_item(UpdateButton("cancel", label=t("Cancel", locale)))
    return text, view


def check_embed(check: dict[str, Any], locale: Any) -> discord.Embed:
    commits = check["commits"]
    embed = discord.Embed(title=t("Updates on {branch}", locale, branch=check["branch"]),
                          colour=discord.Colour.blurple() if commits else discord.Colour.green())
    if not commits:
        embed.description = t("The bot is up to date ({commit}).", locale, commit=short(check["head"]))
        return embed
    lines = []
    for commit in commits[:MAX_LISTED]:
        try:
            when = f" <t:{int(datetime.fromisoformat(commit['date']).timestamp())}:R>"
        except ValueError:
            when = ""
        lines.append(f"`{short(commit['sha'])}` {commit['subject'][:120]} · {commit['author']}{when}")
    if len(commits) > MAX_LISTED:
        lines.append(t(("…and {count} more", "…and {count} more"), locale, count=len(commits) - MAX_LISTED))
    embed.description = t("{head} → {tip}", locale, head=short(check["head"]), tip=short(check["tip"])) + "\n" + \
        "\n".join(lines)
    notes = []
    if check["requirements"]:
        notes.append(t("Its dependencies change: the restart takes longer.", locale))
    if check["migrations"]:
        notes.append(t("Its database changes: going back later needs the backup taken before it.", locale))
    if check["dirty"]:
        notes.append(t(("{count} locally changed file will be discarded (kept in data/updates).",
                        "{count} locally changed files will be discarded (kept in data/updates)."), locale,
                       count=len(check["dirty"])))
    if check["local_commits"]:
        notes.append(t(("{count} local commit is not on the branch and will be lost.",
                        "{count} local commits are not on the branch and will be lost."), locale,
                       count=len(check["local_commits"])))
    if notes:
        embed.add_field(name=t("Good to know", locale), value="\n".join(f"- {n}" for n in notes)[:1024], inline=False)
    return embed


class UpdateCog(commands.Cog):
    update = app_commands.Group(name="update", description="Update or restart the bot (bot admins)", guild_only=True,
                                default_permissions=discord.Permissions(manage_guild=True))

    def __init__(self, services: Services) -> None:
        self.services = services

    @property
    def updater(self) -> Updater:
        return self.services.updater

    @update.command(name="status", description="The bot's version, what is under way and the last updates")
    @require(Perm.BOT_ADMIN)
    async def status(self, interaction: discord.Interaction) -> None:
        locale = interaction.locale
        found = await asyncio.to_thread(self.updater.status)
        lines = [t("Version {commit} on {branch}.", locale, commit=short(found["head"]), branch=found["branch"] or "?")]
        lines.append(t("Started again by: {restarter}.", locale, restarter=found["restarter"]) if found["restarter"]
                     else t("Nothing would start the bot again: /update can't restart it.", locale))
        if found["waiting"]:
            lines.append(t("Under way: {kind} (asked by {member}).", locale,
                           kind=self.updater.kind_word(found["waiting"]["kind"]), member=found["waiting"]["actor"]))
        if found["go_back"]:
            lines.append(t("/update back would return to {commit}.", locale, commit=short(found["go_back"]["to"])))
        if found["history"]:
            lines.append(t("Last:", locale))
            for entry in found["history"]:
                when = entry.get("finished_at") or entry.get("requested_at") or ""
                try:
                    stamp = f"<t:{int(datetime.fromisoformat(when).timestamp())}:f> "
                except ValueError:
                    stamp = ""
                lines.append(f"- {stamp}{self.updater.kind_word(str(entry.get('kind')))} "
                             f"{short(entry.get('from'))} → {short(entry.get('to'))}: {entry.get('outcome')}")
        await reply(interaction, "\n".join(lines)[:2000])

    @update.command(name="check", description="Fetch the branch and show what an update would bring")
    @require(Perm.BOT_ADMIN)
    async def check(self, interaction: discord.Interaction) -> None:
        locale = interaction.locale
        await thinking(interaction)  # (a fetch takes a moment)
        try:
            found = await asyncio.to_thread(self.updater.check)
        except UpdateError as exc:
            await reply(interaction, words(exc, locale))
            return
        view = None
        if found["commits"]:
            view = discord.ui.View(timeout=None)
            view.add_item(UpdateButton("go", found["tip"], label=t("Update now", locale)))
        await reply(interaction, embed=check_embed(found, locale), **({"view": view} if view else {}))

    @update.command(name="now", description="Update the bot to the newest commit of its branch")
    @require(Perm.BOT_ADMIN)
    async def now(self, interaction: discord.Interaction) -> None:
        await go(interaction, "update")

    @update.command(name="restart", description="Restart the bot")
    @require(Perm.BOT_ADMIN)
    async def restart(self, interaction: discord.Interaction) -> None:
        await go(interaction, "restart")

    @update.command(name="back", description="Go back to the commit before the last update")
    @app_commands.describe(restore_db="Also restore the database from before that update (what happened since is lost)")
    @require(Perm.BOT_ADMIN)
    async def back(self, interaction: discord.Interaction, restore_db: bool = False) -> None:
        await go(interaction, "back", restore_db=restore_db)


async def go(interaction: discord.Interaction, kind: str, expect: str | None = None, restore_db: bool = False) -> None:
    updater: Updater = services(interaction).updater
    locale = interaction.locale
    await thinking(interaction)
    try:
        waiting = await updater.start(kind, interaction.user.mention, expect=expect, restore_db=restore_db)
    except UpdateError as exc:
        await reply(interaction, words(exc, locale))
        return
    text, view = started(waiting, updater, locale)
    await reply(interaction, text, **({"view": view} if view else {}))


class UpdateButton(discord.ui.DynamicItem[discord.ui.Button],
                   template=r"update:(?P<action>go|hurry|cancel)(?::(?P<sha>[0-9a-f]{40}))?"):
    """*Update now* under a check (to the tip it showed), *Restart now* / *Cancel* while a start waits. Bot admins."""

    def __init__(self, action: str, sha: str | None = None, label: str = "") -> None:
        self.action, self.sha = action, sha
        style = {"go": discord.ButtonStyle.primary, "hurry": discord.ButtonStyle.danger}.get(action,
                                                                                           discord.ButtonStyle.secondary)
        custom_id = f"update:{action}" + (f":{sha}" if sha else "")
        super().__init__(discord.ui.Button(label=label or action, style=style, custom_id=custom_id))

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button,
                             match: Any) -> UpdateButton:
        return cls(match["action"], match["sha"], label=item.label or "")

    @answers
    async def callback(self, interaction: discord.Interaction) -> None:
        locale = interaction.locale
        if not access(interaction).has(Perm.BOT_ADMIN):
            await reply(interaction, t("You don't have the permission for this.", locale))
            return
        if self.action == "go":
            await go(interaction, "update", expect=self.sha)
            return
        updater: Updater = services(interaction).updater
        try:
            if self.action == "hurry":
                await updater.hurry()
                text = t("The bot restarts now; the scheduled action runs again after the restart.", locale)
            else:
                await updater.cancel(interaction.user.mention)
                text = t("Cancelled.", locale)
        except UpdateError as exc:
            text = words(exc, locale)
        await reply(interaction, text)
