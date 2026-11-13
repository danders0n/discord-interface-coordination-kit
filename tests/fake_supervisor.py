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
PUBLISH, SIGNUP, APPROVE = "operations.publish", "operations.signup", "operations.approve"
SLOT_LINE = re.compile(r"^(?P<lock>!)?\s*(?P<name>.*?)(?:\s+x(?P<count>\d+))?(?:\s*\[(?P<role>[^\[\]]*)\])?"
                       r"(?:\s+x(?P<count2>\d+))?\s*$", re.IGNORECASE)


def parse_orbat(text: str) -> tuple[list[tuple[str, list[tuple[str, str | None, bool]]]], list[str]]:
    """The ORBAT text format (the supervisor's ``app/operations/orbat.py``, simplified: roles by id only): squads
    with their slots (name, role id, locked), and the problems by line."""
    squads: list[tuple[str, list[tuple[str, str | None, bool]]]] = []
    problems = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            squads.append((line.lstrip("#").strip(), []))
            continue
        if not squads:
            problems.append(f"Line {number}: a slot before the first squad (# Name)")
            continue
        match = SLOT_LINE.match(line)
        name = (match.group("name") or "").strip() if match else ""
        if not name:
            problems.append(f"Line {number}: a slot needs a name")
            continue
        role = (match.group("role") or "").strip().lstrip("@") or None
        if role is not None and not role.isdigit():
            problems.append(f"Line {number}: no role '{role}'")
            continue
        count = int(match.group("count") or match.group("count2") or 1)
        squads[-1][1].extend([(name, role, bool(match.group("lock")))] * count)
    return squads, problems


def checklist(op: dict[str, Any]) -> list[str]:
    """What publishing still needs (the real ``OperationService.checklist``)."""
    missing = [f"opord.{p}" for p in ("situation", "mission") if not (op["opord"].get(p) or "").strip()]
    if op["kind"] == "guest" and not op["how_to_join"].strip():
        missing.append("how_to_join")
    return missing


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
    # The supervisor's phase 4: id counters (slots, squads, offers, requests, templates), the ORBAT templates, and the
    # minutes before the assembly from which a freed slot goes to the whole reserve list.
    counters: dict[str, int] = field(default_factory=dict)
    templates: list[dict[str, Any]] = field(default_factory=list)
    offer_to_all_minutes: int = 60
    # The supervisor's phase 5: the rating window and the erasures' counter.
    rating_days: int = 3
    erasures: int = 0

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
              "published_at": None, "ended_at": None, "squads": [], "signups": [], "offers": [], "requests": [],
              "attendance_state": "", "reported_at": None, "guests": 0, "entries": [], "ratings": []}
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

    # ------------------------------------------------------------ the ORBAT and sign-ups (the supervisor's phase 4)

    def _id(self, name: str) -> int:
        value = self.counters.get(name, 0) + 1
        self.counters[name] = value
        return value

    def by_person(self, person_id: int | None) -> dict[str, Any] | None:
        return next((p for p in self.persons.values() if p["id"] == person_id), None) if person_id else None

    def slots(self, op: dict[str, Any]) -> list[dict[str, Any]]:
        return [slot for squad in op["squads"] for slot in squad["slots"]]

    def slot(self, op: dict[str, Any], slot_id: int) -> dict[str, Any] | None:
        return next((s for s in self.slots(op) if s["id"] == slot_id), None)

    def entry(self, op: dict[str, Any], person_id: int) -> dict[str, Any] | None:
        return next((e for e in op["signups"] if e["person_id"] == person_id), None)

    def held(self, op: dict[str, Any], person_id: int) -> dict[str, Any] | None:
        return next((s for s in self.slots(op) if s["holder"] == person_id), None)

    def set_orbat(self, op_id: int, text: str, by: str = "anna") -> list[int]:
        """Replace the ORBAT as the app does: holders keep a slot of the same squad, name and place; the others go
        to the top of the reserve list (a ``slot_lost`` notice). Returns the displaced persons."""
        op = self.operations[op_id]
        old = {(squad["name"], slot["name"], n): slot for squad in op["squads"]
               for n, slot in enumerate(s for s in squad["slots"])}
        squads, problems = parse_orbat(text)
        if problems:
            raise ValueError(problems)
        kept, new = set(), []
        for squad_name, slots in squads:
            made = []
            for n, (name, role_id, locked) in enumerate(slots):
                before = old.get((squad_name, name, n))
                holder = before["holder"] if before is not None and not locked else None
                if holder is not None:
                    kept.add(holder)
                made.append({"id": self._id("slot"), "name": name, "role_id": role_id, "locked": locked or bool(
                    before and before["locked"] and holder), "holder": holder})
            new.append({"id": self._id("squad"), "name": squad_name, "slots": made})
        displaced = [s["holder"] for s in old.values() if s["holder"] is not None and s["holder"] not in kept]
        op["squads"] = new
        for position, person_id in enumerate(displaced):
            self._list(op, person_id, "reserve", top=position)
        if displaced:
            self.notice("slot_lost", operation_id=op_id, persons=[self.by_person(p)["discord_id"] for p in displaced],
                        data={"key": f"OP-{op_id}", "title": op["title"], "reason": "orbat"},
                        expires_at=op["start_at"])
        self._touch(op, by, ["orbat", "signups"] if displaced else ["orbat"], "updated", revision=True)
        return displaced

    def _list(self, op: dict[str, Any], person_id: int, status: str | None, *, top: int | None = None,
              assigned: bool = False) -> None:
        """Put a person on a list (off any other), or off every list (``status`` None)."""
        op["signups"] = [e for e in op["signups"] if e["person_id"] != person_id]
        if status is None:
            return
        entry = {"person_id": person_id, "status": status, "assigned": assigned, "reserve_position": None}
        if status == "reserve":
            reserve = [e for e in op["signups"] if e["status"] == "reserve"]
            entry["reserve_position"] = 0
            if top is None:
                entry["reserve_position"] = max((e["reserve_position"] for e in reserve), default=-1) + 1
            else:
                for other in reserve:
                    if other["reserve_position"] >= top:
                        other["reserve_position"] += 1
                entry["reserve_position"] = top
        op["signups"].append(entry)

    def _open_offer(self, op: dict[str, Any], slot_id: int) -> dict[str, Any] | None:
        return next((o for o in op["offers"] if o["slot_id"] == slot_id and o["state"] == "open"), None)

    def free(self, op: dict[str, Any], slot: dict[str, Any], by: str = "anna") -> None:
        """A slot is freed: offered to the first on the reserve list (to the whole list near the assembly)."""
        slot["holder"], slot["locked"] = None, False
        reserve = sorted((e for e in op["signups"] if e["status"] == "reserve"), key=lambda e: e["reserve_position"])
        if not reserve or self._open_offer(op, slot["id"]) or self.now >= op["start_at"]:
            return
        to_all = op["assembly_at"] - self.now <= timedelta(minutes=self.offer_to_all_minutes)
        persons = [self.by_person(e["person_id"]) for e in (reserve if to_all else reserve[:1])]
        offer = {"id": self._id("offer"), "slot_id": slot["id"], "person_id": None if to_all else reserve[0]["person_id"],
                 "state": "open", "delivered_at": None, "delivered_via": None, "created_at": self.now,
                 "expires_at": op["start_at"] if to_all else None}
        op["offers"].append(offer)
        squad = next(s for s in op["squads"] if slot in s["slots"])
        self.notice("offer", operation_id=op["id"], persons=[p["discord_id"] for p in persons if p],
                    data={"key": f"OP-{op['id']}", "title": op["title"], "offer_id": offer["id"], "to_all": to_all,
                          "slot": {"id": slot["id"], "name": slot["name"], "squad": squad["name"]}},
                    expires_at=op["start_at"])

    def sign(self, op_id: int, discord_id: Any, slot_id: int | None = None, status: str | None = None,
             by: str = "anna") -> None:
        """A member signs up in the app (a slot, or a list)."""
        op, person = self.operations[op_id], self.persons[str(discord_id)]
        if slot_id is not None:
            mine = self.held(op, person["id"])
            if mine is not None:
                self.free(op, mine)
            self.slot(op, slot_id)["holder"] = person["id"]
            self._list(op, person["id"], None)
        else:
            self._list(op, person["id"], status)
        self._touch(op, by, ["signups"], "updated", revision=False)

    def signup_state(self, op: dict[str, Any]) -> str:
        if op["status"] in ("draft", "pending"):
            return "not_published"
        if op["status"] != "published" or self.now >= op["start_at"]:
            return "closed"
        return "open" if op["signup_opens_at"] is None or self.now >= op["signup_opens_at"] else "not_open"

    def _person_out(self, person_id: int | None) -> dict[str, Any] | None:
        return self._person(person_id)

    def board(self, op: dict[str, Any]) -> dict[str, Any]:
        slots = self.slots(op)
        counts = {"total": len(slots), "taken": sum(s["holder"] is not None for s in slots),
                  "closed": sum(s["locked"] and s["holder"] is None for s in slots)}
        counts["free"] = counts["total"] - counts["taken"] - counts["closed"]
        coming = sum(e["status"] == "coming" for e in op["signups"])
        offered = {o["slot_id"] for o in op["offers"] if o["state"] == "open"}
        lists: dict[str, list[dict[str, Any]]] = {name: [] for name in ("maybe", "cant", "reserve", "coming")}
        for entry in sorted(op["signups"], key=lambda e: (e["reserve_position"] is None, e["reserve_position"] or 0)):
            out = self._person_out(entry["person_id"])
            if out is not None:
                lists[entry["status"]].append(out | {"assigned": entry["assigned"], "missing_dlc": []})
        return {
            "slots": counts,
            "signup": {"state": self.signup_state(op), "opens_at": iso(op["signup_opens_at"]),
                       "limit": op["signup_limit"], "coming": coming,
                       "full": bool(op["signup_limit"]) and coming >= op["signup_limit"], "closes_at": iso(op["start_at"])},
            "orbat": [{"id": squad["id"], "name": squad["name"], "free": sum(
                s["holder"] is None and not s["locked"] for s in squad["slots"]), "slots": [
                {"id": s["id"], "name": s["name"], "locked": s["locked"], "offered": s["id"] in offered,
                 "required_role": {"id": s["role_id"], "name": s["role_id"]} if s["role_id"] else None,
                 "holder": self._person_out(s["holder"]), "flagged": False, "missing_dlc": []}
                for s in squad["slots"]]} for squad in op["squads"]],
            "signups": lists,
            "offers": [{"id": o["id"], "slot_id": o["slot_id"], "person": self._person_out(o["person_id"]),
                        "to_all": o["person_id"] is None, "created_at": iso(o["created_at"]),
                        "delivered_at": iso(o["delivered_at"]), "delivered_via": o["delivered_via"],
                        "expires_at": iso(o["expires_at"])} for o in op["offers"] if o["state"] == "open"],
        }

    def mine(self, op: dict[str, Any], person: dict[str, Any]) -> dict[str, Any]:
        """The caller's place, offers and sign-up actions, as the real ``Signups.mine``."""
        slot, entry = self.held(op, person["id"]), self.entry(op, person["id"])
        place = ({"slot_id": slot["id"], "slot": slot["name"], "locked": slot["locked"],
                  "squad": next(s["name"] for s in op["squads"] if slot in s["slots"])} if slot is not None else
                 {"list": entry["status"], "reserve_position": entry["reserve_position"]} if entry is not None else None)
        offers = [o["id"] for o in op["offers"] if o["state"] == "open" and (
            o["person_id"] == person["id"] or (o["person_id"] is None and entry and entry["status"] == "reserve"))]
        actions, state = [], self.signup_state(op)
        if state in ("open", "not_open") and SIGNUP in person["permissions"]:
            pinned, current = slot is not None and slot["locked"], entry["status"] if entry else None
            if op["kind"] in COMING:
                if state == "open" and current != "coming" and not self.board(op)["signup"]["full"]:
                    actions.append("coming")
                if current != "cant":
                    actions.append("cant")
            else:
                if state == "open" and not pinned:
                    actions.append("take")
                    free = any(s["holder"] is None and not s["locked"] for s in self.slots(op))
                    if slot is None and current != "reserve" and not free:
                        actions.append("reserve")
                if not pinned and current != "maybe":
                    actions.append("maybe")
                if not pinned and current != "cant":
                    actions.append("cant")
            if (slot is not None and not pinned) or entry is not None:
                actions.append("leave")
            if offers and state == "open":
                actions.append("accept_offer")
        return {"person_id": person["id"], "place": place, "offers": offers, "signup_actions": actions}

    def request(self, op_id: int, kind: str = "publish", by: str = "anna", **proposed: Any) -> dict[str, Any]:
        """An approval request, as *Send for approval* (or a host request, a times proposal) makes it."""
        op = self.operations[op_id]
        row = {"id": self._id("request"), "kind": kind, "state": "pending", "requested_by": by,
               "requested_at": iso(self.now), "proposed": proposed, "decided_by": None, "decided_at": None,
               "reason": None, "blocked": None}
        op["requests"].append(row)
        self.notice("proposal_requested" if kind == "times" else "approval_requested", operation_id=op_id,
                    data=self.request_out(op, row), expires_at=op["start_at"])
        return row

    def request_out(self, op: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
        return dict(row) | {"key": f"OP-{op['id']}", "title": op["title"], "operation": {
            "id": op["id"], "key": f"OP-{op['id']}", "title": op["title"], "kind": op["kind"], "status": op["status"],
            "assembly_at": iso(op["assembly_at"]), "start_at": iso(op["start_at"]),
            "duration_minutes": op["duration_minutes"]}}

    # ------------------------------------------------------------ attendance and ratings (the supervisor's phase 5)

    def report(self, op_id: int, lines: list[tuple[Any, str]], guests: int = 0) -> None:
        """The attendance as the supervisor reports it: ``lines`` of (Discord id or a player's name, status); the
        operation ends if it hasn't, and an ``attendance_reported`` notice goes out."""
        op = self.operations[op_id]
        op["entries"] = []
        for who, status in lines:
            person = self.persons.get(str(who))
            op["entries"].append({"id": self._id("entry"), "person_id": person["id"] if person else None,
                                  "status": status, "holder": person is not None and self.held(op, person["id"]) is not None,
                                  "name": None if person else str(who),
                                  "minutes": 90 if status in ("attended", "walk_in") else 0, "corrected_by": None})
        op["attendance_state"], op["reported_at"], op["guests"] = "reported", self.now, guests
        if op["status"] != "ended":
            op["status"], op["ended_at"] = "ended", self.now
        counts: dict[str, int] = {}
        for entry in op["entries"]:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        self.notice("attendance_reported", operation_id=op_id, data={"key": f"OP-{op_id}", "title": op["title"],
                                                                     "counts": counts, "guests": guests})
        self._touch(op, "supervisor", ["attendance", "status"], "attendance_reported", revision=False)

    def rating_closes(self, op: dict[str, Any]) -> datetime | None:
        if op["reported_at"] is None or op["attendance_state"] not in ("reported", "unavailable"):
            return None
        return op["reported_at"] + timedelta(days=self.rating_days)

    def may_rate(self, op: dict[str, Any], person: dict[str, Any] | None) -> str | None:
        closes = self.rating_closes(op)
        if closes is None:
            return "not_open"
        if self.now >= closes:
            return "rating_closed"
        entries = [e for e in op["entries"] if person is not None and e["person_id"] == person["id"]]
        ok = any(e["holder"] for e in entries) if op["kind"] == "guest" else any(
            e["status"] in ("attended", "walk_in") for e in entries)
        return None if ok else "not_attended"

    def attendance_out(self, op: dict[str, Any], person: dict[str, Any] | None) -> dict[str, Any]:
        if not op["attendance_state"]:
            return {"attendance": None}
        counts: dict[str, int] = {}
        for entry in op["entries"]:
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        ratings = op["ratings"]
        closes = self.rating_closes(op)
        rating = {"average": round(sum(r["stars"] for r in ratings) / len(ratings), 2) if ratings else None,
                  "count": len(ratings), "closes_at": iso(closes), "open": closes is not None and self.now < closes,
                  "cooperation": op["kind"] == "guest"}
        if person is None or self.organises(op, person) or APPROVE in person["permissions"]:
            rating["ratings"] = [{"person": self._person(r["person_id"]), "stars": r["stars"], "comment": r["comment"]}
                                 for r in ratings]
        return {"attendance": {
            "state": op["attendance_state"], "reported_at": iso(op["reported_at"]), "guests": op["guests"],
            "counts": counts, "min_minutes": 30,
            "entries": [{"id": e["id"], "status": e["status"], "person": self._person(e["person_id"]),
                         "holder": e["holder"], "name": e["name"], "minutes": e["minutes"], "steam_id": None,
                         "probable_name": None, "probable_steam_id": None, "corrected_by": e["corrected_by"]}
                        for e in op["entries"]]}, "rating": rating}

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
            out += ["orbat", "slots"] if op["kind"] not in COMING else ["slots"]
        if op["status"] == "draft" and organiser:
            out.append("publish" if PUBLISH in person["permissions"] or op["kind"] in COMING else "submit")
        if op["status"] == "pending" and organiser:
            out.append("withdraw")
        if op["status"] in ("draft", "pending", "published") and organiser:
            out.append("delete" if op["published_at"] is None else "cancel")
        out.append("copy")
        if APPROVE in person["permissions"] and any(r["state"] == "pending" and r["requested_by"] != (
                person["account"] or {}).get("username") for r in op["requests"]):
            out.append("decide")
        return out

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
        board = self.board(op)
        data |= board if full else {"slots": board["slots"], "signup": board["signup"]}
        if full:
            data["requests"] = [self.request_out(op, r) for r in op["requests"]]
            data |= self.attendance_out(op, person)
        if person is not None:
            mine = self.mine(op, person)
            actions = self.actions(op, person) + mine.pop("signup_actions")
            if op["attendance_state"]:
                entry = next((e for e in op["entries"] if e["person_id"] == person["id"]), None)
                rated = next((r for r in op["ratings"] if r["person_id"] == person["id"]), None)
                mine["attendance"] = entry["status"] if entry else None
                mine["rating"] = {"stars": rated["stars"], "comment": rated["comment"]} if rated else None
                if self.may_rate(op, person) is None:
                    actions.append("rate")
                if op["attendance_state"] in ("reported", "unavailable") and self.organises(op, person):
                    actions.append("correct")
            if op["status"] == "published" and self.now >= op["start_at"] and self.organises(op, person):
                actions.append("end_operation")
            data["my"] = {"organiser": self.organises(op, person), "actions": actions, **mine}
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
        if op["status"] == "pending":
            return _error(409, f"OP-{op['id']} waits for approval: withdraw the request to change it", code="pending")
        if op["status"] not in ("draft", "published"):
            return _error(409, f"OP-{op['id']} is {op['status']}: it is kept as history", code="closed")
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
        person, proposal = _person(request), None
        moved = {k: fields.pop(k) for k in ("assembly_at", "start_at", "duration_minutes") if k in fields}
        if moved and op["status"] == "published" and op["kind"] not in COMING and PUBLISH not in person["permissions"]:
            times = {"assembly_at": iso(trial["assembly_at"]), "start_at": iso(trial["start_at"]),
                     "duration_minutes": trial["duration_minutes"]}
            proposal = state.request(op["id"], "times", by=_actor(request), **times)["id"]
            state._touch(op, _actor(request), ["requests"], "proposed", revision=False)
        else:
            fields |= moved
        if fields:
            state.change(op["id"], by=_actor(request), **fields)
        return web.json_response(state.out(op, person) | {"proposal": proposal}, headers={"ETag": f'"{op["revision"]}"'})

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
        missing = checklist(op)
        if missing:
            return _error(422, f"Publishing needs: {', '.join(missing)}", code="incomplete", missing=missing)
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
        staged = PUBLISH not in _person(request)["permissions"] and op["published_at"] is None  # (until approval)
        if slot == "mission":
            state.change(op["id"], by=_actor(request), mission={"name": upload.filename, "staged": staged})
        elif slot == "preset":  # the launcher's file: one ModContainer row per mod
            mods = [[str(n), f"mod {n}"] for n in range(content.decode("utf-8", "replace").count('data-type="ModContainer"'))]
            state.set_file(op["id"], slot, content, "preset.html", original=upload.filename, mods=mods, staged=staged)
        else:
            state.set_file(op["id"], slot, content, f"{slot}.png")
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

    # ------------------------------------------------------------ the ORBAT and sign-ups (the supervisor's phase 4)

    def _refuse(code: str, message: str, status: int = 409, **extra: Any) -> web.Response:
        return _error(status, message, code=code, **extra)

    def _signed(op: dict[str, Any], request: web.Request, outcome: dict[str, Any] | None = None) -> web.Response:
        return web.json_response(state.out(op, _person(request)) | {"outcome": outcome or {}})

    def _signer(op: dict[str, Any], request: web.Request, answer: bool = False):
        """The person signing up, or the refusal (the real ``Signups.signer``)."""
        person = _person(request)
        if op["status"] != "published":
            return None, _refuse("not_published", f"OP-{op['id']} is not published: nobody can sign up yet")
        if state.now >= op["start_at"]:
            return None, _refuse("closed", f"OP-{op['id']} has started: sign-up is closed")
        if person is None or SIGNUP not in person["permissions"]:
            return None, _refuse("not_allowed", "Signing up needs the permission Sign up for operations", 403)
        if not answer and op["signup_opens_at"] is not None and state.now < op["signup_opens_at"]:
            return None, _refuse("not_open", "Sign-up opens later", opens_at=iso(op["signup_opens_at"]))
        return person, None

    def _changed(op: dict[str, Any], request: web.Request, fields: tuple[str, ...] = ("signups",)) -> None:
        state._touch(op, _actor(request), list(fields), "updated", revision=False)

    def _claim(op: dict[str, Any], slot: dict[str, Any], person: dict[str, Any]) -> web.Response | None:
        """Take ``slot`` for ``person`` if still free (a refusal otherwise), moving them from their other place."""
        if slot["holder"] == person["id"]:
            return _refuse("already", "You have this slot already")
        if slot["holder"] is not None:
            return _refuse("taken", "Someone took this slot a moment ago")
        if slot["locked"]:
            return _refuse("locked", "This slot is closed")
        offer = state._open_offer(op, slot["id"])
        entry = state.entry(op, person["id"])
        if offer is not None and offer["person_id"] != person["id"] and not (
                offer["person_id"] is None and entry is not None and entry["status"] == "reserve"):
            return _refuse("offered", "This slot is offered to someone on the reserve list")
        mine = state.held(op, person["id"])
        if mine is not None and mine["locked"]:
            return _refuse("locked", "An organiser assigned you a slot: ask them to move you")
        if mine is not None:
            state.free(op, mine)
        slot["holder"] = person["id"]
        state._list(op, person["id"], None)
        if offer is not None:
            offer["state"] = "taken"
        return None

    async def take_slot(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        person, refused = _signer(op, request)
        if refused is not None:
            return refused
        if op["kind"] in COMING:
            return _refuse("wrong_kind", "This operation has no ORBAT: say Coming")
        slot = state.slot(op, int(request.match_info["slot_id"]))
        if slot is None:
            return _refuse("no_slot", "No such slot", 404)
        refused = _claim(op, slot, person)
        if refused is not None:
            return refused
        _changed(op, request)
        return _signed(op, request)

    async def answer(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        status = (await request.json())["status"]
        person, refused = _signer(op, request, answer=status in ("maybe", "cant"))
        if refused is not None:
            return refused
        if (status == "coming") != (op["kind"] in COMING) and status != "cant":
            return _refuse("wrong_kind", "That answer doesn't fit this operation")
        if status == "coming" and state.board(op)["signup"]["full"]:
            return _refuse("full", "Everybody who may come is coming already")
        if status == "reserve" and any(s["holder"] is None and not s["locked"] for s in state.slots(op)):
            return _refuse("not_full", "There is a free slot: take it")
        mine = state.held(op, person["id"])
        if mine is not None and mine["locked"]:
            return _refuse("locked", "An organiser assigned you a slot: ask them to move you")
        if mine is not None:
            state.free(op, mine)
        state._list(op, person["id"], status)
        _changed(op, request)
        return _signed(op, request)

    async def leave(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        person, refused = _signer(op, request, answer=True)
        if refused is not None:
            return refused
        mine, entry = state.held(op, person["id"]), state.entry(op, person["id"])
        if mine is None and entry is None:
            return _refuse("not_signed_up", "You are not signed up")
        if mine is not None and mine["locked"]:
            return _refuse("locked", "An organiser assigned you a slot: ask them to move you")
        if mine is not None:
            state.free(op, mine)
        state._list(op, person["id"], None)
        _changed(op, request)
        return _signed(op, request)

    def _offer(op: dict[str, Any], request: web.Request) -> dict[str, Any] | None:
        return next((o for o in op["offers"] if o["id"] == int(request.match_info["offer_id"])), None)

    async def accept_offer(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        person, refused = _signer(op, request)
        if refused is not None:
            return refused
        offer = _offer(op, request)
        if offer is None or offer["state"] != "open":
            return _refuse("offer_gone", "This offer has ended")
        refused = _claim(op, state.slot(op, offer["slot_id"]), person)
        if refused is not None:
            return refused
        _changed(op, request, ("signups", "offers"))
        return _signed(op, request)

    async def decline_offer(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        offer = _offer(op, request)
        if offer is None or offer["state"] != "open" or offer["person_id"] != (_person(request) or {}).get("id"):
            return _refuse("offer_gone", "This offer has ended")
        offer["state"] = "declined"
        _changed(op, request, ("signups", "offers"))
        return _signed(op, request)

    async def offer_delivered(request: web.Request) -> web.Response:
        if PERSON in request:
            return _error(403, "Only the bot reports deliveries", code="act_as_route")
        op = state.operations.get(int(request.match_info["op_id"]))
        offer = _offer(op, request) if op is not None else None
        if offer is None:
            return _error(404, "No such offer", code="not_found")
        if offer["delivered_at"] is None:
            offer["delivered_at"], offer["delivered_via"] = state.now, (await request.json())["via"]
            if offer["person_id"] is not None:
                offer["expires_at"] = min(state.now + timedelta(minutes=30), op["start_at"])
            state._touch(op, "discord-bot", ["offers"], "updated", revision=False)
        return web.json_response(state.out(op))

    def _organised(request: web.Request) -> dict[str, Any] | web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        if not state.organises(op, _person(request)):
            return _refuse("not_allowed", f"Only the organisers of OP-{op['id']} may do that", 403)
        return op

    async def assign_slot(request: web.Request) -> web.Response:
        op = _organised(request)
        if isinstance(op, web.Response):
            return op
        slot = state.slot(op, int(request.match_info["slot_id"]))
        person_id = (await request.json())["person_id"]
        if slot is None or state.by_person(person_id) is None:
            return _refuse("not_found", "No such slot or person", 404)
        if slot["holder"] not in (None, person_id):
            state._list(op, slot["holder"], "reserve", top=0)
        mine = state.held(op, person_id)
        if mine is not None and mine is not slot:
            mine["holder"], mine["locked"] = None, False
        slot["holder"], slot["locked"] = person_id, True
        state._list(op, person_id, None)
        _changed(op, request)
        return _signed(op, request)

    async def lock_slot(request: web.Request) -> web.Response:
        op = _organised(request)
        if isinstance(op, web.Response):
            return op
        slot = state.slot(op, int(request.match_info["slot_id"]))
        if slot is None:
            return _refuse("no_slot", "No such slot", 404)
        slot["locked"] = bool((await request.json())["locked"])
        _changed(op, request)
        return _signed(op, request)

    async def remove_holder(request: web.Request) -> web.Response:
        op = _organised(request)
        if isinstance(op, web.Response):
            return op
        slot = state.slot(op, int(request.match_info["slot_id"]))
        if slot is None or slot["holder"] is None:
            return _refuse("no_slot", "Nobody holds that slot", 404)
        state.free(op, slot)
        _changed(op, request)
        return _signed(op, request)

    async def place_person(request: web.Request) -> web.Response:
        op = _organised(request)
        if isinstance(op, web.Response):
            return op
        person_id = int(request.match_info["person_id"])
        if state.by_person(person_id) is None:
            return _refuse("not_found", "No such person", 404)
        state._list(op, person_id, (await request.json()).get("status"), assigned=True)
        _changed(op, request)
        return _signed(op, request)

    def _orbat_text(op: dict[str, Any]) -> str:
        lines = []
        for squad in op["squads"]:
            lines.append(f"# {squad['name']}")
            for slot in squad["slots"]:
                closed = "!" if slot["locked"] and slot["holder"] is None else ""
                role = f" [{slot['role_id']}]" if slot["role_id"] else ""
                lines.append(f"{closed}{slot['name']}{role}")
        return "\n".join(lines)

    async def get_orbat(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        return web.json_response({"text": _orbat_text(op), "revision": op["revision"]})

    async def set_orbat(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        if op["kind"] in COMING:
            return _refuse("wrong_kind", "A Zeus or training operation has no ORBAT")
        text = (await request.json())["text"]
        problems = parse_orbat(text)[1]
        if problems:
            return _refuse("bad_orbat", "The ORBAT has problems", 422, problems=problems)
        displaced = state.set_orbat(op["id"], text, by=_actor(request))
        squads = [{"name": s["name"], "slots": len(s["slots"])} for s in op["squads"]]
        return _signed(op, request, {"squads": squads, "displaced": len(displaced)})

    async def clear_orbat(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        refused = _editable(op, request)
        if refused is not None:
            return refused
        displaced = state.set_orbat(op["id"], "", by=_actor(request))
        return _signed(op, request, {"squads": [], "displaced": len(displaced)})

    def _template_out(row: dict[str, Any], request: web.Request) -> dict[str, Any]:
        person = _person(request)
        return dict(row) | {"mine": person is not None and row["owner"] == (person["account"] or {}).get("username")}

    async def list_templates(request: web.Request) -> web.Response:
        return web.json_response([_template_out(row, request) for row in state.templates])

    async def save_template(request: web.Request) -> web.Response:
        body = await request.json()
        problems = parse_orbat(body["text"])[1]
        if problems:
            return _refuse("bad_orbat", "The ORBAT has problems", 422, problems=problems)
        row = next((t for t in state.templates if t["name"].casefold() == body["name"].casefold()), None)
        if row is not None and row["owner"] != _actor(request):
            return _refuse("name_taken", f"A template called {body['name']} exists")
        if row is None:
            row = {"id": state._id("template"), "name": body["name"], "owner": _actor(request)}
            state.templates.append(row)
        row["text"] = body["text"]
        return web.json_response(_template_out(row, request), status=201)

    def _template(request: web.Request) -> dict[str, Any] | web.Response:
        row = next((t for t in state.templates if t["id"] == int(request.match_info["template_id"])), None)
        if row is None:
            return _error(404, "No such template", code="not_found")
        if row["owner"] != _actor(request):
            return _refuse("not_allowed", "Only who saved it changes a template", 403)
        return row

    async def change_template(request: web.Request) -> web.Response:
        row = _template(request)
        if isinstance(row, web.Response):
            return row
        row.update({k: v for k, v in (await request.json()).items() if v is not None})
        return web.json_response(_template_out(row, request))

    async def delete_template(request: web.Request) -> web.Response:
        row = _template(request)
        if isinstance(row, web.Response):
            return row
        state.templates.remove(row)
        return web.Response(status=204)

    # ------------------------------------------------------------ approval

    def _requester(row: dict[str, Any]) -> list[str]:
        return [p["discord_id"] for p in state.persons.values() if (p["account"] or {}).get("username") == row["requested_by"]]

    def _find_request(request: web.Request) -> tuple[dict[str, Any], dict[str, Any]] | None:
        wanted = int(request.match_info["request_id"])
        return next(((op, r) for op in state.operations.values() for r in op["requests"] if r["id"] == wanted), None)

    async def list_requests(request: web.Request) -> web.Response:
        wanted = request.query.get("state", "pending")
        return web.json_response([state.request_out(op, r) for op in state.operations.values() for r in op["requests"]
                                  if wanted == "all" or r["state"] == wanted])

    async def _decide(request: web.Request, approve: bool) -> web.Response:
        found = _find_request(request)
        if found is None:
            return _error(404, "No such request", code="not_found")
        op, row = found
        person = _person(request)
        if person is None or APPROVE not in person["permissions"]:
            return _refuse("not_allowed", "Deciding needs Approve operations", 403)
        if row["requested_by"] == (person["account"] or {}).get("username"):
            return _refuse("own_request", "Nobody decides their own request")
        if row["state"] != "pending":
            return _refuse("not_pending", "This request was decided meanwhile")
        body = await request.json() if not approve else {}
        row.update(state="approved" if approve else "rejected", decided_by=_actor(request), decided_at=iso(state.now),
                   reason=body.get("reason") or None)
        if row["kind"] == "publish":
            op["status"] = "published" if approve else "draft"
            if approve and op["published_at"] is None:
                op["published_at"] = state.now
        state.notice("proposal_decided" if row["kind"] == "times" else "approval_decided", operation_id=op["id"],
                     persons=_requester(row), data=state.request_out(op, row))
        state._touch(op, _actor(request), ["status", "requests"], "approved" if approve else "rejected", revision=False)
        return web.json_response(state.request_out(op, row) | {"may_decide": False})

    async def approve_request(request: web.Request) -> web.Response:
        return await _decide(request, True)

    async def reject_request(request: web.Request) -> web.Response:
        return await _decide(request, False)

    async def submit(request: web.Request) -> web.Response:
        op = _organised(request)
        if isinstance(op, web.Response):
            return op
        if PUBLISH in _person(request)["permissions"] or op["kind"] in COMING:
            return _refuse("publish_instead", "You may publish it yourself")
        if op["status"] != "draft":
            return _refuse("not_draft", f"OP-{op['id']} is {op['status']}")
        missing = checklist(op)
        if missing:
            return _refuse("incomplete", f"Publishing needs: {', '.join(missing)}", 422, missing=missing)
        op["status"] = "pending"
        state.request(op["id"], by=_actor(request))
        state._touch(op, _actor(request), ["status", "requests"], "submitted", revision=False)
        return _reply(op, request)

    async def withdraw(request: web.Request) -> web.Response:
        op = _organised(request)
        if isinstance(op, web.Response):
            return op
        if op["status"] != "pending":
            return _refuse("not_pending", f"OP-{op['id']} is not waiting for approval")
        op["status"] = "draft"
        for row in op["requests"]:
            if row["state"] == "pending":
                row["state"] = "withdrawn"
        state._touch(op, _actor(request), ["status", "requests"], "withdrawn", revision=False)
        return _reply(op, request)

    # ------------------------------------------------------------ attendance, ratings, reports, erasure (phase 5)

    async def end_operation(request: web.Request) -> web.Response:
        op = _organised(request)
        if isinstance(op, web.Response):
            return op
        if op["status"] != "published":
            return _refuse("closed", f"OP-{op['id']} is {op['status']}")
        if state.now < op["start_at"]:
            return _refuse("not_started", f"OP-{op['id']} has not started: cancel it instead")
        op["status"], op["ended_at"], op["attendance_state"] = "ended", state.now, "waiting"
        state._touch(op, _actor(request), ["status", "attendance"], "ended", revision=False)
        return _reply(op, request)

    async def correct_attendance(request: web.Request) -> web.Response:
        op = _organised(request)
        if isinstance(op, web.Response):
            return op
        if op["attendance_state"] not in ("reported", "unavailable"):
            return _refuse("no_report", f"OP-{op['id']} has no attendance report yet")
        body = await request.json()
        for person_id in body["person_ids"]:
            if state.by_person(person_id) is None:
                return _refuse("unknown_person", f"Person {person_id} not found", 404)
            entry = next((e for e in op["entries"] if e["person_id"] == person_id), None)
            if entry is None:
                entry = {"id": state._id("entry"), "person_id": person_id, "holder": False, "name": None, "minutes": 0}
                op["entries"].append(entry)
            if entry.get("status") in ("attended", "walk_in") and body["status"] != "attended":
                op["ratings"] = [r for r in op["ratings"] if r["person_id"] != person_id]
            entry["status"], entry["corrected_by"] = body["status"], _actor(request)
        state._touch(op, _actor(request), ["attendance"], "attendance_corrected", revision=False)
        return _reply(op, request)

    async def rate(request: web.Request) -> web.Response:
        op = _get(request)
        if isinstance(op, web.Response):
            return op
        person = _person(request)
        refused = state.may_rate(op, person)
        if refused is not None:
            return _refuse(refused, "You can't rate this operation now")
        body = await request.json()
        op["ratings"] = [r for r in op["ratings"] if r["person_id"] != person["id"]]
        op["ratings"].append({"person_id": person["id"], "stars": body["stars"], "comment": body.get("comment", "")})
        state._touch(op, _actor(request), ["ratings"], "rated", revision=False)
        return _reply(op, request)

    def _who(request: web.Request) -> dict[str, Any] | web.Response:
        who = request.match_info["who"]
        person = _person(request) if who == "me" else (state.by_person(int(who)) if who.isdigit() else None)
        if person is None:
            return _error(404, f"Person {who} not found", code="unknown_person")
        return person

    def _staff(request: web.Request) -> bool:
        return bool({APPROVE, "operations.manage"} & set((_person(request) or {}).get("permissions") or []))

    def _op_brief(op: dict[str, Any]) -> dict[str, Any]:
        return {"id": op["id"], "key": f"OP-{op['id']}", "title": op["title"], "kind": op["kind"],
                "start_at": iso(op["start_at"]), "status": op["status"]}

    def _average(stars: list[int]) -> float | None:
        return round(sum(stars) / len(stars), 2) if stars else None

    def _maker(person: dict[str, Any], request: web.Request) -> dict[str, Any]:
        ops = [o for o in state.operations.values() if o["author_person_id"] == person["id"] and o["kind"] != "guest"
               and o["status"] == "ended"]
        stars = [r["stars"] for o in ops for r in o["ratings"]]
        comments = _staff(request) or (_person(request) or {}).get("id") == person["id"]
        items = []
        for o in ops:
            item = _op_brief(o) | {"average": _average([r["stars"] for r in o["ratings"]]), "count": len(o["ratings"])}
            if comments:
                item["ratings"] = [{"person": state._person(r["person_id"]), "stars": r["stars"],
                                    "comment": r["comment"]} for r in o["ratings"]]
            items.append(item)
        return {"person": state._person(person["id"]), "operations": items,
                "summary": {"missions": len(ops), "average": _average(stars), "ratings": len(stars)}}

    async def report_member(request: web.Request) -> web.Response:
        person = _who(request)
        if isinstance(person, web.Response):
            return person
        mine = (_person(request) or {}).get("id") == person["id"]
        full = mine or _staff(request)
        rows = [(o, e["status"]) for o in sorted(state.operations.values(), key=lambda o: o["start_at"], reverse=True)
                for e in o["entries"] if e["person_id"] == person["id"]]
        attended = sum(s in ("attended", "walk_in") for _, s in rows)
        no_show = sum(s == "no_show" for _, s in rows)
        record: dict[str, Any] = {"attended": attended,
                                  "rate": round(attended / (attended + no_show), 3) if attended + no_show else None}
        if full:
            record |= {"no_show": no_show, "excused": sum(s == "excused" for _, s in rows),
                       "recent": [_op_brief(o) | {"attendance": s} for o, s in rows[:10]]}
        out: dict[str, Any] = {"person": state._person(person["id"]), "mine": mine, "details": full,
                               "roles": [r["name"] for r in state.roles if str(r.get("id")) in set(person["roles"])],
                               "attendance": record, "maker": _maker(person, request)["summary"]}
        if full:
            uid = person.get("steam_uid")
            out["steam"] = {"steam_id": uid, "state": person.get("steam_state")} if uid else None
        return web.json_response(out)

    async def report_maker(request: web.Request) -> web.Response:
        person = _who(request)
        return person if isinstance(person, web.Response) else web.json_response(_maker(person, request))

    async def report_group(request: web.Request) -> web.Response:
        group = state._group(int(request.match_info["group_id"]))
        if group is None:
            return _error(404, "Group not found", code="not_found")
        ops = [o for o in state.operations.values()
               if (o["host_group_id"] == group["id"] or group["id"] in o["group_ids"])
               and o["status"] in ("published", "ended")]
        out: dict[str, Any] = {"group": group, "count": len(ops), "operations": [
            _op_brief(o) | {"role": "host" if o["host_group_id"] == group["id"] else "invited"} for o in ops]}
        if _staff(request):
            ratings = [r for o in ops if o["host_group_id"] == group["id"] for r in o["ratings"]]
            out["cooperation"] = {"average": _average([r["stars"] for r in ratings]), "count": len(ratings),
                                  "ratings": []}
        return web.json_response(out)

    async def report_attendance(request: web.Request) -> web.Response:
        if not _staff(request):
            return _refuse("not_allowed", "The attendance report needs Manage or Approve operations", 403)
        days = int(request.query.get("days", 30))
        found: dict[int, dict[str, int]] = {}
        for o in state.operations.values():
            if days and o["start_at"] < state.now - timedelta(days=days):
                continue
            for e in o["entries"]:
                if e["person_id"] is None:
                    continue
                row = found.setdefault(e["person_id"], {"attended": 0, "no_show": 0, "excused": 0})
                key = "attended" if e["status"] in ("attended", "walk_in") else e["status"]
                if key in row:
                    row[key] += 1
        rows = [{"person": state._person(pid), **row,
                 "rate": round(row["attended"] / (row["attended"] + row["no_show"]), 3)
                 if row["attended"] + row["no_show"] else None} for pid, row in found.items()]
        rows.sort(key=lambda r: (-r["attended"], r["person"]["display_name"].casefold()))
        if request.query.get("format") == "csv":
            lines = ["username,display_name,attended,no_show,excused,rate"] + [
                f"{r['person']['username']},{r['person']['display_name']},{r['attended']},{r['no_show']},"
                f"{r['excused']},{'' if r['rate'] is None else r['rate']}" for r in rows]
            return web.Response(text="\n".join(lines) + "\n", content_type="text/csv")
        return web.json_response(rows)

    async def erase_member(request: web.Request) -> web.Response:
        actor = _person(request)
        if actor is None or "users.manage" not in actor["permissions"]:
            return _error(403, "Erasing needs Manage users", code="forbidden")
        body = await request.json()
        if not body.get("confirm"):
            return _error(422, "Erasing can't be undone: send confirm: true")
        person = state.persons.get(str(body.get("discord_id")))
        if person is None:
            return _error(404, "Nobody to erase: no such member or account", code="not_found")
        if person.get("erased"):
            return _error(409, f"This member was erased already ({person['erased']})", code="erased")
        if person is actor:
            return _error(409, "You can't erase your own account", code="self")
        state.erasures += 1
        former = f"former:{state.erasures}"
        touched = sorted(o["id"] for o in state.operations.values()
                         if person["id"] in [*o["organisers"], *(e["person_id"] for e in o["entries"])]
                         or state.held(o, person["id"]) or state.entry(o, person["id"]))
        account = person["account"] is not None
        person.update(username=former, display_name=former, account=None, erased=former, steam_uid=None)
        state.notice("erased", persons=[person["discord_id"]], data={"discord_id": person["discord_id"],
                                                                     "former": former, "operations": touched})
        return web.json_response({"former": former, "number": state.erasures, "account_deleted": account,
                                  "player": False, "operations": touched})

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
    add.add_get("/api/operations/{op_id:\\d+}/orbat", get_orbat)
    add.add_put("/api/operations/{op_id:\\d+}/orbat", set_orbat)
    add.add_delete("/api/operations/{op_id:\\d+}/orbat", clear_orbat)
    add.add_post("/api/operations/{op_id:\\d+}/slots/{slot_id:\\d+}/take", take_slot)
    add.add_post("/api/operations/{op_id:\\d+}/slots/{slot_id:\\d+}/assign", assign_slot)
    add.add_put("/api/operations/{op_id:\\d+}/slots/{slot_id:\\d+}/lock", lock_slot)
    add.add_delete("/api/operations/{op_id:\\d+}/slots/{slot_id:\\d+}/holder", remove_holder)
    add.add_put("/api/operations/{op_id:\\d+}/signup", answer)
    add.add_delete("/api/operations/{op_id:\\d+}/signup", leave)
    add.add_put("/api/operations/{op_id:\\d+}/signups/{person_id:\\d+}", place_person)
    add.add_post("/api/operations/{op_id:\\d+}/offers/{offer_id:\\d+}/accept", accept_offer)
    add.add_post("/api/operations/{op_id:\\d+}/offers/{offer_id:\\d+}/decline", decline_offer)
    add.add_post("/api/operations/{op_id:\\d+}/offers/{offer_id:\\d+}/delivered", offer_delivered)
    add.add_get("/api/operations/templates", list_templates)
    add.add_post("/api/operations/templates", save_template)
    add.add_patch("/api/operations/templates/{template_id:\\d+}", change_template)
    add.add_delete("/api/operations/templates/{template_id:\\d+}", delete_template)
    add.add_get("/api/operations/requests", list_requests)
    add.add_post("/api/operations/requests/{request_id:\\d+}/approve", approve_request)
    add.add_post("/api/operations/requests/{request_id:\\d+}/reject", reject_request)
    add.add_post("/api/operations/{op_id:\\d+}/submit", submit)
    add.add_post("/api/operations/{op_id:\\d+}/withdraw", withdraw)
    add.add_post("/api/operations/{op_id:\\d+}/end", end_operation)
    add.add_put("/api/operations/{op_id:\\d+}/attendance", correct_attendance)
    add.add_put("/api/operations/{op_id:\\d+}/rating", rate)
    add.add_get("/api/operations/reports/member/{who}", report_member)
    add.add_get("/api/operations/reports/maker/{who}", report_maker)
    add.add_get("/api/operations/reports/group/{group_id:\\d+}", report_group)
    add.add_get("/api/operations/reports/attendance", report_attendance)
    add.add_post("/api/members/erase", erase_member)
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=int(sys.argv[1]) if len(sys.argv) > 1 else 8099)
