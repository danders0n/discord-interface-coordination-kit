"""Small helpers for the Discord layer: a member's rights, the permission check for slash commands, and the
reply to a failed command."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from app.i18n import t
from app.permissions import Access, Perm

log = logging.getLogger(__name__)
NO_MENTIONS = discord.AllowedMentions.none()


class NotAllowed(app_commands.CheckFailure):
    """A check that failed with a message for the member."""


def services(interaction: discord.Interaction) -> Any:
    return interaction.client.services  # type: ignore[attr-defined]


def access(interaction: discord.Interaction) -> Access:
    roles = getattr(interaction.user, "roles", ())
    return Access(services(interaction).ids, (role.id for role in roles))


def require(perm: Perm):
    """Only members holding ``perm`` through their roles (on top of Discord's own command visibility)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        if access(interaction).has(perm):
            return True
        raise NotAllowed(t("You don't have the permission for this.", interaction.locale))

    return app_commands.check(predicate)


def fleeting(interaction: discord.Interaction) -> float | None:
    """How long a private answer stays (``times.reply_seconds``); None: it stays."""
    settings = getattr(services(interaction), "settings", None)
    return (settings.times.reply_seconds or None) if settings is not None else None


async def reply(interaction: discord.Interaction, content: str | None = None, **kwargs: Any) -> None:
    """Answer privately, whether or not the interaction was answered already. A plain text answer (a confirmation,
    a refusal) disappears after :func:`fleeting`; one with buttons, an embed or a file stays to be used or read."""
    kwargs.setdefault("ephemeral", True)
    kwargs.setdefault("allowed_mentions", NO_MENTIONS)
    plain = kwargs["ephemeral"] and not {"view", "embed", "embeds", "file", "files"} & set(kwargs)
    seconds = fleeting(interaction) if plain else None
    if interaction.response.is_done():
        message = await interaction.followup.send(content, wait=True, **kwargs)
        if seconds and message is not None:
            await message.delete(delay=seconds)
    else:
        await interaction.response.send_message(content, delete_after=seconds, **kwargs)


async def thinking(interaction: discord.Interaction) -> None:
    """Answer "thinking..." at once, unless answered already: Discord drops an interaction not answered within 3
    seconds, and work that touches channels or the supervisor can take longer. :func:`reply` sends the answer."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)


async def finish(interaction: discord.Interaction, content: str) -> None:
    """Replace the message whose button was pressed with a last word, which then disappears like a reply. After
    ``interaction.response.defer()`` (a deferred update, not :func:`thinking`) it edits that message as well."""
    seconds = fleeting(interaction)
    if not interaction.response.is_done():
        await interaction.response.edit_message(content=content, view=None, delete_after=seconds)
        return
    message = await interaction.edit_original_response(content=content, view=None)
    if seconds and message is not None:
        await message.delete(delay=seconds)


async def _sorry(interaction: discord.Interaction, error: Exception, where: str) -> None:
    log.error("%s failed: %s", where, error, exc_info=error)
    try:
        await reply(interaction, t("Something went wrong. Staff can find the details in the log.", interaction.locale))
    except discord.HTTPException:  # (the interaction expired meanwhile)
        pass


async def on_view_error(view: Any, interaction: discord.Interaction, error: Exception, item: Any) -> None:
    """Buttons and lists (the persistent ones too): the member hears that it failed, as after a command."""
    await _sorry(interaction, error, f"{type(item).__name__} in {type(view).__name__}")


async def on_modal_error(modal: Any, interaction: discord.Interaction, error: Exception) -> None:
    await _sorry(interaction, error, f"Modal {type(modal).__name__}")


def install_error_handlers() -> None:
    """discord.py's views and modals only log a failure; every one of them answers instead (set once, at start)."""
    discord.ui.view.BaseView.on_error = on_view_error  # type: ignore[method-assign]
    discord.ui.Modal.on_error = on_modal_error  # type: ignore[method-assign]


async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, NotAllowed):
        await reply(interaction, str(error))
        return
    original = getattr(error, "original", error)
    log.error("Command /%s failed: %s", interaction.command.qualified_name if interaction.command else "?",
              original, exc_info=original)
    await reply(interaction, t("Something went wrong. Staff can find the details in the log.", interaction.locale))
