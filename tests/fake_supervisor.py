"""A fake arma-server-supervisor (docs/PLAN-front-end.md, *Tests and the fake supervisor*): an aiohttp app speaking
the routes of the bot's contract, with the supervisor's error shape (``{"detail": ..., "code": ...}``) and the
behaviour the bot relies on, as read in the supervisor's code:

- the roster: a full push sets who is in the guild; one that would make more than ``roster_guard`` persons with an
  account leave answers 409 ``roster_guard``, changes nothing and adds a ``roster_guard`` notice;
- ``X-Act-As: discord:<id>`` only on the contract's ``x-act-as`` routes (403 ``act_as_route``), for a person on the
  roster (403 ``unknown_person``) whose account isn't disabled (403 ``account_disabled``);
- notices: listed oldest first after ``after`` (only ``new`` ones; expired ones marked so), ``done``, and ``claim``
  of a login link's URL once (410 after that);
- operations: two counters (``revision`` for what organisers edit, checked by ``If-Match``: 409 ``stale`` with the
  operation; ``change_seq`` for every change, the plumbing too), the change feed with tombstones and ``reset``, a
  draft never published is deleted rather than cancelled, files by name, ``meta``;
- ``/api/events/stream``: ``hello``, then what :meth:`State.publish` sends (``operation`` on every change).

Every request on a contract route is checked against ``tests/contract/openapi-bot.json`` (``contract_check``):
unknown query names or a JSON body the real supervisor would refuse answer 422 with ``contract`` in the detail.

Tests change operations as the app would through :meth:`State.operation`, :meth:`State.change` and
:meth:`State.delete_operation`. Run on its own for the test Discord server: ``python -m tests.fake_supervisor
[port]``."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web

try:
    import contract_check
except ImportError:  # run as python -m tests.fake_supervisor
    from tests import contract_check

TOKEN = "test-token"
ALL = ["events_stream", "heartbeat", "members", "act_as", "notices", "steam_links", "operations", "operation_feed",
       "operation_files"]
TZ = ZoneInfo("Europe/Warsaw")
KINDS = ("internal", "guest", "hosted", "zeus", "training")
COMING = ("zeus", "training")
PARAGRAPHS = ("situation", "mission", "execution", "sustainment", "command")
KIND_NAMES = {"internal": "Internal", "guest": "Guest", "hosted": "Hosted", "zeus": "Zeus", "training": "Training"}
EDITABLE = ("kind", "title", "climate", "host_group_id", "group_ids", "author_person_id", "assembly_at", "start_at",
            "duration_minutes", "signup_opens_at", "signup_limit", "how_to_join", "dont_end_automatically", "public",
            "hold", "note")
TIMES = ("assembly_at", "start_at", "signup_opens_at")
PUBLISH = "operations.publish"


def iso(value: datetime | str | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if value.tzinfo is None:
        raise ValueError("naive time")
    return value.astimezone(UTC).isoformat()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("naive time")  # the real one reads it as local time: the bot must never send one
    return parsed.astimezone(UTC)


@dataclass
class State:
    capabilities: list[str] = field(default_factory=lambda: list(ALL))
    has_system_info: bool = True
    api_level: int = 2
    timezone: str = "CET"
    utc_offset: str | None = None  # None: the system info has none (the bot then can't compare)
    bot_permissions: list[str] = field(default_factory=lambda: ["integration.act_as"])
    heartbeats: list[dict[str, Any]] = field(default_factory=list)
    now: datetime = field(default_factory=lambda: datetime.now(UTC))
    calls: list[tuple[str, str]] = field(default_factory=list)
    # The front end: persons by Discord id (username, display_name, roles, in_guild, account: None or
    # {"username", "disabled"}, permissions: the app permissions of their account), the guild's roles, the guard
    # (None: off), the notices, the login links' URLs by notice id (popped when claimed), the act-as calls made
    # (method, path, discord id), and the open streams.
    persons: dict[str, dict[str, Any]] = field(default_factory=dict)
    roles: list[dict[str, Any]] = field(default_factory=list)
    roster_guard: int | None = None
    rank_roles: set[str] = field(default_factory=set)  # the Discord roles marked as squad.xml ranks (unlinked=true)
    notices: list[dict[str, Any]] = field(default_factory=list)
    link_urls: dict[int, str] = field(default_factory=dict)
    acted: list[tuple[str, str, str]] = field(default_factory=list)
    streams: list[asyncio.Queue] = field(default_factory=list)
    stream_gap: bool = False  # what the next hello says
    down: bool = False  # True: every route answers 503 (a supervisor that is restarting)
    # Operations: stored as the fields the real one keeps (times as aware datetimes), drawn by ``out()``; the feed's
    # counter, the tombstones (seq, id, title, deleted_at) and the highest pruned seq; files by (operation, name).
    operations: dict[int, dict[str, Any]] = field(default_factory=dict)
    seq: int = 0
    tombstones: list[dict[str, Any]] = field(default_factory=list)
    pruned_seq: int = 0
    files: dict[tuple[int, str], bytes] = field(default_factory=dict)
    groups: list[dict[str, Any]] = field(default_factory=list)
    climates: list[dict[str, Any]] = field(default_factory=list)
    staff_roles: dict[str, list[str]] = field(default_factory=lambda: {"manage": [], "approve": []})
    acronym: str = "IBC"
    server_missions: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ the front end

    def person(self, discord_id: Any, username: str = "member", *, account: bool | dict[str, Any] = True,
               **extra: Any) -> dict[str, Any]:
        """Put a person on the roster (a test's shortcut for a push); ``account``: False for a guest, or the
        account as a dict."""
        if account is True:
            account = {"username": username, "disabled": False}
        row = {"id": len(self.persons) + 1, "discord_id": str(discord_id), "username": username,
               "display_name": username, "roles": [], "in_guild": True, "account": account or None,
               "permissions": []} | extra
        self.persons[str(discord_id)] = row
        return row

    def notice(self, kind: str, *, persons: list[Any] = (), data: dict[str, Any] | None = None,
               operation_id: int | None = None, expires_at: datetime | None = None) -> dict[str, Any]:
        """Add a notice, as the supervisor does, and wake the streams."""
        row = {"id": len(self.notices) + 1, "kind": kind, "operation_id": operation_id,
               "persons": [str(p) for p in persons], "data": data or {}, "created_at": self.now.isoformat(),
               "expires_at": (expires_at or self.now + timedelta(days=2)).isoformat(), "state": "new", "reason": None}
        self.notices.append(row)
        self.publish("notice", {"id": row["id"], "kind": kind, "operation_id": operation_id})
        return row

    def login_link(self, discord_id: Any, url: str = "https://app.example/ui/#/set-password/secret") -> dict[str, Any]:
        row = self.notice("login_link", persons=[discord_id], data={
            "link_id": len(self.link_urls) + 1, "account": self.persons.get(str(discord_id), {}).get("username"),
            "expires_at": (self.now + timedelta(hours=24)).isoformat()})
        self.link_urls[row["id"]] = url
        return row

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        self.event_seq = getattr(self, "event_seq", 0) + 1
        for queue in self.streams:
            queue.put_nowait((self.event_seq, kind, data))

    def close_streams(self) -> None:
        for queue in self.streams:
            queue.put_nowait(None)

    # ------------------------------------------------------------ operations, as the app changes them

    def _next(self) -> int:
        self.seq += 1
        return self.seq

    def _touch(self, op: dict[str, Any], actor: str, fields: list[str], action: str, revision: bool) -> None:
        op["change_seq"] = self._next()
        op["changed_by"], op["changed_fields"], op["changed_at"] = actor, sorted(set(fields)), self.now
        if revision:
            op["revision"] += 1
        self.publish("operation", {"id": op["id"], "key": f"OP-{op['id']}", "action": action,
                                   "revision": op["revision"], "seq": op["change_seq"], "changed_by": actor,
                                   "changed_fields": op["changed_fields"]})

    def operation(self, title: str = "Operation", *, assembly: datetime | None = None, start: datetime | None = None,
                  kind: str = "internal", status: str = "draft", by: str = "anna", **fields: Any) -> dict[str, Any]:
        """A new operation, as one made in the app (``status`` published: published at once)."""
        assembly = assembly or self.now + timedelta(days=3)
        op = {"id": max(self.operations, default=0) + 1, "kind": kind, "title": title, "status": "draft",
              "climate": "", "climate_role_id": None, "host_group_id": None, "group_ids": [], "created_by": by,
              "author_person_id": None, "organisers": [], "assembly_at": assembly,
              "start_at": start or assembly + timedelta(minutes=30), "duration_minutes": 180,
              "signup_opens_at": None, "signup_limit": None, "how_to_join": "", "dont_end_automatically": False,
              "public": True, "hold": "off" if kind == "guest" else "on", "note": "", "opord": {}, "files": {},
              "mission": {}, "discord": {}, "cancel_reason": None, "revision": 1, "change_seq": 0,
              "changed_by": by, "changed_fields": [], "changed_at": self.now, "created_at": self.now,
              "published_at": None, "ended_at": None}
        op |= fields
        self.operations[op["id"]] = op
        self._touch(op, by, ["created"], "created", revision=False)
        if status != "draft":
            self.change(op["id"], status=status, by=by)
        return op

    def change(self, op_id: int, *, by: str = "anna", revision: bool = True, **fields: Any) -> dict[str, Any]:
        """Change an operation as the app would: ``published`` sets ``published_at``, a new seq, an event."""
        op = self.operations[op_id]
        if fields.get("status") == "published" and op["published_at"] is None:
            op["published_at"] = self.now
        if fields.get("status") in ("cancelled", "ended"):
            op["ended_at"] = self.now
        op.update(fields)
        self._touch(op, by, list(fields), "updated", revision)
        return op

    def set_file(self, op_id: int, slot: str, content: bytes, name: str | None = None, **info: Any) -> None:
        """A file of the operation (``preset``, ``map``, ``banner``) as an upload in the app leaves it."""
        name = name or {"preset": "preset.html", "map": "map.png", "banner": "banner.png"}[slot]
        self.files[(op_id, name)] = content
        kind = "text/html" if name.endswith(".html") else "image/png"
        entry = {"file": name, "hash": hashlib.sha256(content).hexdigest(), "size": len(content), "type": kind} | info
        files = dict(self.operations[op_id]["files"]) | {slot: entry}
        self.change(op_id, files=files)

    def delete_operation(self, op_id: int) -> None:
        """A draft deleted (or a stale one cancelled): a tombstone in the feed."""
        op = self.operations.pop(op_id)
        seq = self._next()
        self.tombstones.append({"seq": seq, "id": op_id, "title": op["title"], "deleted_at": self.now})
        self.publish("operation", {"id": op_id, "key": f"OP-{op_id}", "action": "deleted", "revision": 0,
                                   "seq": seq, "changed_by": "anna", "changed_fields": ["deleted"]})

    def prune(self, before_seq: int) -> None:
        """The tombstones up to ``before_seq`` pruned: a feed read after an older seq answers ``reset``."""
        self.tombstones = [t for t in self.tombstones if t["seq"] > before_seq]
        self.pruned_seq = max(self.pruned_seq, before_seq)

    # ------------------------------------------------------------ drawing an operation

    def _person(self, person_id: int | None) -> dict[str, Any] | None:
        row = next((p for p in self.persons.values() if p["id"] == person_id), None) if person_id else None
        if row is None:
            return None
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"] or row["username"],
                "discord_id": row["discord_id"]}

    def _group(self, group_id: int | None) -> dict[str, Any] | None:
        return next((dict(g) for g in self.groups if g["id"] == group_id), None) if group_id else None

    def channel_name(self, op: dict[str, Any]) -> str:
        acronym = self.acronym or "op"
        if op["kind"] in COMING:
            acronym = op["kind"]
        elif op["kind"] == "guest" and op["host_group_id"]:
            acronym = (self._group(op["host_group_id"]) or {}).get("acronym", acronym)
        return f"{op['assembly_at'].astimezone(TZ):%d-%m}-{acronym}".lower()

    def organises(self, op: dict[str, Any], person: dict[str, Any] | None) -> bool:
        if person is None:
            return False
        account = (person.get("account") or {}).get("username")
        return op["created_by"] == account or person["id"] in op["organisers"]

    def actions(self, op: dict[str, Any], person: dict[str, Any]) -> list[str]:
        out, organiser = [], self.organises(op, person)
        if op["status"] in ("draft", "published") and organiser:
            out += ["edit", "opord", "files", "preset", "mission", "profile"]
        if op["status"] == "draft" and organiser and PUBLISH in person["permissions"]:
            out.append("publish")
        if op["status"] in ("draft", "pending", "published") and organiser:
            out.append("delete" if op["published_at"] is None else "cancel")
        return out + ["copy"]

    def out(self, op: dict[str, Any], person: dict[str, Any] | None = None, *, full: bool = True) -> dict[str, Any]:
        ends = op["start_at"] + timedelta(minutes=op["duration_minutes"])
        files = {name: dict(info) | {"url": f"api/operations/{op['id']}/files/{info['file']}"}
                 for name, info in op["files"].items()}
        data = {
            "id": op["id"], "key": f"OP-{op['id']}", "kind": op["kind"], "kind_name": KIND_NAMES[op["kind"]],
            "title": op["title"], "status": op["status"], "climate": op["climate"],
            "climate_role_id": op["climate_role_id"], "host_group": self._group(op["host_group_id"]),
            "groups": [g for g in map(self._group, op["group_ids"]) if g], "created_by": op["created_by"],
            "author": self._person(op["author_person_id"]),
            "organisers": [p for p in map(self._person, op["organisers"]) if p],
            "assembly_at": iso(op["assembly_at"]), "start_at": iso(op["start_at"]),
            "duration_minutes": op["duration_minutes"], "ends_at": iso(ends),
            "signup_opens_at": iso(op["signup_opens_at"]), "signup_limit": op["signup_limit"],
            "how_to_join": op["how_to_join"], "dont_end_automatically": op["dont_end_automatically"],
            "public": op["public"], "hold": op["hold"], "server": None if op["kind"] == "guest" else
            {"id": 2, "slug": "event", "name": "Event"}, "files": files, "mission": op["mission"] or None,
            "warnings": [], "window": {"starts_at": iso(op["assembly_at"] - timedelta(minutes=45)),
                                       "ends_at": iso(ends + timedelta(minutes=30)), "state": "planned",
                                       "hold": op["hold"] == "on"},
            "frozen": False, "channel_name": self.channel_name(op), "discord": dict(op["discord"]),
            "cancel_reason": op["cancel_reason"], "revision": op["revision"], "change_seq": op["change_seq"],
            "changed_by": op["changed_by"], "changed_fields": list(op["changed_fields"]),
            "changed_at": iso(op["changed_at"]), "created_at": iso(op["created_at"]),
            "published_at": iso(op["published_at"]), "ended_at": iso(op["ended_at"]), "note": op["note"],
            "opord": {p: (op["opord"].get(p, "") if full else bool(op["opord"].get(p))) for p in PARAGRAPHS},
        }
        if person is not None:
            data["my"] = {"organiser": self.organises(op, person), "actions": self.actions(op, person)}
        return data

    def may_see(self, op: dict[str, Any], person: dict[str, Any] | None) -> bool:
        return person is None or op["status"] not in ("draft", "pending") or self.organises(op, person)


STATE = web.AppKey("state", State)
PERSON = web.RequestKey("person", dict)
ACT_AS = re.compile(r"^discord:(\d{1,25})$")


def _error(status: int, detail: Any, **extra: Any) -> web.Response:
    return web.json_response({"detail": detail, **extra}, status=status)


def _contract_path(request: web.Request) -> str:
    """The route as the contract writes it: ``{discord_id:\\d{1,25}}`` -> ``{discord_id}``."""
    info = request.match_info.route.resource.get_info() if request.match_info.route.resource else {}
    path = info.get("formatter") or info.get("path") or request.path
    return re.sub(r"\{(\w+)(:[^}]*)?\}", r"{\1}", path)


def make_app(state: State | None = None) -> web.Application:
    state = state or State()

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return _error(401, "Not authenticated")
        state.calls.append((request.method, request.path))
        if state.down:
            return _error(503, "The supervisor is restarting")
        path = _contract_path(request)
        op = contract_check.operation(request.method, path)
        if op is not None:  # hold the request to the contract
            body = None
            if request.can_read_body and request.content_type == "application/json":
                text = await request.text()
                body = json.loads(text) if text else None
            found = contract_check.request_problems(request.method, path, dict(request.query), body)
            if found:
                return _error(422, f"contract: {'; '.join(found)}")
        act_as = request.headers.get("X-Act-As")
        if act_as is not None:
            if op is None or not op.get("x-act-as"):
                return _error(403, f"{request.method} {path} can't be done for a member: use the app",
                              code="act_as_route")
            match = ACT_AS.match(act_as)
            if match is None:
                return _error(400, "X-Act-As must be discord:<id>", code="act_as_invalid")
            person = state.persons.get(match.group(1))
            if person is None or not person["in_guild"]:
                return _error(403, "This member is not on the roster yet: try again in a minute",
                              code="unknown_person")
            if person["account"] is not None and person["account"]["disabled"]:
                return _error(403, "This member's account is disabled", code="account_disabled")
            state.acted.append((request.method, path, match.group(1)))
            request[PERSON] = person
        return await handler(request)

    async def system_info(_request: web.Request) -> web.Response:
        if not state.has_system_info:
            return _error(404, "Not Found")
        return web.json_response({"version": {"version": "fake"}, "api_level": state.api_level,
                                  "timezone": state.timezone, "utc_offset": state.utc_offset,
                                  "capabilities": state.capabilities})

    async def heartbeat(request: web.Request) -> web.Response:
        body = await request.json()
        if not 10 <= int(body.get("interval_s", 0)) <= 3600:
            return _error(422, [{"msg": "interval_s out of range"}])
        state.heartbeats.append(body)
        return web.json_response({"name": body["name"], "silent": False})

    # ------------------------------------------------------------ identity, roster, notices, stream

    async def me(request: web.Request) -> web.Response:
        person = request.get(PERSON)
        if person is None:
            return web.json_response({"username": "discord-bot", "role": "integration",
                                      "permissions": sorted(state.bot_permissions), "acting_via": None})
        account = person["account"] or {}
        return web.json_response({"username": account.get("username") or person["username"], "role": "member",
                                  "permissions": list(person["permissions"]), "via": "discord-bot",
                                  "discord_id": person["discord_id"]})

    def _mine(request: web.Request) -> dict[str, Any]:
        person = request[PERSON]
        return {"person_id": person["id"], "discord_id": person["discord_id"],
                "username": person["username"], "display_name": person["display_name"],
                "steam_uid": person.get("steam_uid"), "steam_state": person.get("steam_state"),
                "nick": person.get("nick"), "squadxml_url": "https://app.example/squad/squad.xml"}

    async def my_member(request: web.Request) -> web.Response:
        if PERSON not in request:
            return _error(404, "This account has no Discord member")
        return web.json_response(_mine(request))

    async def link_steam(request: web.Request) -> web.Response:
        if PERSON not in request:
            return _error(404, "This account has no Discord member")
        text = (await request.json())["steam"].strip()
        match = re.fullmatch(r"(?:https?://steamcommunity\.com/profiles/)?(7656119\d{10})/?", text)
        if match is None:
            return _error(400, "Steam ID: the 17-digit Steam64 ID (7656119...) or steamcommunity.com/profiles/<id>",
                          code="link_invalid")
        uid, person = match.group(1), request[PERSON]
        owner = next((p for p in state.persons.values() if p.get("steam_uid") == uid and p is not person), None)
        if owner is not None:
            state.notice("link_duplicate", persons=[person["discord_id"]], data={"steam_uid": uid})
            return _error(409, f"Steam ID {uid} is linked to another member", code="link_taken")
        if person.get("steam_uid") != uid:
            person.update(steam_uid=uid, steam_state="unconfirmed")
            state.notice("link_request", persons=[person["discord_id"]], data={"steam_uid": uid})
        return web.json_response(_mine(request))

    async def unlink_steam(request: web.Request) -> web.Response:
        if PERSON not in request:
            return _error(404, "This account has no Discord member")
        request[PERSON].update(steam_uid=None, steam_state=None)
        return web.json_response(_mine(request))

    def _apply(discord_id: str, member: dict[str, Any], in_guild: bool) -> dict[str, Any]:
        row = state.persons.get(discord_id) or {"id": len(state.persons) + 1, "discord_id": discord_id,
                                                "account": {"username": member["username"], "disabled": False},
                                                "permissions": []}
        row.update(username=member["username"], display_name=member.get("display_name", ""),
                   roles=list(member.get("roles", [])), in_guild=in_guild)
        state.persons[discord_id] = row
        return row

    async def push_roster(request: web.Request) -> web.Response:
        members = (await request.json())["members"]
        listed = {m["discord_id"] for m in members}
        leaving = [p for d, p in state.persons.items() if d not in listed and p["in_guild"] and p["account"]]
        if state.roster_guard is not None and len(leaving) > state.roster_guard:
            state.notice("roster_guard", data={"push_id": 1, "count": len(leaving),
                                               "accounts": [p["account"]["username"] for p in leaving]})
            return _error(409, f"The roster push would disable {len(leaving)} member accounts: it waits for staff",
                          code="roster_guard", push={"id": 1, "count": len(leaving)})
        for member in members:
            _apply(member["discord_id"], member, True)
        for person in state.persons.values():
            if person["discord_id"] not in listed:
                person["in_guild"] = False
        return web.json_response({"members": len(members), "report": {"left": [p["discord_id"] for p in leaving]}})

    async def push_roles(request: web.Request) -> web.Response:
        state.roles = (await request.json())["roles"]
        return web.json_response({"roles": len(state.roles)})

    async def push_member(request: web.Request) -> web.Response:
        body = await request.json()
        row = _apply(request.match_info["discord_id"], body, body.get("in_guild", True))
        return web.json_response({"report": {}, "person": row})

    async def list_members(request: web.Request) -> web.Response:
        q, kind = request.query.get("q", "").casefold(), request.query.get("state", "member")
        rows = [p for p in state.persons.values()
                if (kind == "all" or p["in_guild"] == (kind == "member"))
                and (not q or q in p["username"].casefold() or q in p["display_name"].casefold() or q == p["discord_id"])]
        if "account" in request.query:
            wanted = request.query["account"] == "true"
            rows = [p for p in rows if (p["account"] is not None) == wanted]
        if request.query.get("unlinked") == "true":  # a squad.xml rank and no link, on the server
            rows = [p for p in rows if p["in_guild"] and not p.get("steam_uid") and state.rank_roles & set(p["roles"])]
        return web.json_response({"total": len(rows), "members": rows})

    async def list_notices(request: web.Request) -> web.Response:
        after, limit = int(request.query.get("after", 0)), int(request.query.get("limit", 100))
        out = []
        for row in state.notices:
            if row["id"] <= after or row["state"] != "new":
                continue
            if datetime.fromisoformat(row["expires_at"]) <= state.now:
                row["state"] = "expired"
                continue
            out.append(row)
        return web.json_response({"notices": out[:limit]})

    def _notice(request: web.Request) -> dict[str, Any] | None:
        return next((n for n in state.notices if n["id"] == int(request.match_info["notice_id"])), None)

    async def notice_done(request: web.Request) -> web.Response:
        row = _notice(request)
        if row is None:
            return _error(404, f"Notice {request.match_info['notice_id']} not found")
        body = await request.json()
        row["state"], row["reason"] = body["state"], body.get("reason")
        return web.json_response(row)

    async def notice_claim(request: web.Request) -> web.Response:
        row = _notice(request)
        if row is None:
            return _error(404, f"Notice {request.match_info['notice_id']} not found")
        if row["kind"] != "login_link":
            return _error(400, "Only a login_link notice has a URL to claim")
        url = state.link_urls.pop(row["id"], None)
        if url is None:
            return _error(410, "This link was claimed already, replaced or used")
        return web.json_response({"url": url, "expires_at": row["data"]["expires_at"], "account": row["data"]["account"]})

    async def stream(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        queue: asyncio.Queue = asyncio.Queue()
        state.streams.append(queue)
        kinds = set(request.query.get("kinds", "").split(",")) - {""}
        try:
            hello = {"boot": "fake", "gap": state.stream_gap, "replayed": 0, "kinds": sorted(kinds)}
            state.stream_gap = False
            await response.write(f"event: hello\ndata: {json.dumps(hello)}\n\n".encode())
            while (item := await queue.get()) is not None:
                seq, kind, data = item
                if kinds and kind not in kinds:
                    continue
                body = {"kind": kind, "seq": seq, "server": None, "data": data}
                await response.write(f"id: fake:{seq}\nevent: {kind}\ndata: {json.dumps(body)}\n\n".encode())
        finally:
            state.streams.remove(queue)
        return response

    # ------------------------------------------------------------ operations: reference data and the feed

    def _person(request: web.Request) -> dict[str, Any] | None:
        return request.get(PERSON)

    def _actor(request: web.Request) -> str:
        person = _person(request)
        return (person["account"] or {}).get("username", person["username"]) if person else "discord-bot"

    async def meta(_request: web.Request) -> web.Response:
        return web.json_response({
            "api_level": state.api_level, "timezone": state.timezone, "utc_offset": state.utc_offset or "+0100",
            "kinds": [{"id": k, "name": KIND_NAMES[k]} for k in KINDS], "climates": state.climates,
            "groups": state.groups, "discord_roles": [], "staff_roles": state.staff_roles,
            "squadxml_url": "https://app.example/squad/squad.xml",
            "defaults": {"server": {"id": 2, "slug": "event", "name": "Event"}, "base_profile": None,
                         "duration_minutes": 180, "hold": "on"},
            "window": {"lead_minutes": 45, "buffer_minutes": 30, "quiet_minutes": 30, "warn_minutes": 15},
            "max_days_ahead": 120, "acronym": state.acronym, "paragraphs": list(PARAGRAPHS),
            "image_slots": ["map", "banner"], "limits": {"title": 100, "paragraph": 4000}})

    async def changes(request: web.Request) -> web.Response:
        after, limit = int(request.query.get("after", 0)), int(request.query.get("limit", 100))
        reset = 0 < after < state.pruned_seq or after > state.seq
        if reset:
            cutoff = state.now - timedelta(days=30)
            ops = sorted((o for o in state.operations.values()
                          if o["status"] in ("draft", "pending", "published") or o["changed_at"] > cutoff),
                         key=lambda o: o["change_seq"])
            items = [{"seq": o["change_seq"], "operation": state.out(o)} for o in ops[:limit]]
            return web.json_response({"seq": state.seq, "reset": True, "more": len(ops) > limit, "changes": items})
        merged = sorted([(o["change_seq"], "op", o) for o in state.operations.values() if o["change_seq"] > after]
                        + [(t["seq"], "stone", t) for t in state.tombstones if t["seq"] > after],
                        key=lambda item: item[0])
        items = []
        for seq, what, row in merged[:limit]:
            if what == "op":
                items.append({"seq": seq, "operation": state.out(row)})
            else:
                items.append({"seq": seq, "deleted": {"id": row["id"], "key": f"OP-{row['id']}", "title": row["title"],
                                                      "deleted_at": iso(row["deleted_at"])}})
        return web.json_response({"seq": state.seq, "reset": False, "more": len(merged) > limit, "changes": items})

    async def persons(request: web.Request) -> web.Response:
        q = request.query.get("q", "").casefold()
        rows = [p for p in state.persons.values() if p["in_guild"]
                and (not q or q in p["username"].casefold() or q in p["display_name"].casefold())]
        return web.json_response([{"id": p["id"], "username": p["username"], "display_name": p["display_name"]}
                                  for p in rows[:int(request.query.get("limit", 50))]])

    async def missions(_request: web.Request) -> web.Response:
        return web.json_response([{"name": name, "terrain": name.split(".")[-2] if name.count(".") >= 2 else "",
                                   "size": 1000} for name in sorted(state.server_missions)])

    async def list_groups(_request: web.Request) -> web.Response:
        return web.json_response(state.groups)

    async def add_group(request: web.Request) -> web.Response:
        body = await request.json()
        if any(g["acronym"].casefold() == body["acronym"].casefold() for g in state.groups):
            return _error(409, f"{body['acronym']} exists already", code="exists")
        group = {"id": len(state.groups) + 1, "acronym": body["acronym"], "name": body["name"]}
        state.groups.append(group)
        return web.json_response(group, status=201)

    async def rename_group(request: web.Request) -> web.Response:
        group = next((g for g in state.groups if g["id"] == int(request.match_info["group_id"])), None)
        if group is None:
            return _error(404, "Group not found", code="not_found")
        group.update({k: v for k, v in (await request.json()).items() if v is not None})
        return web.json_response(group)

    # ------------------------------------------------------------ operations

    def _get(request: web.Request) -> dict[str, Any] | web.Response:
        op = state.operations.get(int(request.match_info["op_id"]))
        if op is None or not state.may_see(op, _person(request)):
            return _error(404, f"OP-{request.match_info['op_id']} not found", code="not_found")
        return op

    def _reply(op: dict[str, Any], request: web.Request, status: int = 200) -> web.Response:
        return web.json_response(state.out(op, _person(request)), status=status, headers={"ETag": f'"{op["revision"]}"'})

    def _editable(op: dict[str, Any], request: web.Request) -> web.Response | None:
        """Organisers edit (the bot's own account: never, it has no operations.create); If-Match is checked."""
        person = _person(request)
        if person is None or not state.organises(op, person):
            return _error(403, f"Only the organisers of OP-{op['id']} may change it", code="not_allowed")
        if op["status"] not in ("draft", "published"):
            return _error(409, f"OP-{op['id']} is {op['status']}", code="locked")
        wanted = request.headers.get("If-Match")
        if wanted and wanted.strip().strip('"') != str(op["revision"]):
            return _error(409, f"OP-{op['id']} changed meanwhile", code="stale", operation=state.out(op, person))
        return None

    def _fields(body: dict[str, Any]) -> dict[str, Any] | web.Response:
        fields = {k: v for k, v in body.items() if k in EDITABLE}
        try:
            for name in TIMES:
                if fields.get(name) is not None:
                    fields[name] = _time(fields[name])
        except ValueError as exc:
            return _error(422, [{"msg": f"times: {exc}"}])
        return fields

    def _check_times(op: dict[str, Any]) -> web.Response | None:
        if op["start_at"] < op["assembly_at"]:
            return _error(422, "The start is before the assembly", code="start_before_assembly")
        if op["assembly_at"] <= state.now:
            return _error(422, "That time is over: the assembly must be in the future", code="past")
        return None

    async def list_operations(request: web.Request) -> web.Response:
        statuses = [s for s in request.query.get("status", "").split(",") if s]
        upcoming = request.query.get("upcoming") == "true"
        rows = [o for o in state.operations.values() if state.may_see(o, _person(request))
                and (not statuses or o["status"] in statuses)
                and (not upcoming or o["start_at"] + timedelta(minutes=o["duration_minutes"]) > state.now)]
        rows.sort(key=lambda o: o["start_at"], reverse=not upcoming)
        return web.json_response([state.out(o, _person(request), full=False)
                                  for o in rows[:int(request.query.get("limit", 200))]])

    async def create_operation(request: web.Request) -> web.Response:
        person = _person(request)
        if person is None:
            return _error(403, "Creating operations needs operations.create", code="forbidden")
        fields = _fields(await request.json())
        if isinstance(fields, web.Response):
            return fields
        if fields.get("kind") == "guest" and PUBLISH not in person["permissions"]:
            return _error(403, "A guest operation needs Publish operations", code="kind_not_allowed")
        draft = {"assembly_at": fields["assembly_at"], "start_at": fields["start_at"]}
        problem = _check_times(draft)
        if problem is not None:
            return problem
        op = state.operation(fields.pop("title"), assembly=fields.pop("assembly_at"), start=fields.pop("start_at"),
                             by=_actor(request), author_person_id=person["id"], **fields)
        return _reply(op, request, 201)

    async def get_operation(request: web.Request) -> web.Response:
        op = _get(request)
        return op if isinstance(op, web.Response) else _reply(op, request)

    async def update_operation(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        fields = _fields(await request.json())
        if isinstance(fields, web.Response):
            return fields
        trial = op | fields
        problem = _check_times(trial) if any(k in fields for k in ("assembly_at", "start_at")) else None
        if problem is not None:
            return problem
        state.change(op["id"], by=_actor(request), **fields)
        return _reply(op, request)

    async def delete_operation(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        if not state.organises(op, _person(request)):
            return _error(403, f"Only the organisers of OP-{op['id']} may delete it", code="not_allowed")
        if op["published_at"] is not None:
            return _error(409, f"OP-{op['id']} was published: cancel it instead", code="published")
        state.delete_operation(op["id"])
        return web.Response(status=204)

    async def set_opord(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        body = await request.json()
        state.change(op["id"], by=_actor(request), opord=dict(op["opord"]) | {k: v for k, v in body.items() if v is not None})
        return _reply(op, request)

    async def set_organisers(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        state.change(op["id"], by=_actor(request), organisers=list((await request.json())["person_ids"]))
        return _reply(op, request)

    async def publish(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        person = _person(request)
        if person is None or PUBLISH not in person["permissions"]:
            return _error(403, "Publishing needs Publish operations: send it for approval", code="needs_approval")
        if op["status"] != "draft":
            return _error(409, f"OP-{op['id']} is {op['status']}", code="not_draft")
        missing = [p for p in ("situation", "mission") if not op["opord"].get(p)]
        if missing:
            return _error(422, "Write the OPORD's Situation and Mission first", code="incomplete", missing=missing)
        state.change(op["id"], by=_actor(request), status="published")
        return _reply(op, request)

    async def cancel(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        if not state.organises(op, _person(request)):
            return _error(403, f"Only the organisers of OP-{op['id']} may cancel it", code="not_allowed")
        if op["published_at"] is None:
            state.delete_operation(op["id"])
            return web.json_response(None)
        state.change(op["id"], by=_actor(request), status="cancelled", cancel_reason=(await request.json()).get("reason") or None)
        return _reply(op, request)

    async def copy_operation(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        body = await request.json() if request.can_read_body else {}
        shift = timedelta(days=7)
        assembly = _time(body["assembly_at"]) if body.get("assembly_at") else op["assembly_at"] + shift
        start = _time(body["start_at"]) if body.get("start_at") else assembly + (op["start_at"] - op["assembly_at"])
        made = state.operation(body.get("title") or op["title"], assembly=assembly, start=start, kind=op["kind"],
                               by=_actor(request), opord=dict(op["opord"]), files=copy.deepcopy(op["files"]),
                               organisers=list(op["organisers"]), author_person_id=op["author_person_id"])
        return _reply(made, request, 201)

    async def set_discord(request: web.Request) -> web.Response:
        if PERSON in request:
            return _error(403, "The plumbing is the bot's own", code="act_as_route")
        op = state.operations.get(int(request.match_info["op_id"]))
        if op is None:
            return _error(404, f"OP-{request.match_info['op_id']} not found", code="not_found")
        plumbing = dict(op["discord"]) | await request.json()
        if plumbing != op["discord"]:
            state.change(op["id"], by="discord-bot", revision=False, discord=plumbing)
        return _reply(op, request)

    async def get_file(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        content = state.files.get((op["id"], request.match_info["name"]))
        if content is None:
            return _error(404, f"OP-{op['id']} has no file '{request.match_info['name']}'", code="not_found")
        return web.Response(body=content, content_type="text/html" if request.match_info["name"].endswith(".html")
                            else "image/png")

    async def _upload(request: web.Request, slot: str) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        form = await request.post()
        upload = form["file"]
        content = upload.file.read()
        if slot == "mission":
            state.change(op["id"], by=_actor(request), mission={"name": upload.filename, "staged": False})
        else:
            name = "preset.html" if slot == "preset" else f"{slot}.png"
            state.set_file(op["id"], slot, content, name)
        return _reply(op, request)

    async def upload_preset(request: web.Request) -> web.Response:
        return await _upload(request, "preset")

    async def upload_image(request: web.Request) -> web.Response:
        return await _upload(request, request.match_info["slot"])

    async def upload_mission(request: web.Request) -> web.Response:
        return await _upload(request, "mission")

    async def pick_mission(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        name = (await request.json())["name"]
        if name not in state.server_missions:
            return _error(404, f"No mission '{name}' on the server", code="not_found")
        state.change(op["id"], by=_actor(request), mission={"name": name, "staged": False})
        return _reply(op, request)

    async def clear_mission(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        state.change(op["id"], by=_actor(request), mission={})
        return _reply(op, request)

    async def remove_file(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        slot = request.match_info["slot"]
        if slot in op["files"]:
            state.change(op["id"], by=_actor(request), files={k: v for k, v in op["files"].items() if k != slot})
        return _reply(op, request)

    app = web.Application(middlewares=[auth])
    app[STATE] = state
    add = app.router
    add.add_get("/api/system/info", system_info)
    add.add_post("/api/integrations/heartbeat", heartbeat)
    add.add_get("/api/auth/me", me)
    add.add_get("/api/events/stream", stream)
    add.add_put("/api/members/roster", push_roster)
    add.add_put("/api/members/roles", push_roles)
    add.add_get("/api/members/me", my_member)
    add.add_put("/api/members/me/steam", link_steam)
    add.add_delete("/api/members/me/steam", unlink_steam)
    add.add_put("/api/members/{discord_id:\\d{1,25}}", push_member)
    add.add_get("/api/members", list_members)
    add.add_get("/api/notices", list_notices)
    add.add_post("/api/notices/{notice_id:\\d+}/done", notice_done)
    add.add_post("/api/notices/{notice_id:\\d+}/claim", notice_claim)
    add.add_get("/api/operations", list_operations)
    add.add_post("/api/operations", create_operation)
    add.add_get("/api/operations/changes", changes)
    add.add_get("/api/operations/meta", meta)
    add.add_get("/api/operations/persons", persons)
    add.add_get("/api/operations/missions", missions)
    add.add_get("/api/operations/groups", list_groups)
    add.add_post("/api/operations/groups", add_group)
    add.add_patch("/api/operations/groups/{group_id:\\d+}", rename_group)
    add.add_get("/api/operations/{op_id:\\d+}", get_operation)
    add.add_patch("/api/operations/{op_id:\\d+}", update_operation)
    add.add_delete("/api/operations/{op_id:\\d+}", delete_operation)
    add.add_put("/api/operations/{op_id:\\d+}/opord", set_opord)
    add.add_put("/api/operations/{op_id:\\d+}/organisers", set_organisers)
    add.add_post("/api/operations/{op_id:\\d+}/publish", publish)
    add.add_post("/api/operations/{op_id:\\d+}/cancel", cancel)
    add.add_post("/api/operations/{op_id:\\d+}/copy", copy_operation)
    add.add_put("/api/operations/{op_id:\\d+}/discord", set_discord)
    add.add_get("/api/operations/{op_id:\\d+}/files/{name}", get_file)
    add.add_delete("/api/operations/{op_id:\\d+}/files/{slot}", remove_file)
    add.add_put("/api/operations/{op_id:\\d+}/images/{slot}", upload_image)
    add.add_put("/api/operations/{op_id:\\d+}/preset", upload_preset)
    add.add_put("/api/operations/{op_id:\\d+}/mission", upload_mission)
    add.add_delete("/api/operations/{op_id:\\d+}/mission", clear_mission)
    add.add_post("/api/operations/{op_id:\\d+}/mission/pick", pick_mission)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=int(sys.argv[1]) if len(sys.argv) > 1 else 8099)
