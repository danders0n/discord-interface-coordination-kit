"""Who played during an event, from the supervisor (docs/PLAN.md, *Supervisor: attendance*, *Plays*, *Counted*).

With ``attendance_window`` one ``GET /api/stats/attendance`` gives the plays and every player's mission time
(``mission_s``) in the window. Without it, the mission plays of the event server (numeric id, looked up in
``GET /api/servers``; newest first, no time filter, so paged until past the window and filtered here), then
``GET /api/stats/missions/{id}`` per play, summing ``seconds``. Both count **mission time**, so a result doesn't
depend on the supervisor's version. The attendance route takes at most 24 h: a longer window keeps its last 24 h."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.supervisor_client import SupervisorClient

MAX_WINDOW = timedelta(hours=24)
PAGE = 100


@dataclass(frozen=True)
class Play:
    id: int
    started_at: datetime
    ended_at: datetime | None  # None: still running
    end: str | None  # finished | next_mission | server_stopped | crashed | lost


@dataclass(frozen=True)
class Player:
    uid: str | None  # Steam64; None for a player only OCAP saw
    name: str
    seconds: int  # mission time in the window


@dataclass(frozen=True)
class Stats:
    plays: list[Play]
    players: list[Player]


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _play(row: dict) -> Play:
    return Play(int(row["id"]), _time(row["started_at"]), _time(row.get("ended_at")), row.get("end"))


def _overlaps(play: Play, start: datetime, end: datetime) -> bool:
    return play.started_at <= end and (play.ended_at is None or play.ended_at >= start)


class StatsReader:
    def __init__(self, client: SupervisorClient) -> None:
        self.client = client

    async def read(self, server: str, start: datetime, end: datetime, players: bool = True) -> Stats:
        """The plays overlapping the window, and (``players``) everyone's mission time in it."""
        await self.client.ensure_info()
        if self.client.info.has("attendance_window"):
            body = await self.client.attendance(server, max(start, end - MAX_WINDOW), end)
            return Stats([_play(p) for p in body.get("plays", [])],
                         [Player(p.get("uid"), p.get("name") or "", int(p.get("mission_s") or 0))
                          for p in body.get("players", [])])
        plays = await self._plays(await self._server_id(server), start, end)
        if not players:
            return Stats(plays, [])
        seconds: dict[str, int] = {}
        names: dict[str, str] = {}
        for play in plays:
            for p in (await self.client.mission_play(play.id)).get("players", []):
                key = p.get("uid") or f"name:{p.get('name')}"
                seconds[key] = seconds.get(key, 0) + int(p.get("seconds") or 0)
                names[key] = p.get("name") or names.get(key, "")
        return Stats(plays, [Player(None if key.startswith("name:") else key, names[key], s)
                             for key, s in seconds.items()])

    async def _server_id(self, server: str) -> int:
        for row in await self.client.servers():
            info = row.get("server", {})
            if server in (info.get("slug"), str(info.get("id"))):
                return int(info["id"])
        raise LookupError(f"The supervisor has no server {server}")

    async def _plays(self, server_id: int, start: datetime, end: datetime) -> list[Play]:
        found, offset = [], 0
        while True:
            rows = await self.client.mission_plays(server_id, offset, PAGE)
            plays = [_play(r) for r in rows]
            found += [p for p in plays if _overlaps(p, start, end)]
            # Plays on one server don't overlap: once one began before the window, the older pages can't reach it.
            if len(rows) < PAGE or any(p.started_at < start for p in plays):
                return sorted(found, key=lambda p: p.started_at)
            offset += PAGE
