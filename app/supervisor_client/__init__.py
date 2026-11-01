"""HTTP client for arma-server-supervisor (docs/PLAN.md, *Supervisor changes this plan uses*).

The bot never imports the supervisor's code. It reads ``GET /api/system/info`` at start-up and after reconnecting,
and gates every newer feature on the capabilities listed there (a 404 means none). Errors carry the supervisor's
own message, which is shown to the Discord user rather than a generic failure. Phase 2 adds the reservations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp

TIMEOUT = aiohttp.ClientTimeout(total=30)
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600)  # a mission of a few hundred MB, on the same machine


class SupervisorError(Exception):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body


class SupervisorUnreachable(SupervisorError):
    def __init__(self, message: str) -> None:
        super().__init__(0, message)


class ReservationRefused(SupervisorError):
    """A start or restart a reservation refused (409 with ``reservation``: id, name, server_id, starts_at, until)."""

    @property
    def reservation(self) -> dict[str, Any]:
        return self.body["reservation"]


@dataclass
class SystemInfo:
    version: Any = None
    api_level: int = 0
    timezone: str | None = None
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def has(self, capability: str) -> bool:
        return capability in self.capabilities


class SupervisorClient:
    def __init__(self, url: str, token: str | None, session: aiohttp.ClientSession | None = None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self._session = session
        self.info = SystemInfo()
        self._info_read = False

    async def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=TIMEOUT)
        return self._session

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        session = await self._http()
        if self.token:
            kwargs["headers"] = {"Authorization": f"Bearer {self.token}"} | kwargs.get("headers", {})
        try:
            async with session.request(method, f"{self.url}{path}", **kwargs) as response:
                text = await response.text()
                try:
                    body = json.loads(text) if text else None
                except ValueError:
                    body = text
                if response.status >= 400:
                    error = ReservationRefused if isinstance(body, dict) and "reservation" in body else SupervisorError
                    raise error(response.status, _message(body, response.reason), body)
                return body
        except aiohttp.ClientError as exc:
            raise SupervisorUnreachable(f"The supervisor at {self.url} could not be reached: {exc}") from exc
        except TimeoutError as exc:
            raise SupervisorUnreachable(f"The supervisor at {self.url} did not answer in time") from exc

    async def refresh_info(self) -> SystemInfo:
        """Read what this supervisor offers; an older one without the route offers nothing new."""
        try:
            body = await self.request("GET", "/api/system/info")
        except SupervisorError as exc:
            if exc.status != 404:
                raise
            body = {}
        if not isinstance(body, dict):
            body = {}
        self.info = SystemInfo(version=body.get("version"), api_level=int(body.get("api_level") or 0),
                               timezone=body.get("timezone"), capabilities=frozenset(body.get("capabilities") or ()))
        self._info_read = True
        return self.info

    async def ensure_info(self) -> SystemInfo:
        """The capabilities, read once (the heartbeat loop reads them again after a reconnect)."""
        if not self._info_read:
            await self.refresh_info()
        return self.info

    async def heartbeat(self, name: str, interval_s: int) -> None:
        await self.request("POST", "/api/integrations/heartbeat", json={"name": name, "interval_s": interval_s})

    # ---------------------------------------------------------------- servers

    async def servers(self) -> list[dict[str, Any]]:
        """Every server's status: ``server`` (id, slug, name, protected ...), ``state``, ``players_online``."""
        return await self.request("GET", "/api/servers")

    async def server_status(self, slug: str) -> dict[str, Any]:
        return await self.request("GET", f"/api/servers/{slug}/status", params={"query": "false"})

    async def start(self, slug: str, override: bool = False) -> dict[str, Any]:
        return await self.request("POST", f"/api/servers/{slug}/start", json={"override": override})

    async def stop(self, slug: str) -> dict[str, Any]:
        return await self.request("POST", f"/api/servers/{slug}/stop", json={})

    async def restart(self, slug: str, override: bool = False) -> dict[str, Any]:
        return await self.request("POST", f"/api/servers/{slug}/restart", json={"override": override})

    async def say(self, slug: str, message: str) -> None:
        await self.request("POST", f"/api/servers/{slug}/say", json={"message": message})

    # ---------------------------------------------------------------- player stats

    async def player_by_uid(self, uid: str) -> dict[str, Any] | None:
        """The player with exactly this Steam ID (``name``, ``last_seen`` ...), or None when no server saw it.
        Without ``player_uid_lookup`` the broad ``q=`` search, which also matches names, filtered here."""
        await self.ensure_info()
        params = {"uid": uid} if self.info.has("player_uid_lookup") else {"q": uid, "limit": "50"}
        body = await self.request("GET", "/api/stats/players", params=params)
        players = body.get("players", []) if isinstance(body, dict) else []
        return next((p for p in players if p.get("uid") == uid), None)

    async def attendance(self, server: str, start: datetime, end: datetime) -> dict[str, Any]:
        """``attendance_window``: who was on ``server`` (id or slug) between the times (at most 24 h): ``players``
        with ``uid``, ``name``, ``mission_s`` ..., and the ``plays`` overlapping the window."""
        return await self.request("GET", "/api/stats/attendance", params={
            "server": server, "from": start.isoformat(), "to": end.isoformat()})

    async def mission_plays(self, server_id: int, offset: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """Mission plays on the server (numeric id), newest first, every one (``all``): the fallback."""
        body = await self.request("GET", "/api/stats/missions", params={
            "server": str(server_id), "all": "true", "limit": str(limit), "offset": str(offset)})
        return body.get("missions", []) if isinstance(body, dict) else []

    async def mission_play(self, play_id: int) -> dict[str, Any]:
        """One play with its ``players`` (``uid``, ``name``, ``seconds``)."""
        return await self.request("GET", f"/api/stats/missions/{play_id}")

    # ---------------------------------------------------------------- event profiles (phase 7)

    async def profiles(self) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/profiles")

    async def profile(self, profile_id: int) -> dict[str, Any]:
        return await self.request("GET", f"/api/profiles/{profile_id}")

    async def clone_profile(self, profile_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """``name``, and with ``managed_refs`` ``managed_by`` / ``external_ref`` (a known pair: that profile)."""
        return await self.request("POST", f"/api/profiles/{profile_id}/clone", json=body)

    async def patch_profile_config(self, profile_id: int, kind: str, changes: dict[str, Any]) -> dict[str, Any]:
        """Deep-merged into the stored config; lists are replaced (so ``missions`` becomes exactly these)."""
        return await self.request("PATCH", f"/api/profiles/{profile_id}/config/{kind}", json=changes)

    async def import_preset(self, profile_id: int, content: bytes, filename: str) -> dict[str, Any]:
        """The launcher preset as the profile's mods (``replace`` keeps the server-side ones), downloading only
        what is missing: a supervisor job (``job``)."""
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename, content_type="text/html")
        for name, value in (("mode", "replace"), ("download", "missing"), ("include_cdlc", "true"),
                            ("restart_server", "false"), ("include_dependencies", "true")):
            form.add_field(name, value)
        return await self.request("POST", f"/api/profiles/{profile_id}/mods/import-preset", data=form)

    async def delete_profile(self, profile_id: int) -> None:
        await self.request("DELETE", f"/api/profiles/{profile_id}")

    async def job(self, job_id: int) -> dict[str, Any]:
        """``status`` (queued, held, running, waiting_auth, succeeded, failed, cancelled), ``progress``, ``error``."""
        return await self.request("GET", f"/api/jobs/{job_id}")

    async def missions(self) -> list[dict[str, Any]]:
        """The missions on the server: ``name``, ``template``, ``title``, ``players``, ``profiles`` using it ..."""
        return await self.request("GET", "/api/missions")

    async def mission_report(self, name: str) -> dict[str, Any]:
        return await self.request("GET", f"/api/missions/{quote(name, safe='')}/report")

    async def upload_mission(self, filename: str, content: bytes, overwrite: bool) -> dict[str, Any]:
        """The saved mission with its inspection ``report``."""
        form = aiohttp.FormData()
        form.add_field("files", content, filename=filename, content_type="application/octet-stream")
        saved = await self.request("POST", "/api/missions", data=form, params={"overwrite": str(overwrite).lower()},
                                   timeout=UPLOAD_TIMEOUT)
        return saved[0]

    async def assign_profile(self, slug: str, profile_id: int | None) -> dict[str, Any]:
        """The profile the server starts with (the swap fallback without ``reservation_profile``)."""
        return await self.request("PATCH", f"/api/servers/{slug}", json={"profile_id": profile_id})

    # ---------------------------------------------------------------- reservations

    async def reservations(self, managed_by: str | None = None, external_ref: str | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in (("managed_by", managed_by), ("external_ref", external_ref)) if v is not None}
        return await self.request("GET", "/api/reservations", params=params)

    async def create_reservation(self, body: dict[str, Any]) -> dict[str, Any]:
        """201 for a new one; with a known ``managed_by`` / ``external_ref``, 200 and the existing one unchanged."""
        return await self.request("POST", "/api/reservations", json=body)

    async def update_reservation(self, reservation_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """``PUT`` replaces the whole reservation: send every field."""
        return await self.request("PUT", f"/api/reservations/{reservation_id}", json=body)

    async def delete_reservation(self, reservation_id: int) -> None:
        await self.request("DELETE", f"/api/reservations/{reservation_id}")

    async def end_reservation(self, reservation_id: int) -> bool:
        """End it now if it is under way (or in its quiet period). False when it wasn't."""
        try:
            if self.info.has("reservation_end_by_id"):
                await self.request("POST", f"/api/reservations/{reservation_id}/end")
                return True
            current = await self.request("GET", "/api/reservations/current")
            if not current or current.get("reservation_id") != reservation_id:
                return False
            await self.request("POST", "/api/reservations/current/end")
            return True
        except SupervisorError as exc:
            if exc.status in (404, 409):
                return False
            raise


def _message(body: Any, fallback: str | None) -> str:
    """FastAPI puts the message in ``detail`` (a string, an object with ``message``, or a validation list)."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return detail["message"]
    if isinstance(detail, list) and detail:
        return "; ".join(str(item.get("msg", item)) if isinstance(item, dict) else str(item) for item in detail)
    return fallback or "The supervisor refused the request"
