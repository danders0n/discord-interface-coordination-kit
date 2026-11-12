"""Steam links (docs/PLAN-front-end.md, *Commands and buttons*), without discord.py.

The supervisor owns the links and squad.xml (its ``docs/PLAN-operations.md``, *Steam link and squad.xml*): a member
links their own Steam ID through act-as (``PUT /api/members/me/steam``), unconfirmed until staff confirm it in the
app; staff manage every link there. What stays in the bot is delivery:

- reminders for members with a squad.xml rank and no link (the supervisor's ``GET /api/members?unlinked=true``),
  every N days, at most so many per run; the first rollout counts as everyone's first reminder; a member's first
  sign-up without a link asks once; staff can turn a member's reminders off (``muted``) until they link. The
  bookkeeping is the ``link_reminder`` table.
- the Steam IDs the bot's own attendance report matches (confirmed links only, as the supervisor counts them), until
  the attendance moves to the supervisor too.

Refusals the member can fix are :class:`LinkError` codes the Discord layer words; anything else is the supervisor's
:class:`~app.supervisor_client.SupervisorError`."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import delete, select

from app.db import Database, state
from app.db.models import LinkReminder
from app.supervisor_client import SupervisorClient, SupervisorError

ROLLOUT_KEY = "links_rollout"
CUSTOM = re.compile(r"steamcommunity\.com/id/", re.IGNORECASE)
PAGE = 10_000  # GET /api/members's limit: the whole guild in one read


class Problem(StrEnum):
    BAD_ID = "bad_id"
    CUSTOM_URL = "custom_url"  # steamcommunity.com/id/<name>: no Steam Web API, so no ID from it
    TAKEN = "taken"  # another member has the Steam ID (staff get a link_duplicate notice)


class LinkError(Exception):
    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.value)
        self.problem = problem


class LinkService:
    def __init__(self, db: Database, client: SupervisorClient, as_member: Callable[..., Any],
                 clock: Callable[[], datetime] | None = None) -> None:
        """``as_member``: :meth:`app.roster.service.Roster.as_member` (an act-as call, the member pushed first if the
        supervisor doesn't know them yet)."""
        self.db, self.client, self.as_member = db, client, as_member
        self.clock = clock or (lambda: datetime.now(UTC))

    # ---------------------------------------------------------------- the supervisor's links

    async def mine(self, member_id: int) -> dict[str, Any]:
        """The member's person as the supervisor has it: ``steam_uid``, ``steam_state``, ``nick``,
        ``squadxml_url``."""
        return await self.as_member(member_id, self.client.my_member)

    async def link(self, member_id: int, steam_text: str) -> dict[str, Any]:
        """Link the member's own Steam ID (unconfirmed until staff confirm it); returns their person."""
        try:
            person = await self.as_member(member_id, lambda who: self.client.link_steam(who, steam_text.strip()))
        except SupervisorError as exc:
            if exc.code == "link_taken":
                raise LinkError(Problem.TAKEN) from exc
            if exc.code == "link_invalid":
                raise LinkError(Problem.CUSTOM_URL if CUSTOM.search(steam_text) else Problem.BAD_ID) from exc
            raise
        await self.forget(member_id)
        return person

    async def unlink(self, member_id: int) -> tuple[str | None, dict[str, Any]]:
        """Remove the member's own link: (the Steam ID it was, or None when there was none; their person now)."""
        before = (await self.mine(member_id)).get("steam_uid")
        if before is None:
            return None, {}
        return before, await self.as_member(member_id, self.client.unlink_steam)

    async def person(self, member_id: int) -> dict[str, Any] | None:
        """Anyone's person (the bot's own read), or None when the supervisor doesn't know them."""
        body = await self.client.members(q=str(member_id), state="all")
        return next((p for p in body.get("members", []) if p.get("discord_id") == str(member_id)), None)

    async def unlinked(self) -> set[int]:
        """Members on the server with a squad.xml rank role and no Steam link."""
        body = await self.client.members(unlinked=True, limit=PAGE)
        return {int(p["discord_id"]) for p in body.get("members", []) if str(p.get("discord_id", "")).isdigit()}

    async def steam_ids(self) -> dict[int, str]:
        """Discord id -> confirmed Steam ID, for the attendance report."""
        body = await self.client.members(state="all", limit=PAGE)
        return {int(p["discord_id"]): p["steam_uid"] for p in body.get("members", [])
                if p.get("steam_uid") and p.get("steam_state") == "confirmed" and str(p.get("discord_id", "")).isdigit()}

    # ---------------------------------------------------------------- reminders

    async def due_reminders(self, unlinked: Iterable[int], every: timedelta, limit: int) -> list[int]:
        """Of the members with a rank and no link, those not reminded for ``every``, reminders on: never reminded
        first."""
        cutoff = self.clock() - every
        async with self.db.session() as session:
            rows = list((await session.execute(select(LinkReminder))).scalars())
        last = {row.member_id: row.last_at for row in rows}
        muted = {row.member_id for row in rows if row.muted}
        due = [m for m in set(unlinked) - muted if last.get(m) is None or last[m] <= cutoff]
        due.sort(key=lambda m: (last.get(m) is not None, last.get(m) or self.clock(), m))
        return due[:limit]

    async def reminded(self, member_ids: Iterable[int]) -> None:
        now = self.clock()
        async with self.db.session() as session, session.begin():
            for member_id in member_ids:
                row = await session.get(LinkReminder, member_id)
                if row is None:
                    session.add(LinkReminder(member_id=member_id, last_at=now, count=1))
                else:
                    row.last_at, row.count = now, row.count + 1

    async def never_reminded(self, member_id: int) -> bool:
        async with self.db.session() as session:
            row = await session.get(LinkReminder, member_id)
        return row is None or (row.last_at is None and not row.muted)

    async def forget(self, member_id: int) -> None:
        """The member linked: their reminders end."""
        async with self.db.session() as session, session.begin():
            await session.execute(delete(LinkReminder).where(LinkReminder.member_id == member_id))

    async def ask_at_signup(self, member_id: int) -> bool:
        """True once, at the first sign-up of a member without a link (the supervisor asked only when the bookkeeping
        says it may be the first time; unreachable: not now)."""
        async with self.db.session() as session:
            row = await session.get(LinkReminder, member_id)
            if row is not None and (row.asked_at_signup or row.muted):
                return False
        try:
            if (await self.mine(member_id)).get("steam_uid"):
                return False
        except SupervisorError:
            return False
        async with self.db.session() as session, session.begin():
            row = await session.get(LinkReminder, member_id)
            if row is None:
                session.add(LinkReminder(member_id=member_id, asked_at_signup=True))
            elif row.asked_at_signup:
                return False
            else:
                row.asked_at_signup = True
        return True

    async def mute(self, member_id: int, muted: bool) -> None:
        """Turn the member's reminders off (or on again) until they link."""
        async with self.db.session() as session, session.begin():
            row = await session.get(LinkReminder, member_id)
            if row is None:
                session.add(LinkReminder(member_id=member_id, muted=muted))
            else:
                row.muted = muted

    async def muted(self, member_id: int) -> bool:
        async with self.db.session() as session:
            row = await session.get(LinkReminder, member_id)
        return row is not None and row.muted

    async def reminders(self) -> dict[int, LinkReminder]:
        async with self.db.session() as session:
            return {row.member_id: row for row in (await session.execute(select(LinkReminder))).scalars()}

    async def rollout_done(self) -> bool:
        return await state.get(self.db, ROLLOUT_KEY) is not None

    async def mark_rollout(self, member_ids: Iterable[int]) -> None:
        """The first run's announcement: it counts as everyone's first reminder."""
        await self.reminded(list(member_ids))
        await state.put(self.db, ROLLOUT_KEY, {"at": self.clock().isoformat()})
