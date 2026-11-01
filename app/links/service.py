"""Steam links (docs/PLAN.md, *Steam link and squad.xml*), without discord.py.

- A link is a member's Steam64 ID and their squad.xml nick. One Steam ID belongs to one member: a duplicate is
  refused with its owner, for the staff log. Accepted forms are the 17-digit ID and
  ``steamcommunity.com/profiles/<id>``; a custom ``/id/<name>`` address is refused (no Steam Web API, no key).
- A link whose member has no rank role (or left) since ``rank_lost_at`` is deleted N days later; regaining a rank
  in between clears it. :meth:`LinkService.reconcile` sets the times from the server at start-up.
- Members with a rank and no link are reminded every N days (``link_reminder``), at most so many per run; the
  first rollout counts as a reminder for everyone then, so the DMs start N days later. A member's first sign-up
  without a link asks once.
- :func:`import_old` reads the old bot's ``data/steam_ids.json`` (``python -m app import-old-links``).

Refusals are :class:`LinkError` codes the Discord layer words."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import delete, select

from app.db import Database, state
from app.db.models import LinkReminder, SteamLink

STEAM64 = re.compile(r"7656119\d{10}")
PROFILE = re.compile(r"steamcommunity\.com/profiles/(\d+)", re.IGNORECASE)
CUSTOM = re.compile(r"steamcommunity\.com/id/", re.IGNORECASE)
MAX_NICK = 50
ROLLOUT_KEY = "links_rollout"


class Problem(StrEnum):
    BAD_ID = "bad_id"
    CUSTOM_URL = "custom_url"  # steamcommunity.com/id/<name>: the bot can't turn it into an ID
    BAD_NICK = "bad_nick"
    TAKEN = "taken"  # the Steam ID belongs to another member (``owner_id``)


class LinkError(Exception):
    def __init__(self, problem: Problem, owner_id: int | None = None) -> None:
        super().__init__(problem.value)
        self.problem, self.owner_id = problem, owner_id


def steam_id(text: str) -> str:
    """The Steam64 ID in what a member typed: the ID itself or a profile address."""
    text = text.strip().strip("<>")
    if CUSTOM.search(text):
        raise LinkError(Problem.CUSTOM_URL)
    found = PROFILE.search(text)
    candidate = found.group(1) if found else text
    if not STEAM64.fullmatch(candidate):
        raise LinkError(Problem.BAD_ID)
    return candidate


def nick(text: str) -> str:
    cleaned = " ".join(text.split())
    if not 1 <= len(cleaned) <= MAX_NICK or any(ord(c) < 32 for c in cleaned):
        raise LinkError(Problem.BAD_NICK)
    return cleaned


@dataclass(frozen=True)
class Linked:
    """What :meth:`LinkService.link` did: the link now, and what it was before (None: a new link)."""

    member_id: int
    steam_id: str
    nick: str
    before: tuple[str, str] | None  # (steam_id, nick)

    @property
    def changed(self) -> bool:
        return self.before != (self.steam_id, self.nick)


class LinkService:
    def __init__(self, db: Database, clock: Callable[[], datetime] | None = None) -> None:
        self.db = db
        self.clock = clock or (lambda: datetime.now(UTC))

    # ---------------------------------------------------------------- reading

    async def get(self, member_id: int) -> SteamLink | None:
        async with self.db.session() as session:
            return await session.get(SteamLink, member_id)

    async def all(self) -> list[SteamLink]:
        async with self.db.session() as session:
            return list((await session.execute(select(SteamLink))).scalars())

    # ---------------------------------------------------------------- linking

    async def link(self, member_id: int, steam_text: str, nick_text: str, by: int, has_rank: bool) -> Linked:
        """Link or relink ``member_id`` (``by``: who did it, the member or staff)."""
        uid, name = steam_id(steam_text), nick(nick_text)
        now = self.clock()
        async with self.db.session() as session, session.begin():
            owner = (await session.execute(select(SteamLink.member_id).where(
                SteamLink.steam_id == uid, SteamLink.member_id != member_id))).scalar_one_or_none()
            if owner is not None:
                raise LinkError(Problem.TAKEN, owner)
            row = await session.get(SteamLink, member_id)
            before = (row.steam_id, row.nick) if row is not None else None
            if row is None:
                row = SteamLink(member_id=member_id, steam_id=uid, nick=name, linked_by=by,
                                rank_lost_at=None if has_rank else now)
                session.add(row)
            else:
                row.steam_id, row.nick, row.linked_by = uid, name, by
                if has_rank:
                    row.rank_lost_at = None
            await session.execute(delete(LinkReminder).where(LinkReminder.member_id == member_id))
        return Linked(member_id, uid, name, before)

    async def remove(self, member_id: int) -> SteamLink | None:
        """Delete the link; returns what it was."""
        async with self.db.session() as session, session.begin():
            row = await session.get(SteamLink, member_id)
            if row is not None:
                await session.delete(row)
        return row

    # ---------------------------------------------------------------- ranks

    async def rank_changed(self, member_id: int, has_rank: bool) -> None:
        """A member gained or lost every rank role, or left (``has_rank`` False)."""
        async with self.db.session() as session, session.begin():
            row = await session.get(SteamLink, member_id)
            if row is None:
                return
            if has_rank:
                row.rank_lost_at = None
            elif row.rank_lost_at is None:
                row.rank_lost_at = self.clock()

    async def reconcile(self, ranked: set[int]) -> None:
        """At start-up: what happened while the bot was down (``ranked``: members on the server with a rank)."""
        now = self.clock()
        async with self.db.session() as session, session.begin():
            for row in (await session.execute(select(SteamLink))).scalars():
                if row.member_id in ranked:
                    row.rank_lost_at = None
                elif row.rank_lost_at is None:
                    row.rank_lost_at = now

    async def expire(self, days: int) -> list[SteamLink]:
        """Delete the links whose member has been without a rank for ``days``; returns them."""
        cutoff = self.clock() - timedelta(days=days)
        async with self.db.session() as session, session.begin():
            rows = list((await session.execute(select(SteamLink).where(SteamLink.rank_lost_at <= cutoff))).scalars())
            for row in rows:
                await session.delete(row)
        return rows

    # ---------------------------------------------------------------- reminders

    async def due_reminders(self, ranked: Iterable[int], every: timedelta, limit: int) -> list[int]:
        """Of the members with a rank, those without a link not reminded for ``every``: never reminded first."""
        ranked = set(ranked)
        cutoff = self.clock() - every
        async with self.db.session() as session:
            linked = set((await session.execute(select(SteamLink.member_id))).scalars())
            last = {row.member_id: row.last_at for row in (await session.execute(select(LinkReminder))).scalars()}
        due = [m for m in ranked - linked if last.get(m) is None or last[m] <= cutoff]
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

    async def ask_at_signup(self, member_id: int) -> bool:
        """True once, at the first sign-up of a member without a link."""
        async with self.db.session() as session, session.begin():
            if await session.get(SteamLink, member_id) is not None:
                return False
            row = await session.get(LinkReminder, member_id)
            if row is None:
                session.add(LinkReminder(member_id=member_id, asked_at_signup=True))
                return True
            if row.asked_at_signup:
                return False
            row.asked_at_signup = True
            return True

    async def rollout_done(self) -> bool:
        return await state.get(self.db, ROLLOUT_KEY) is not None

    async def mark_rollout(self, member_ids: Iterable[int]) -> None:
        """The first run's announcement: it counts as everyone's first reminder."""
        await self.reminded(list(member_ids))
        await state.put(self.db, ROLLOUT_KEY, {"at": self.clock().isoformat()})


async def import_old(db: Database, path: Path) -> tuple[int, list[str]]:
    """The old bot's ``steam_ids.json`` (Discord id -> ``steam_uid``, ``game_nick``) as links. Returns how many
    were imported and a line per entry skipped: a bad id or nick, a member already linked, a duplicate Steam ID."""
    data = json.loads(path.read_text(encoding="utf-8"))
    imported, skipped = 0, []
    links = LinkService(db)
    async with db.session() as session:
        linked = set((await session.execute(select(SteamLink.member_id))).scalars())
    for key, entry in data.items():
        entry = entry if isinstance(entry, dict) else {}
        if not str(key).isdigit():
            skipped.append(f"{key}: not a Discord id")
            continue
        if int(key) in linked:
            skipped.append(f"{key}: already linked")
            continue
        try:
            await links.link(int(key), str(entry.get("steam_uid", "")), str(entry.get("game_nick", "")), 0,
                             has_rank=True)  # (the bot sets the rank times from the server at start-up)
        except LinkError as exc:
            reason = {Problem.TAKEN: f"Steam ID {entry.get('steam_uid')} belongs to {exc.owner_id}",
                      Problem.BAD_NICK: f"bad nick {entry.get('game_nick')!r}"}.get(
                exc.problem, f"bad Steam ID {entry.get('steam_uid')!r}")
            skipped.append(f"{key}: {reason}")
            continue
        imported += 1
    return imported, skipped
