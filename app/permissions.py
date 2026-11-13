"""Who may use the bot's own commands (docs/PLAN-front-end.md, *What stays, what goes*): the role -> permission
mapping from `.env`, now only *bot admin*. Every other right is the supervisor's: a member's actions go to it in their
name (act-as) and it decides.

Works on role ids against the resolved configuration (app/resolve.py), so the services don't need discord.py."""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum

from app.resolve import Ids


class Perm(StrEnum):
    BOT_ADMIN = "bot_admin"


class Access:
    """One member's rights, from their role ids. Before the configuration is resolved nobody holds anything."""

    def __init__(self, ids: Ids, role_ids: Iterable[int]) -> None:
        self.ids = ids
        self.role_ids = frozenset(role_ids)

    def has(self, perm: Perm) -> bool:
        return not self.role_ids.isdisjoint(self.ids.permissions.get(perm.value, ()))
