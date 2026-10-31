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


async def reply(interaction: discord.Interaction, content: str | None = None, **kwargs: Any) -> None:
    """Answer privately, whether or not the interaction was answered already."""
    kwargs.setdefault("ephemeral", True)
    kwargs.setdefault("allowed_mentions", NO_MENTIONS)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
    else:
        await interaction.response.send_message(content, **kwargs)


async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    if isinstance(error, NotAllowed):
        await reply(interaction, str(error))
        return
    original = getattr(error, "original", error)
    log.error("Command /%s failed: %s", interaction.command.qualified_name if interaction.command else "?",
              original, exc_info=original)
    await reply(interaction, t("Something went wrong. Staff can find the details in the log.", interaction.locale))
