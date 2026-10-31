"""The start-up check (docs/PLAN.md, *Configuration and deployment*, *Check at startup*): every configured channel
and role exists, the categories are categories, and the bot holds the Discord permissions it needs there. The
problems are logged and posted to the staff log; the bot keeps running."""

from __future__ import annotations

from typing import Any

import discord

from app.i18n import t
from app.settings import Settings

TEXT = ("view_channel", "send_messages", "embed_links", "read_message_history")
# Event channels are made in the categories, inherit from them, and get their own overwrites and threads.
CATEGORY = ("view_channel", "manage_channels", "manage_roles", "send_messages", "send_messages_in_threads",
            "create_public_threads", "manage_threads", "embed_links", "attach_files", "read_message_history")


def _channels(settings: Settings) -> list[tuple[str, int | None, bool]]:
    c = settings.channels
    return [("drafts_category", c.drafts_category, True), ("events_category", c.events_category, True),
            ("header", c.header, False), ("announcements", c.announcements, False),
            ("moderation", c.moderation, False), ("staff_log", c.staff_log, False),
            ("link_reminder", c.link_reminder, False)]


def _roles(settings: Settings) -> list[tuple[str, int]]:
    r, p = settings.roles, settings.permissions
    found = [("roles.member", r.member), ("roles.mission_maker", r.mission_maker)]
    if r.announcement is not None:
        found.append(("roles.announcement", r.announcement))
    found += [("roles.signup", role) for role in r.signup]
    found += [(f"permissions.{name}", role) for name, roles in p.model_dump().items() for role in roles]
    return found


def check(guild: Any, settings: Settings) -> list[str]:
    """Everything wrong with the configuration on this server, in the public language. Empty when all is well."""
    language = settings.language
    problems: list[str] = []
    me = guild.me
    for name, channel_id, is_category in _channels(settings):
        if channel_id is None:
            continue
        variable = f"BOT_CHANNELS__{name.upper()}"
        channel = guild.get_channel(channel_id)
        if channel is None:
            problems.append(t("{variable}: no channel {id} on this server", language, variable=variable, id=channel_id))
            continue
        if is_category != (channel.type == discord.ChannelType.category):
            wanted = t("a category", language) if is_category else t("a text channel", language)
            problems.append(t("{variable}: {name} is not {wanted}", language, variable=variable, name=channel.name,
                              wanted=wanted))
            continue
        allowed = channel.permissions_for(me)
        missing = [perm for perm in (CATEGORY if is_category else TEXT) if not getattr(allowed, perm)]
        if missing:
            problems.append(t("{variable}: the bot lacks {permissions} in {name}", language, variable=variable,
                              permissions=", ".join(missing), name=channel.name))
    for name, role_id in _roles(settings):
        if guild.get_role(role_id) is None:
            variable = f"BOT_{name.replace('.', '__').upper()}"
            problems.append(t("{variable}: no role {id} on this server", language, variable=variable, id=role_id))
    announcement = guild.get_role(settings.roles.announcement) if settings.roles.announcement else None
    if announcement is not None and not announcement.mentionable and not me.guild_permissions.mention_everyone:
        problems.append(t("The bot can't mention {role}: make the role mentionable, or give the bot "
                          "\"Mention @everyone, @here and All Roles\"", language, role=announcement.name))
    return problems
