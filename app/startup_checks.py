"""The start-up check (docs/PLAN.md, *Configuration and deployment*, *Check at startup*): every configured channel
and role is found (by id or name, app/resolve.py), and the bot holds the Discord permissions it needs there. The
problems are logged and posted to the staff log; the bot keeps running with what it could resolve."""

from __future__ import annotations

from typing import Any

from app.i18n import t
from app.resolve import CATEGORY_CHANNELS, Ids, resolve
from app.settings import Settings

TEXT = ("view_channel", "send_messages", "embed_links", "read_message_history")
# Operation channels are made in the categories, inherit from them, and get their own overwrites and threads.
CATEGORY = ("view_channel", "manage_channels", "manage_roles", "send_messages", "send_messages_in_threads",
            "create_public_threads", "manage_threads", "embed_links", "attach_files", "read_message_history")
# The bot keeps its staff log and moderation threads in the staff channel.
STAFF = TEXT + ("send_messages_in_threads", "create_public_threads", "manage_threads")


def check(guild: Any, settings: Settings) -> tuple[Ids, list[str]]:
    """The resolved ids, and everything wrong with the configuration on this server in the public language."""
    language = settings.language
    ids, problems = resolve(guild, settings)
    me = guild.me
    for name in ("events_category", "header", "announcements", "staff", "link_reminder"):
        channel_id = getattr(ids, name)
        if channel_id is None:
            continue
        channel = guild.get_channel(channel_id)
        needed = CATEGORY if name in CATEGORY_CHANNELS else STAFF if name == "staff" else TEXT
        allowed = channel.permissions_for(me)
        missing = [perm for perm in needed if not getattr(allowed, perm)]
        if missing:
            problems.append(t("{variable}: the bot lacks {permissions} in {name}", language,
                              variable=f"BOT_CHANNELS__{name.upper()}", permissions=", ".join(missing),
                              name=channel.name))
    # Announcements and the sign-up ping mention the announcement role (and the climate's, from the supervisor).
    role = guild.get_role(ids.announcement) if ids.announcement else None
    if role is not None and not role.mentionable and not me.guild_permissions.mention_everyone:
        problems.append(t("The bot can't mention {role}: make the role mentionable, or give the bot "
                          "\"Mention @everyone, @here and All Roles\"", language, role=role.name))
    return ids, problems
