"""Who may do what (docs/PLAN.md, *Who may do what*): the role -> permission mapping from `.env`.

Works on role ids, so the services don't need discord.py. Per-event rights (creator, co-organisers) are checked by
the events service on top of these."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from app.settings import Settings


class Perm(StrEnum):
    APPROVE_EVENTS = "approve_events"
    MANAGE_ANY_EVENT = "manage_any_event"
    SERVER_CONTROL = "server_control"
    SERVER_MESSAGE = "server_message"
    SERVER_OVERRIDE = "server_override"
    MANAGE_LINKS = "manage_links"
    BOT_ADMIN = "bot_admin"


# "The administration" in *Who sees what*: they see other members' details.
ADMINISTRATION = (Perm.MANAGE_ANY_EVENT, Perm.APPROVE_EVENTS, Perm.BOT_ADMIN)


class Access:
    """One member's rights, from their role ids."""

    def __init__(self, settings: Settings, role_ids: Iterable[int]) -> None:
        self.settings = settings
        self.role_ids = frozenset(role_ids)

    def has(self, perm: Perm) -> bool:
        return not self.role_ids.isdisjoint(getattr(self.settings.permissions, perm.value))

    def any(self, *perms: Perm) -> bool:
        return any(self.has(p) for p in perms)

    @property
    def is_member(self) -> bool:
        return self.settings.roles.member in self.role_ids

    @property
    def is_mission_maker(self) -> bool:
        return self.settings.roles.mission_maker in self.role_ids

    @property
    def may_sign_up(self) -> bool:
        return not self.role_ids.isdisjoint(self.settings.roles.signup)

    @property
    def may_create_events(self) -> bool:
        return self.is_member or self.is_mission_maker or self.has(Perm.MANAGE_ANY_EVENT)

    @property
    def publishes_without_approval(self) -> bool:
        """The roles of whoever presses *Publish* decide (*Approval*, *Who needs it*)."""
        return self.is_mission_maker or self.any(Perm.MANAGE_ANY_EVENT, Perm.APPROVE_EVENTS)

    @property
    def is_administration(self) -> bool:
        return self.any(*ADMINISTRATION)
