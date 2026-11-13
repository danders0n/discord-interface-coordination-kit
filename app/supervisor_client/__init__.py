"""HTTP client for arma-server-supervisor (docs/PLAN-front-end.md; the routes are its ``docs/api/openapi-bot.json``).

The bot never imports the supervisor's code. It reads ``GET /api/system/info`` at start-up (the supervisor check)
and acts for members with ``X-Act-As: discord:<id>``. Errors carry the supervisor's own message and ``code``, which
the Discord side words for the member rather than showing a generic failure."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import aiohttp

from app.supervisor_client.sse import SseParser, StreamEvent

TIMEOUT = aiohttp.ClientTimeout(total=30)
UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=600)  # a mission of a few hundred MB, on the same machine
# The stream sends a keep-alive every 15 s: a minute of silence means the connection is dead.
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
ACT_AS_HEADER = "X-Act-As"

__all__ = ["ACT_AS_HEADER", "StreamEvent", "SupervisorClient", "SupervisorError", "SupervisorUnreachable",
           "SystemInfo"]


class SupervisorError(Exception):
    def __init__(self, status: int, message: str, body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.body = body

    @property
    def code(self) -> str | None:
        """What a program can act on (``unknown_person``, ``account_disabled``, ``roster_guard`` ...), if given."""
        code = self.body.get("code") if isinstance(self.body, dict) else None
        return code if isinstance(code, str) else None


class SupervisorUnreachable(SupervisorError):
    def __init__(self, message: str) -> None:
        super().__init__(0, message)


@dataclass
class SystemInfo:
    version: Any = None
    api_level: int = 0
    timezone: str | None = None
    utc_offset: str | None = None  # '+0100': the supervisor's zone now
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

    def _headers(self, act_as: int | None, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if act_as is not None:  # docs/PLAN-front-end.md, *Acting for members*
            headers[ACT_AS_HEADER] = f"discord:{int(act_as)}"
        return headers

    async def request(self, method: str, path: str, *, act_as: int | None = None, **kwargs: Any) -> Any:
        """One call; with ``act_as`` (a Discord id) in that member's name, the supervisor checking their rights."""
        session = await self._http()
        kwargs["headers"] = self._headers(act_as, kwargs.get("headers"))
        try:
            async with session.request(method, f"{self.url}{path}", **kwargs) as response:
                text = await response.text()
                try:
                    body = json.loads(text) if text else None
                except ValueError:
                    body = text
                if response.status >= 400:
                    raise SupervisorError(response.status, _message(body, response.reason), body)
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
                               timezone=body.get("timezone"), utc_offset=body.get("utc_offset"),
                               capabilities=frozenset(body.get("capabilities") or ()))
        self._info_read = True
        return self.info

    async def ensure_info(self) -> SystemInfo:
        """The capabilities, read once (the heartbeat loop reads them again after a reconnect)."""
        if not self._info_read:
            await self.refresh_info()
        return self.info

    async def heartbeat(self, name: str, interval_s: int) -> None:
        await self.request("POST", "/api/integrations/heartbeat", json={"name": name, "interval_s": interval_s})

    async def me(self, act_as: int | None = None) -> dict[str, Any]:
        """Who the call runs as: the bot's account, or with ``act_as`` the member (``permissions`` ...)."""
        return await self.request("GET", "/api/auth/me", act_as=act_as)

    # ---------------------------------------------------------------- the roster (docs/PLAN-front-end.md)

    async def push_roster(self, members: list[dict[str, Any]]) -> dict[str, Any]:
        """The whole guild (``discord_id``, ``username``, ``display_name``, ``roles``). 409 ``roster_guard``: held
        for staff, nothing changed."""
        return await self.request("PUT", "/api/members/roster", json={"members": members})

    async def push_roles(self, roles: list[dict[str, Any]]) -> dict[str, Any]:
        """The guild's roles (``id``, ``name``, ``position``, ``color``)."""
        return await self.request("PUT", "/api/members/roles", json={"roles": roles})

    async def push_member(self, discord_id: int, body: dict[str, Any]) -> dict[str, Any]:
        """One member joined, left (``in_guild: false``), was renamed or changed roles: applied at once."""
        return await self.request("PUT", f"/api/members/{int(discord_id)}", json=body)

    async def members(self, **params: Any) -> dict[str, Any]:
        """Persons on the roster (``q``, ``state``, ``account``, ``limit``, ``offset``): ``total``, ``members``."""
        query = {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in params.items() if v is not None}
        return await self.request("GET", "/api/members", params=query)

    # ---------------------------------------------------------------- a member's own Steam link (act-as)

    async def my_member(self, act_as: int) -> dict[str, Any]:
        """The member's person: ``steam_uid``, ``steam_state`` (unconfirmed / confirmed / None), ``nick``,
        ``squadxml_url`` ..."""
        return await self.request("GET", "/api/members/me", act_as=act_as)

    async def link_steam(self, act_as: int, steam: str) -> dict[str, Any]:
        """Link the member's own Steam ID (unconfirmed until staff confirm). 409 ``link_taken``; 400 ``link_invalid``."""
        return await self.request("PUT", "/api/members/me/steam", act_as=act_as, json={"steam": steam})

    async def unlink_steam(self, act_as: int) -> dict[str, Any]:
        return await self.request("DELETE", "/api/members/me/steam", act_as=act_as)

    # ---------------------------------------------------------------- notices

    async def notices(self, after: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        """New notices after ``after``, oldest first (expired ones left out)."""
        body = await self.request("GET", "/api/notices", params={"after": str(after), "limit": str(limit)})
        return body.get("notices", []) if isinstance(body, dict) else []

    async def notice_done(self, notice_id: int, state: str, reason: str | None = None) -> dict[str, Any]:
        return await self.request("POST", f"/api/notices/{int(notice_id)}/done",
                                  json={"state": state, "reason": reason})

    async def notice_claim(self, notice_id: int) -> dict[str, Any]:
        """A ``login_link`` notice's ``url`` (once; 410 after that). Never log it."""
        return await self.request("POST", f"/api/notices/{int(notice_id)}/claim")

    # ---------------------------------------------------------------- live events

    async def stream(self, kinds: tuple[str, ...], after: str | None = None) -> AsyncIterator[StreamEvent]:
        """``GET /api/events/stream``: ``hello`` first, then the events missed after ``after`` and live ones. Ends
        when the connection does; the caller reconnects with the last ``id``."""
        session = await self._http()
        params = {"kinds": ",".join(kinds)} | ({"after": after} if after else {})
        try:
            async with session.get(f"{self.url}/api/events/stream", params=params, headers=self._headers(None),
                                   timeout=STREAM_TIMEOUT) as response:
                if response.status >= 400:
                    text = await response.text()
                    try:
                        body = json.loads(text) if text else None
                    except ValueError:
                        body = text
                    raise SupervisorError(response.status, _message(body, response.reason), body)
                parser = SseParser()
                async for raw in response.content:
                    event = parser.feed(raw.decode("utf-8", "replace"))
                    if event is not None:
                        yield event
        except aiohttp.ClientError as exc:
            raise SupervisorUnreachable(f"The supervisor's live events at {self.url} broke off: {exc}") from exc
        except TimeoutError as exc:
            raise SupervisorUnreachable(f"The supervisor's live events at {self.url} fell silent") from exc

    # ---------------------------------------------------------------- operations (docs/PLAN-front-end.md)

    async def operation_changes(self, after: int, limit: int = 100) -> dict[str, Any]:
        """``GET /api/operations/changes``: ``seq`` (the newest), ``reset``, ``more``, and ``changes``, oldest
        first: ``{seq, operation}`` (the whole operation now) or ``{seq, deleted}`` (a tombstone)."""
        return await self.request("GET", "/api/operations/changes", params={"after": str(int(after)),
                                                                             "limit": str(int(limit))})

    async def operation(self, op_id: int, act_as: int | None = None) -> dict[str, Any]:
        """The whole operation; read for a member (``act_as``) it carries ``my`` (``organiser``, ``actions``)."""
        return await self.request("GET", f"/api/operations/{int(op_id)}", act_as=act_as)

    async def operations_meta(self) -> dict[str, Any]:
        """The kinds' names, the climates with their roles, the other groups, the Discord roles table,
        ``staff_roles`` (manage / approve), ``squadxml_url``, the zone, the limits."""
        return await self.request("GET", "/api/operations/meta")

    async def operation_file(self, op_id: int, name: str) -> bytes:
        """A file of the operation by the name its ``files`` give (``preset.html``, ``map.png`` ...)."""
        session = await self._http()
        url = f"{self.url}/api/operations/{int(op_id)}/files/{_segment(name)}"
        try:
            async with session.get(url, headers=self._headers(None), timeout=UPLOAD_TIMEOUT) as response:
                if response.status >= 400:
                    text = await response.text()
                    try:
                        body = json.loads(text) if text else None
                    except ValueError:
                        body = text
                    raise SupervisorError(response.status, _message(body, response.reason), body)
                return await response.read()
        except aiohttp.ClientError as exc:
            raise SupervisorUnreachable(f"The supervisor at {self.url} could not be reached: {exc}") from exc
        except TimeoutError as exc:
            raise SupervisorUnreachable(f"The supervisor at {self.url} did not answer in time") from exc

    async def set_operation_discord(self, op_id: int, **plumbing: str | None) -> dict[str, Any]:
        """The bot's plumbing (``channel_url``, ``thread_url``): fields left out stay, None clears one."""
        return await self.request("PUT", f"/api/operations/{int(op_id)}/discord", json=plumbing)

    # ---------------------------------------------------------------- sign-ups and offers (act-as)
    # Each answers the operation as the member now sees it, plus ``outcome`` (``missing_dlc``: Creator DLC names
    # their roles don't show; a warning). Refusals carry a ``code`` (app/operations/words.py words them).

    async def take_slot(self, act_as: int, op_id: int, slot_id: int) -> dict[str, Any]:
        """Take the slot if it is still free (one atomic update), moving from the member's other place."""
        return await self.request("POST", f"/api/operations/{int(op_id)}/slots/{int(slot_id)}/take", act_as=act_as)

    async def answer(self, act_as: int, op_id: int, status: str) -> dict[str, Any]:
        """``maybe``, ``cant``, ``reserve`` or ``coming`` (Zeus and training)."""
        return await self.request("PUT", f"/api/operations/{int(op_id)}/signup", act_as=act_as,
                                  json={"status": status})

    async def leave(self, act_as: int, op_id: int) -> dict[str, Any]:
        """Off the member's slot and every list."""
        return await self.request("DELETE", f"/api/operations/{int(op_id)}/signup", act_as=act_as)

    async def accept_offer(self, act_as: int, op_id: int, offer_id: int) -> dict[str, Any]:
        return await self.request("POST", f"/api/operations/{int(op_id)}/offers/{int(offer_id)}/accept", act_as=act_as)

    async def decline_offer(self, act_as: int, op_id: int, offer_id: int) -> dict[str, Any]:
        return await self.request("POST", f"/api/operations/{int(op_id)}/offers/{int(offer_id)}/decline", act_as=act_as)

    async def offer_delivered(self, op_id: int, offer_id: int, via: str) -> dict[str, Any]:
        """The bot's own account: the offer reached the member (``dm`` or ``thread``); its 30 minutes start now."""
        return await self.request("POST", f"/api/operations/{int(op_id)}/offers/{int(offer_id)}/delivered",
                                  json={"via": via})

    # ---------------------------------------------------------------- organising (act-as)
    # Writes that change what the panel showed send ``If-Match: <revision>``; a stale one is 409 ``stale`` (with the
    # operation as it is). ``revision`` None sends no header (no check).

    @staticmethod
    def _if_match(revision: int | None) -> dict[str, str]:
        return {"If-Match": f'"{int(revision)}"'} if revision is not None else {}

    async def operations(self, act_as: int, *, upcoming: bool = True, status: str | None = None,
                         limit: int = 200) -> list[dict[str, Any]]:
        """The operations the member may see (short form: counts and the member's ``my``), soonest first."""
        params = {"upcoming": "true" if upcoming else "false", "limit": str(int(limit))}
        if status:
            params["status"] = status
        return await self.request("GET", "/api/operations", act_as=act_as, params=params)

    async def create_operation(self, act_as: int, body: dict[str, Any]) -> dict[str, Any]:
        """A draft (``title``, ``kind``, zone-aware ``assembly_at`` / ``start_at``, ``duration_minutes`` ...)."""
        return await self.request("POST", "/api/operations", act_as=act_as, json=body)

    async def update_operation(self, act_as: int, op_id: int, fields: dict[str, Any],
                               revision: int | None) -> dict[str, Any]:
        """Change details (an explicit None clears a field). ``proposal``: new times became a request."""
        return await self.request("PATCH", f"/api/operations/{int(op_id)}", act_as=act_as, json=fields,
                                  headers=self._if_match(revision))

    async def set_opord(self, act_as: int, op_id: int, paragraphs: dict[str, str], revision: int | None) -> dict[str, Any]:
        return await self.request("PUT", f"/api/operations/{int(op_id)}/opord", act_as=act_as, json=paragraphs,
                                  headers=self._if_match(revision))

    async def set_organisers(self, act_as: int, op_id: int, person_ids: list[int],
                             revision: int | None) -> dict[str, Any]:
        return await self.request("PUT", f"/api/operations/{int(op_id)}/organisers", act_as=act_as,
                                  json={"person_ids": [int(p) for p in person_ids]}, headers=self._if_match(revision))

    async def operation_step(self, act_as: int, op_id: int, step: str) -> dict[str, Any]:
        """``publish``, ``submit`` (send for approval) or ``withdraw`` (the request)."""
        assert step in ("publish", "submit", "withdraw")
        return await self.request("POST", f"/api/operations/{int(op_id)}/{step}", act_as=act_as)

    async def cancel_operation(self, act_as: int, op_id: int, reason: str) -> Any:
        """Cancelled (a draft never published is deleted: the answer is then empty)."""
        return await self.request("POST", f"/api/operations/{int(op_id)}/cancel", act_as=act_as,
                                  json={"reason": reason[:500]})

    async def delete_operation(self, act_as: int, op_id: int) -> Any:
        return await self.request("DELETE", f"/api/operations/{int(op_id)}", act_as=act_as)

    async def copy_operation(self, act_as: int, op_id: int, **body: Any) -> dict[str, Any]:
        """A new draft from this one (a week later unless ``assembly_at`` / ``start_at`` are given)."""
        return await self.request("POST", f"/api/operations/{int(op_id)}/copy", act_as=act_as, json=body)

    async def groups(self, act_as: int) -> list[dict[str, Any]]:
        return await self.request("GET", "/api/operations/groups", act_as=act_as)

    async def add_group(self, act_as: int, acronym: str, name: str) -> dict[str, Any]:
        return await self.request("POST", "/api/operations/groups", act_as=act_as,
                                  json={"acronym": acronym, "name": name})

    # ---------------------------------------------------------------- the ORBAT and its templates (act-as)

    async def orbat(self, act_as: int, op_id: int) -> dict[str, Any]:
        """``text`` (the ORBAT in its text form) and ``revision``."""
        return await self.request("GET", f"/api/operations/{int(op_id)}/orbat", act_as=act_as)

    async def set_orbat(self, act_as: int, op_id: int, text: str, revision: int | None) -> dict[str, Any]:
        """Replace it; ``outcome``: ``squads`` (name, slots) and ``displaced`` (moved to the reserve list). 422
        ``bad_orbat`` with ``problems``."""
        return await self.request("PUT", f"/api/operations/{int(op_id)}/orbat", act_as=act_as, json={"text": text},
                                  headers=self._if_match(revision))

    async def templates(self, act_as: int) -> list[dict[str, Any]]:
        """Every ORBAT template (``id``, ``name``, ``text``, ``owner``, ``mine``)."""
        return await self.request("GET", "/api/operations/templates", act_as=act_as)

    async def save_template(self, act_as: int, name: str, text: str) -> dict[str, Any]:
        """New, or the member's own of that name overwritten."""
        return await self.request("POST", "/api/operations/templates", act_as=act_as, json={"name": name, "text": text})

    async def rename_template(self, act_as: int, template_id: int, name: str) -> dict[str, Any]:
        return await self.request("PATCH", f"/api/operations/templates/{int(template_id)}", act_as=act_as,
                                  json={"name": name})

    async def delete_template(self, act_as: int, template_id: int) -> Any:
        return await self.request("DELETE", f"/api/operations/templates/{int(template_id)}", act_as=act_as)

    async def slot_action(self, act_as: int, op_id: int, slot_id: int, action: str,
                          person_id: int | None = None) -> dict[str, Any]:
        """An organiser's: ``lock``, ``unlock``, ``remove`` (the holder) or ``assign`` (``person_id``, locked)."""
        path = f"/api/operations/{int(op_id)}/slots/{int(slot_id)}"
        if action in ("lock", "unlock"):
            return await self.request("PUT", f"{path}/lock", act_as=act_as, json={"locked": action == "lock"})
        if action == "remove":
            return await self.request("DELETE", f"{path}/holder", act_as=act_as)
        return await self.request("POST", f"{path}/assign", act_as=act_as, json={"person_id": int(person_id)})

    # ---------------------------------------------------------------- files (act-as)

    async def upload(self, act_as: int, op_id: int, slot: str, filename: str, content: bytes) -> dict[str, Any]:
        """``preset``, ``mission`` or an image slot (``map``, ``banner``): the operation, with the file's report
        (``files.preset``, ``mission.report``, ``warnings``)."""
        path = {"preset": "preset", "mission": "mission"}.get(slot, f"images/{_segment(slot)}")
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename, content_type="application/octet-stream")
        return await self.request("PUT", f"/api/operations/{int(op_id)}/{path}", act_as=act_as, data=form,
                                  timeout=UPLOAD_TIMEOUT)

    async def pick_mission(self, act_as: int, op_id: int, name: str) -> dict[str, Any]:
        """A mission already in the server's missions folder."""
        return await self.request("POST", f"/api/operations/{int(op_id)}/mission/pick", act_as=act_as,
                                  json={"name": name})

    async def missions(self, act_as: int) -> list[dict[str, Any]]:
        """The server's missions (``name``, ``terrain``, ``size`` ...)."""
        return await self.request("GET", "/api/operations/missions", act_as=act_as)

    # ---------------------------------------------------------------- approval (act-as)

    async def approve_request(self, act_as: int, request_id: int) -> dict[str, Any]:
        """Publish, hold the host, or move to the proposed times. 409 when it would clash (it stays pending)."""
        return await self.request("POST", f"/api/operations/requests/{int(request_id)}/approve", act_as=act_as)

    async def reject_request(self, act_as: int, request_id: int, reason: str) -> dict[str, Any]:
        return await self.request("POST", f"/api/operations/requests/{int(request_id)}/reject", act_as=act_as,
                                  json={"reason": reason[:500]})


def _segment(value: Any) -> str:
    """One path segment from outside (a slug someone typed, a file name): quoted whole, so ``/``, ``..``, ``?`` and
    ``#`` in it can't lead the request to another route."""
    return quote(str(value), safe="")


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
