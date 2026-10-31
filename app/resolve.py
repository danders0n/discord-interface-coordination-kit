"""Channels and roles from `.env` turned into ids on the server (docs/PLAN.md, *Configuration and deployment*).

A setting may hold an id or a name. Names are matched without regard to case; a name that matches nothing, or
more than one channel or role, is a problem named after its variable, and that setting stays unresolved. This
runs at start-up and on ``/reload``; everything else reads the resulting :class:`Ids`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import discord

from app.i18n import t
from app.settings import Settings

TEXT_TYPES = (discord.ChannelType.text, discord.ChannelType.news)
CATEGORY_CHANNELS = ("drafts_category", "events_category")
TEXT_CHANNELS = ("header", "announcements", "staff", "link_reminder")


@dataclass(frozen=True)
class Ids:
    """The configuration's channels and roles as ids; None (or left out of a set) when not resolved."""

    drafts_category: int | None = None
    events_category: int | None = None
    header: int | None = None
    announcements: int | None = None
    staff: int | None = None
    link_reminder: int | None = None
    member: frozenset[int] = frozenset()
    mission_maker: int | None = None
    announcement: int | None = None
    signup: frozenset[int] = frozenset()
    permissions: Mapping[str, frozenset[int]] = field(default_factory=dict)
    dlc: Mapping[str, int] = field(default_factory=dict)  # Creator DLC key -> role


def _variable(path: str) -> str:
    return "BOT_" + path.replace(".", "__").upper()


class _Resolver:
    def __init__(self, guild: Any, language: str) -> None:
        self.guild = guild
        self.language = language
        self.problems: list[str] = []

    def _match(self, ref: int | str, items: Iterable[Any], path: str) -> Any | None:
        items = list(items)
        if isinstance(ref, int):
            found = [item for item in items if item.id == ref]
        else:
            found = [item for item in items if item.name.casefold() == ref.casefold()]
        if not found:
            self.problems.append(t("{variable}: {ref} not found on this server", self.language,
                                   variable=_variable(path), ref=ref))
            return None
        if len(found) > 1:
            self.problems.append(t("{variable}: more than one is called {ref}; use the id ({ids})", self.language,
                                   variable=_variable(path), ref=ref, ids=", ".join(str(i.id) for i in found)))
            return None
        return found[0]

    def channel(self, ref: int | str | None, path: str, category: bool) -> int | None:
        if ref is None:
            return None
        wanted = [c for c in self.guild.channels if c.type == discord.ChannelType.category] if category else \
            [c for c in self.guild.channels if c.type in TEXT_TYPES]
        found = self._match(ref, wanted, path)
        if found is None:
            other = [c for c in self.guild.channels if c not in wanted
                     and (c.id == ref if isinstance(ref, int) else c.name.casefold() == str(ref).casefold())]
            if len(other) == 1:  # it exists, but is the wrong kind: say so rather than "not found"
                variable, name = _variable(path), other[0].name
                self.problems[-1] = (t("{variable}: {name} is not a category", self.language, variable=variable,
                                       name=name) if category else
                                     t("{variable}: {name} is not a text channel", self.language, variable=variable,
                                       name=name))
        return found.id if found is not None else None

    def role(self, ref: int | str | None, path: str) -> int | None:
        if ref is None:
            return None
        found = self._match(ref, self.guild.roles, path)
        return found.id if found is not None else None

    def roles(self, refs: Iterable[int | str], path: str) -> frozenset[int]:
        return frozenset(i for i in (self.role(ref, path) for ref in refs) if i is not None)


def resolve(guild: Any, settings: Settings) -> tuple[Ids, list[str]]:
    r = _Resolver(guild, settings.language)
    channels = {name: r.channel(getattr(settings.channels, name), f"channels.{name}", True)
                for name in CATEGORY_CHANNELS}
    channels |= {name: r.channel(getattr(settings.channels, name), f"channels.{name}", False)
                 for name in TEXT_CHANNELS}
    roles = settings.roles
    ids = Ids(
        **channels,
        member=r.roles(roles.member, "roles.member"),
        mission_maker=r.role(roles.mission_maker, "roles.mission_maker"),
        announcement=r.role(roles.announcement, "roles.announcement"),
        signup=r.roles(roles.signup, "roles.signup"),
        permissions={name: r.roles(refs, f"permissions.{name}")
                     for name, refs in settings.permissions.model_dump().items()},
        dlc={key: role for key, ref in settings.dlc_roles.model_dump().items()
             if (role := r.role(ref, f"dlc_roles.{key}")) is not None},
    )
    return ids, r.problems
