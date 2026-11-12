"""The roster: the guild's members and roles pushed to the supervisor (docs/PLAN-front-end.md, *Roster*).

- The whole guild (roles first, then members) at start-up and daily: the ``roster_push`` action, key ``roster_push``.
  Scheduling it for *now* at start-up both pushes at once and keeps the daily rhythm.
- One member at once on a join, leave, rename or role change. If the supervisor can't be reached, a
  ``member_push:<id>`` action retries the latest state of that member; a later push that gets through drops it.
- The roles again when one is created, renamed or deleted (``roles_push`` retries those).
- A full push the supervisor holds for staff (409 ``roster_guard``) is not retried until the next daily one. The
  supervisor's ``roster_guard`` notice is what tells staff (``app.notices``), so the bot writes no note of its own.

It also carries the *unknown person* rule of *Acting for members*: a member the supervisor doesn't know yet (joined
seconds ago) is pushed on the spot and the call repeated once (:meth:`Roster.as_member`).

No discord.py here: the bot hands in the guild's members and roles as :class:`Member` / :class:`Role`."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TypeVar

from app.scheduler import Action, Scheduler
from app.supervisor_client import SupervisorClient, SupervisorError, SupervisorUnreachable

log = logging.getLogger(__name__)
T = TypeVar("T")

FULL, MEMBER, ROLES = "roster_push", "member_push", "roles_push"
DAILY = timedelta(days=1)
RETRY_FULL = timedelta(minutes=15)  # the supervisor down, or the member list not loaded yet
RETRY_ONE = timedelta(minutes=2)
GUARDED, PUSHED, UNREACHABLE, NOT_READY = "guarded", "pushed", "unreachable", "not_ready"


@dataclass(frozen=True)
class Member:
    discord_id: int
    username: str
    display_name: str = ""
    roles: tuple[int, ...] = ()

    def body(self, in_guild: bool = True) -> dict:
        """``PUT /api/members/{id}``'s body (the supervisor's ``MemberPush``)."""
        return {"username": self.username[:100], "display_name": self.display_name[:100],
                "roles": [str(r) for r in self.roles], "in_guild": in_guild}

    def roster_row(self) -> dict:
        """One of ``PUT /api/members/roster``'s ``members`` (``RosterMember``)."""
        return {"discord_id": str(self.discord_id), "username": self.username[:100],
                "display_name": self.display_name[:100], "roles": [str(r) for r in self.roles]}


@dataclass(frozen=True)
class Role:
    id: int
    name: str = ""
    position: int = 0
    color: int = 0

    def row(self) -> dict:
        return {"id": str(self.id), "name": self.name[:100], "position": self.position, "color": self.color}


class Roster:
    def __init__(self, client: SupervisorClient, scheduler: Scheduler,
                 members: Callable[[], Iterable[Member] | None], roles: Callable[[], Iterable[Role] | None],
                 member: Callable[[int], Member | None]) -> None:
        """``members`` / ``roles``: the guild's now, or None until Discord has sent them; ``member``: one member now,
        or None when not on the server."""
        self.client, self.scheduler = client, scheduler
        self.members, self.roles, self.member = members, roles, member
        scheduler.register(FULL, self._full)
        scheduler.register(MEMBER, self._one)
        scheduler.register(ROLES, self._roles)

    async def start(self) -> None:
        """Once connected: the whole guild now, then daily."""
        await self.scheduler.schedule(FULL, self.scheduler.clock(), key=FULL)

    # ---------------------------------------------------------------- the whole guild

    async def push_all(self) -> str:
        members, roles = self.members(), self.roles()
        if members is None or roles is None:
            return NOT_READY
        try:
            await self.client.push_roles([r.row() for r in roles])
            await self.client.push_roster([m.roster_row() for m in members])
        except SupervisorUnreachable as exc:
            log.warning("Roster push: %s", exc.message)
            return UNREACHABLE
        except SupervisorError as exc:
            if exc.status == 409 and exc.code == "roster_guard":
                log.warning("The supervisor held the roster push for staff: %s", exc.message)
                return GUARDED
            raise  # a refused token or a bad payload: the scheduler's ERROR reaches the staff log
        return PUSHED

    async def _full(self, _action: Action, now: datetime) -> datetime:
        outcome = await self.push_all()
        return now + (RETRY_FULL if outcome in (UNREACHABLE, NOT_READY) else DAILY)

    # ---------------------------------------------------------------- one member, the roles

    async def member_changed(self, member: Member) -> None:
        """Joined, renamed or changed roles."""
        await self._push(member.discord_id, member.body())

    async def member_left(self, member: Member) -> None:
        await self._push(member.discord_id, member.body(in_guild=False))

    async def _push(self, discord_id: int, body: dict) -> bool:
        """Push now; if the supervisor is down, queue the latest state. True when it got through."""
        key = f"{MEMBER}:{discord_id}"
        try:
            await self.client.push_member(discord_id, body)
        except SupervisorUnreachable:
            await self.scheduler.schedule(MEMBER, self.scheduler.clock() + RETRY_ONE, key=key,
                                          payload={"discord_id": discord_id, "body": body})
            return False
        except SupervisorError as exc:  # refused: retrying the same won't help; the daily push repairs
            log.warning("Pushing member %s to the supervisor was refused: %s", discord_id, exc.message)
            return False
        await self.scheduler.cancel(key)  # an older state waiting there would undo this one
        return True

    async def _one(self, action: Action, now: datetime) -> datetime | None:
        discord_id = int(action.payload["discord_id"])
        try:
            await self.client.push_member(discord_id, action.payload["body"])
        except SupervisorUnreachable:
            return now + RETRY_ONE
        except SupervisorError as exc:
            log.warning("Pushing member %s to the supervisor was refused: %s", discord_id, exc.message)
        return None

    async def roles_changed(self) -> None:
        roles = self.roles()
        if roles is None:
            return
        try:
            await self.client.push_roles([r.row() for r in roles])
        except SupervisorUnreachable:
            await self.scheduler.schedule(ROLES, self.scheduler.clock() + RETRY_ONE, key=ROLES)
            return
        except SupervisorError as exc:
            log.warning("Pushing the roles to the supervisor was refused: %s", exc.message)
            return
        await self.scheduler.cancel(ROLES)

    async def _roles(self, _action: Action, now: datetime) -> datetime | None:
        roles = self.roles()
        if roles is None:
            return now + RETRY_ONE
        try:
            await self.client.push_roles([r.row() for r in roles])
        except SupervisorUnreachable:
            return now + RETRY_ONE
        return None

    # ---------------------------------------------------------------- acting for a member

    async def as_member(self, discord_id: int, call: Callable[[int], Awaitable[T]]) -> T:
        """``call(discord_id)`` (an act-as call). If the supervisor doesn't know the member yet, push them and try
        once more; any other refusal, or a second ``unknown_person``, goes to the caller."""
        try:
            return await call(discord_id)
        except SupervisorError as exc:
            if exc.code != "unknown_person":
                raise
            member = self.member(discord_id)
            if member is None or not await self._push(discord_id, member.body()):
                raise
        return await call(discord_id)
