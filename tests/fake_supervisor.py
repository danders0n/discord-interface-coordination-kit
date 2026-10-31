"""A fake arma-server-supervisor (docs/PLAN.md, *Phases*): an aiohttp app speaking the routes the bot uses, with
the supervisor's error shape (``{"detail": ...}``). Later phases add their routes here.

Run on its own for the test Discord server: ``python -m tests.fake_supervisor [port]``."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

TOKEN = "test-token"


@dataclass
class State:
    capabilities: list[str] = field(default_factory=lambda: ["reservations", "heartbeat"])
    has_system_info: bool = True
    heartbeats: list[dict[str, Any]] = field(default_factory=list)


STATE = web.AppKey("state", State)


def _error(status: int, detail: Any) -> web.Response:
    return web.json_response({"detail": detail}, status=status)


def make_app(state: State | None = None) -> web.Application:
    state = state or State()

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return _error(401, "Not authenticated")
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

    app = web.Application(middlewares=[auth])
    app[STATE] = state
    app.router.add_get("/api/system/info", system_info)
    app.router.add_post("/api/integrations/heartbeat", heartbeat)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=int(sys.argv[1]) if len(sys.argv) > 1 else 8099)
