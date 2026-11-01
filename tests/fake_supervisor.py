"""A fake arma-server-supervisor (docs/PLAN.md, *Phases*): an aiohttp app speaking the routes the bot uses, with
the supervisor's error shape (``{"detail": ...}``) and the behaviour the plan relies on, as read in its code:

- an occurrence in its quiet period or under way keeps its times on ``PUT`` and refuses ``DELETE`` (409);
- a create with a ``managed_by`` / ``external_ref`` that exists returns that reservation (200), body ignored;
- overlapping active windows are refused (409);
- a start of another server while a reservation is under way answers 409 with ``reservation``.

Run on its own for the test Discord server: ``python -m tests.fake_supervisor [port]``."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiohttp import web

TOKEN = "test-token"
ALL = ["reservations", "integration_role", "managed_refs", "reservation_end_by_id", "reservation_profile",
       "events_stream", "attendance_window", "player_uid_lookup", "heartbeat"]


def _server(number: int, slug: str, state: str = "stopped", players: int = 0) -> dict[str, Any]:
    return {"server": {"id": number, "slug": slug, "name": slug.title(), "protected": False}, "state": state,
            "players_online": [{"name": f"player{i}"} for i in range(players)]}


@dataclass
class State:
    capabilities: list[str] = field(default_factory=lambda: list(ALL))
    has_system_info: bool = True
    heartbeats: list[dict[str, Any]] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    servers: list[dict[str, Any]] = field(default_factory=lambda: [_server(1, "server-1"), _server(2, "event")])
    reservations: dict[int, dict[str, Any]] = field(default_factory=dict)
    players: list[dict[str, Any]] = field(default_factory=list)  # player stats: uid, name, last_seen
    calls: list[tuple[str, str]] = field(default_factory=list)
    next_id: int = 1

    def server(self, ref: Any) -> dict[str, Any] | None:
        return next((s for s in self.servers if ref in (s["server"]["id"], s["server"]["slug"], str(s["server"]["id"]))), None)

    def under_way(self, r: dict[str, Any]) -> bool:
        return not r["ended"] and r["starts_at"] - timedelta(minutes=r["quiet_minutes"]) <= self.now < r["ends_at"]

    def current(self) -> dict[str, Any] | None:
        return next((r for r in self.reservations.values() if self.under_way(r)), None)


STATE = web.AppKey("state", State)


def _error(status: int, detail: Any, **extra: Any) -> web.Response:
    return web.json_response({"detail": detail, **extra}, status=status)


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive time")  # the real one reads it as local time: the bot must never send one
    return parsed.astimezone(UTC)


def _out(r: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in r.items() if k not in ("starts_at", "ends_at", "ended")}
    return out | {"starts_at": r["starts_at"].isoformat(), "ends_at": r["ends_at"].isoformat()}


def _occurrence(r: dict[str, Any], state: State) -> dict[str, Any]:
    return {"reservation_id": r["id"], "name": r["name"], "server_id": r["server_id"],
            "starts_at": r["starts_at"].isoformat(), "ends_at": r["ends_at"].isoformat(),
            "state": "ended" if r["ended"] else ("active" if state.now >= r["starts_at"] else "quiet")}


def make_app(state: State | None = None) -> web.Application:
    state = state or State()

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return _error(401, "Not authenticated")
        state.calls.append((request.method, request.path))
        return await handler(request)

    async def system_info(_request: web.Request) -> web.Response:
        if not state.has_system_info:
            return _error(404, "Not Found")
        return web.json_response({"version": {"version": "fake"}, "api_level": 1, "timezone": "CET",
                                  "capabilities": state.capabilities})

    async def heartbeat(request: web.Request) -> web.Response:
        body = await request.json()
        if not 10 <= int(body.get("interval_s", 0)) <= 3600:
            return _error(422, [{"msg": "interval_s out of range"}])
        state.heartbeats.append(body)
        return web.json_response({"name": body["name"], "silent": False})

    # ------------------------------------------------------------ servers

    def _status(request: web.Request) -> dict[str, Any] | None:
        return state.server(request.match_info["slug"])

    async def servers(_request: web.Request) -> web.Response:
        return web.json_response(state.servers)

    async def status(request: web.Request) -> web.Response:
        found = _status(request)
        return web.json_response(found) if found else _error(404, "Server not found")

    async def start(request: web.Request) -> web.Response:
        found = _status(request)
        if found is None:
            return _error(404, "Server not found")
        body = await request.json() if request.can_read_body else {}
        held = state.current()
        if held is not None and held["server_id"] != found["server"]["id"] and not body.get("override"):
            return _error(409, f"'{held['name']}' has the host until {held['ends_at']:%H:%M}",
                          reservation={"id": held["id"], "name": held["name"], "server_id": held["server_id"],
                                       "starts_at": held["starts_at"].isoformat(), "until": held["ends_at"].isoformat()})
        found["state"] = "running"
        return web.json_response({"run_id": 1, "status": found})

    async def stop(request: web.Request) -> web.Response:
        found = _status(request)
        if found is None:
            return _error(404, "Server not found")
        found["state"], found["players_online"] = "stopped", []
        return web.json_response(found)

    async def say(request: web.Request) -> web.Response:
        found = _status(request)
        if found is None:
            return _error(404, "Server not found")
        if found["state"] != "running":
            return _error(409, f"{found['server']['slug']} is not running")
        return web.json_response({"sent": (await request.json())["message"]})

    # ------------------------------------------------------------ reservations

    def _clash(r: dict[str, Any], starts: datetime, ends: datetime) -> dict[str, Any] | None:
        return next((o for o in state.reservations.values() if o["id"] != r.get("id") and o["enabled"]
                     and not o["ended"] and o["starts_at"] < ends and starts < o["ends_at"]), None)

    def _fields(body: dict[str, Any]) -> dict[str, Any] | web.Response:
        server = state.server(body.get("server_id"))
        if server is None:
            return _error(404, f"Server {body.get('server_id')} not found")
        if not 1 <= len(body.get("name") or "") <= 80:
            return _error(422, [{"msg": "name: 1-80 characters"}])
        try:
            starts, ends = _time(body["starts_at"]), _time(body["ends_at"])
        except (KeyError, ValueError) as exc:
            return _error(422, [{"msg": f"times: {exc}"}])
        if ("managed_by" in body) != ("external_ref" in body) or (body.get("managed_by") is None) != (body.get("external_ref") is None):
            return _error(422, "managed_by and external_ref go together")
        return {"name": body["name"], "server_id": server["server"]["id"], "starts_at": starts, "ends_at": ends,
                "quiet_minutes": body.get("quiet_minutes", 30), "warn_minutes": body.get("warn_minutes", 15),
                "enabled": body.get("enabled", True), "note": body.get("note"), "link": body.get("link"),
                "profile_id": body.get("profile_id")} | {k: body[k] for k in ("managed_by", "external_ref") if k in body}

    async def list_reservations(request: web.Request) -> web.Response:
        q = request.query
        return web.json_response([_out(r) for r in state.reservations.values()
                                  if all(r.get(k) == q[k] for k in ("managed_by", "external_ref") if k in q)])

    async def create(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("managed_by") is not None:
            for r in state.reservations.values():
                if (r.get("managed_by"), r.get("external_ref")) == (body["managed_by"], body.get("external_ref")):
                    return web.json_response(_out(r), status=200)
        fields = _fields(body)
        if isinstance(fields, web.Response):
            return fields
        clash = _clash({}, fields["starts_at"], fields["ends_at"])
        if clash is not None:
            return _error(409, f"Overlaps '{clash['name']}'")
        r = fields | {"id": state.next_id, "repeat": "once", "ended": False}
        state.next_id += 1
        state.reservations[r["id"]] = r
        return web.json_response(_out(r), status=201)

    def _get(request: web.Request) -> dict[str, Any] | None:
        return state.reservations.get(int(request.match_info["id"]))

    async def update(request: web.Request) -> web.Response:
        r = _get(request)
        if r is None:
            return _error(404, "Reservation not found")
        fields = _fields(await request.json())
        if isinstance(fields, web.Response):
            return fields
        if state.under_way(r):  # "an occurrence under way keeps its times and its server"
            for key in ("starts_at", "ends_at", "server_id"):
                fields.pop(key)
        clash = _clash(r, fields.get("starts_at", r["starts_at"]), fields.get("ends_at", r["ends_at"]))
        if clash is not None:
            return _error(409, f"Overlaps '{clash['name']}'")
        r.update(fields)
        return web.json_response(_out(r))

    async def delete(request: web.Request) -> web.Response:
        r = _get(request)
        if r is None:
            return _error(404, "Reservation not found")
        if state.under_way(r):
            return _error(409, f"'{r['name']}' is under way until {r['ends_at']:%H:%M}: end it first")
        del state.reservations[r["id"]]
        return web.Response(status=204)

    async def end_by_id(request: web.Request) -> web.Response:
        r = _get(request)
        if r is None:
            return _error(404, "Reservation not found")
        if not state.under_way(r):
            return _error(409, f"'{r['name']}' is not under way or about to begin")
        r["ended"] = True
        return web.json_response(_occurrence(r, state))

    async def current(_request: web.Request) -> web.Response:
        r = state.current()
        return web.json_response(_occurrence(r, state) if r else None)

    async def end_current(_request: web.Request) -> web.Response:
        r = state.current()
        if r is None:
            return _error(404, "No reservation is under way or about to begin")
        r["ended"] = True
        return web.json_response(_occurrence(r, state))

    # ------------------------------------------------------------ player stats

    async def players(request: web.Request) -> web.Response:
        """``uid=``: exactly that Steam ID; ``q=``: also part of a name or ID, as the real search."""
        uid, q = request.query.get("uid"), request.query.get("q", "")
        if uid is not None:
            found = [p for p in state.players if p["uid"] == uid]
        else:
            found = [p for p in state.players if q.casefold() in p["name"].casefold() or q in p["uid"]]
        return web.json_response({"total": len(found), "players": found})

    app = web.Application(middlewares=[auth])
    app[STATE] = state
    app.router.add_get("/api/system/info", system_info)
    app.router.add_post("/api/integrations/heartbeat", heartbeat)
    app.router.add_get("/api/servers", servers)
    app.router.add_get("/api/servers/{slug}/status", status)
    app.router.add_post("/api/servers/{slug}/start", start)
    app.router.add_post("/api/servers/{slug}/restart", start)
    app.router.add_post("/api/servers/{slug}/stop", stop)
    app.router.add_post("/api/servers/{slug}/say", say)
    app.router.add_get("/api/reservations", list_reservations)
    app.router.add_post("/api/reservations", create)
    app.router.add_get("/api/reservations/current", current)
    app.router.add_post("/api/reservations/current/end", end_current)
    app.router.add_put("/api/reservations/{id:\\d+}", update)
    app.router.add_delete("/api/reservations/{id:\\d+}", delete)
    app.router.add_post("/api/reservations/{id:\\d+}/end", end_by_id)
    app.router.add_get("/api/stats/players", players)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=int(sys.argv[1]) if len(sys.argv) > 1 else 8099)
