"""The event profile (docs/PLAN.md, *Supervisor: event profile*), without discord.py.

- **Mission.** Uploaded (``/event mission file``) or picked from the server's missions. Overwriting a file of the
  same name is allowed only when no other profile uses it. The supervisor's inspection is kept trimmed
  (:func:`trim`); while its addon index is still being built, the index job is followed and the report read again.
- **Warnings** (:meth:`ProfileService.warnings`), which never block: mods or Creator DLC the mission needs that
  the preset lacks, fewer playable slots than ORBAT slots, an error status, an incomplete index. They go to the
  staff log whenever a report is read.
- **Build.** The base profile (config) is cloned once (``EVT <channel> <title>``), the launcher preset imported as
  its mods (``replace``, ``download=missing``: a supervisor job, followed until it ends), and the server config set:
  the mission as the only entry of the cycle, the hostname, max players and signatures. Changing the mission, the
  preset or the settings later builds the same profile again. Publishing builds it if it hasn't been.
- **On the server.** With ``reservation_profile`` the reservation carries the profile. Without it, the event
  server's assigned profile is swapped ``profile_swap_minutes`` before the reservation and given back at its end,
  but only while the server still has the event profile.
- **Cleanup.** ``profile_keep_days`` after the event (unless *keep*), at once when it is cancelled. A refused
  delete (the profile still runs) is retried daily; after seven refusals in a row the staff log hears of it.

Refusals are :class:`ProfileError` codes (with the supervisor's message where it has one)."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import func, select

from app.db import Database
from app.db.models import Event, Slot
from app.events.service import RESERVED
from app.permissions import Perm
from app.reservations.service import MANAGED_BY
from app.scheduler import Action, Scheduler
from app.settings import SettingsStore
from app.signup.service import Member
from app.supervisor_client import SupervisorClient, SupervisorError, SupervisorUnreachable

log = logging.getLogger(__name__)
JOB, INDEX, SWAP, RESTORE, CLEANUP = "profile_job", "mission_index", "profile_swap", "profile_restore", "profile_cleanup"
JOB_POLL = timedelta(seconds=30)
INDEX_POLL = timedelta(minutes=1)
DELETE_RETRY = timedelta(days=1)
WARN_AFTER = 7  # refused deletes in a row before the staff log hears
MAX_MISSION = 100 * 1024 * 1024
FINISHED = ("succeeded", "failed", "cancelled")
NAME_LIMIT = 100


class Problem(StrEnum):
    NOT_ALLOWED = "not_allowed"  # mission maker role or manage any event, and an organiser
    NOT_RESERVED = "not_reserved"  # a guest event never touches the supervisor
    NO_MISSION = "no_mission"
    NOT_PBO = "not_pbo"
    NAME_IN_USE = "name_in_use"  # another profile uses a mission of that file name
    UNKNOWN_MISSION = "unknown_mission"
    NO_BASE = "no_base"  # BOT_SUPERVISOR__BASE_PROFILE names no profile
    BAD_NUMBER = "bad_number"
    SUPERVISOR = "supervisor"  # the supervisor's own message


class ProfileError(Exception):
    def __init__(self, problem: Problem, message: str = "") -> None:
        super().__init__(message or problem.value)
        self.problem, self.message = problem, message


class ProfileNotifier(Protocol):
    async def profile_failed(self, event_id: int, reason: str) -> None: ...  # organisers in the thread, staff
    async def profile_warnings(self, event_id: int, warnings: list[tuple[str, str]]) -> None: ...  # staff
    async def profile_note(self, event_id: int, code: str) -> None: ...  # staff: no profile at the swap, refusals


def template(name: str | None) -> str | None:
    """The mission cycle's ``template``: the file name without ``.pbo``."""
    if not name:
        return None
    return name[:-4] if name.lower().endswith(".pbo") else name


def trim(report: dict[str, Any]) -> dict[str, Any]:
    """What the panel and the warnings need of the supervisor's mission report."""
    meta = report.get("meta") or {}
    needs = {}
    for requirement in report.get("requirements") or ():
        for provider in requirement.get("providers") or ():
            if provider.get("kind") in ("dlc", "mod"):
                needs[(provider["kind"], provider.get("key"))] = provider.get("name") or provider.get("key")
    index = report.get("index") or {}
    return {
        "name": report.get("name"), "terrain": report.get("terrain"), "status": report.get("status"),
        "title": meta.get("briefingName") or meta.get("title") or report.get("template"),
        "author": meta.get("author"), "playable": report.get("playable"),
        "needs": [{"kind": kind, "key": key, "name": name} for (kind, key), name in sorted(needs.items(), key=str)],
        "errors": [i.get("message") for i in report.get("issues") or () if i.get("severity") == "error"][:5],
        "index_built": bool(index.get("built")), "index_job_id": index.get("job_id"),
    }


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


class ProfileService:
    def __init__(self, db: Database, store: SettingsStore, scheduler: Scheduler, client: SupervisorClient,
                 events: Any, notifier: ProfileNotifier, clock: Callable[[], datetime] | None = None) -> None:
        """``events``: the EventService (who organises, the reservation's window, the channel's name)."""
        self.db, self.store, self.scheduler, self.client, self.events = db, store, scheduler, client, events
        self.notifier = notifier
        self.clock = clock or scheduler.clock
        for kind, handler in ((JOB, self._job), (INDEX, self._index), (SWAP, self._swap), (RESTORE, self._restore),
                              (CLEANUP, self._cleanup)):
            scheduler.register(kind, handler)

    @property
    def settings(self) -> Any:
        return self.store.current

    # ---------------------------------------------------------------- reading

    def may_manage(self, member: Member) -> bool:
        access = self.events.access(member)
        return access.is_mission_maker or access.has(Perm.MANAGE_ANY_EVENT)

    async def _event(self, event_id: int) -> Event:
        return await self.events.get(event_id)

    async def _editable(self, event_id: int, member: Member) -> Event:
        event = await self.events.editable(event_id, member)
        if event.kind not in RESERVED:
            raise ProfileError(Problem.NOT_RESERVED)
        if not self.may_manage(member):
            raise ProfileError(Problem.NOT_ALLOWED)
        return event

    async def slots(self, event: Event) -> int:
        if event.kind == "zeus":
            return event.signup_limit or 0
        async with self.db.session() as session:
            return int((await session.execute(select(func.count(Slot.id)).where(Slot.event_id == event.id))).scalar_one())

    async def defaults(self, event: Event) -> tuple[str, int | None]:
        """The hostname (our acronym and the title) and max players (the mission's playable slots, else the ORBAT's
        slots or a Zeus event's limit) when the mission maker set none."""
        hostname = f"{self.settings.group.acronym} | {event.title}"
        playable = (event.mission_report or {}).get("playable")
        return hostname, playable or (await self.slots(event)) or None

    async def effective(self, event: Event) -> tuple[str, int | None]:
        hostname, players = await self.defaults(event)
        return event.hostname or hostname, event.max_players or players

    async def warnings(self, event: Event) -> list[tuple[str, str]]:
        """``(code, detail)``: missing_dlc, missing_mods, no_preset, few_slots, mission_error, index_incomplete."""
        report = event.mission_report or {}
        if not report:
            return []
        found = []
        needs = report.get("needs") or []
        dlc = [n["name"] for n in needs if n["kind"] == "dlc" and n["key"] not in (event.required_dlc or ())]
        if dlc:
            found.append(("missing_dlc", ", ".join(dlc)))
        mods = [n for n in needs if n["kind"] == "mod"]
        if mods and not event.preset_mods:
            found.append(("no_preset", ", ".join(n["name"] for n in mods)[:500]))
        elif mods:
            have = {_plain(name) for _, name in event.preset_mods}
            missing = [n["name"] for n in mods if _plain(n["name"]) not in have and _plain(n["key"] or "") not in have]
            if missing:
                found.append(("missing_mods", ", ".join(missing)[:500]))
        playable, slots = report.get("playable"), await self.slots(event)
        if playable is not None and event.kind != "zeus" and playable < slots:
            found.append(("few_slots", f"{playable}/{slots}"))
        if report.get("status") == "error":
            found.append(("mission_error", "; ".join(report.get("errors") or ())[:500]))
        if not report.get("index_built") or report.get("index_job_id"):
            found.append(("index_incomplete", ""))
        return found

    async def missions(self) -> list[dict[str, Any]]:
        return await self.client.missions()

    # ---------------------------------------------------------------- the mission and the settings

    async def upload_mission(self, event_id: int, member: Member, filename: str, content: bytes) -> Event:
        event = await self._editable(event_id, member)
        if not filename.lower().endswith(".pbo") or "/" in filename or "\\" in filename:
            raise ProfileError(Problem.NOT_PBO)
        try:
            existing = next((m for m in await self.client.missions() if m["name"].lower() == filename.lower()), None)
            ours = (await self.client.profile(event.profile_id))["name"] if event.profile_id else None
            if existing is not None and any(name != ours for name in existing.get("profiles") or ()):
                raise ProfileError(Problem.NAME_IN_USE)
            saved = await self.client.upload_mission(filename, content, overwrite=existing is not None)
        except SupervisorError as exc:
            raise ProfileError(Problem.SUPERVISOR, exc.message) from exc
        return await self._mission(event, saved["name"], saved.get("report") or {})

    async def pick_mission(self, event_id: int, member: Member, name: str) -> Event:
        event = await self._editable(event_id, member)
        try:
            known = {m["name"] for m in await self.client.missions()}
            if name not in known:
                raise ProfileError(Problem.UNKNOWN_MISSION)
            report = await self.client.mission_report(name)
        except SupervisorError as exc:
            raise ProfileError(Problem.SUPERVISOR, exc.message) from exc
        return await self._mission(event, name, report)

    async def _mission(self, event: Event, name: str, report: dict[str, Any]) -> Event:
        await self._save(event.id, mission_name=name, mission_report=trim(report))
        event = await self._report_read(event.id)
        await self.changed(event.id)
        return event

    async def _report_read(self, event_id: int) -> Event:
        """A report is in: staff hear the warnings; an index still being built is followed."""
        event = await self._event(event_id)
        job_id = (event.mission_report or {}).get("index_job_id")
        if job_id:
            await self.scheduler.schedule(INDEX, self.clock() + INDEX_POLL, key=f"{INDEX}:{event_id}", event_id=event_id,
                                          payload={"job": job_id})
        warnings = await self.warnings(event)
        if warnings:
            await self.notifier.profile_warnings(event_id, warnings)
        return event

    async def set_settings(self, event_id: int, member: Member, hostname: str, max_players: str, signatures: bool) -> None:
        await self._editable(event_id, member)
        players = max_players.strip()
        if players and (not players.isdigit() or not 1 <= int(players) <= 256):
            raise ProfileError(Problem.BAD_NUMBER)
        await self._save(event_id, hostname=hostname.strip()[:200] or None, max_players=int(players) if players else None,
                         verify_signatures=signatures)
        await self.changed(event_id)

    async def set_keep(self, event_id: int, member: Member, keep: bool) -> None:
        event = await self.events.get(event_id)
        if not await self.events.can_organise(event, member) or not self.may_manage(member):
            raise ProfileError(Problem.NOT_ALLOWED)
        await self._save(event_id, profile_keep=keep)

    # ---------------------------------------------------------------- building

    async def changed(self, event_id: int) -> None:
        """The mission, the preset or the settings changed: a built profile is built again."""
        event = await self._event(event_id)
        if event.profile_id is not None and event.status in ("draft", "pending", "published"):
            await self._build_quietly(event_id)

    async def build(self, event_id: int, member: Member) -> Event:
        """*Build profile*: refusals and the supervisor's errors are raised for the panel to show."""
        await self._editable(event_id, member)
        return await self._build(event_id)

    async def _build_quietly(self, event_id: int) -> None:
        try:
            await self._build(event_id)
        except ProfileError as exc:
            log.info("Event %s: profile not built: %s", event_id, exc)

    async def _base(self) -> dict[str, Any]:
        wanted = (self.settings.supervisor.base_profile or "").strip()
        if not wanted:
            raise ProfileError(Problem.NO_BASE)
        for profile in await self.client.profiles():
            if str(profile["id"]) == wanted or profile["name"].casefold() == wanted.casefold():
                return profile
        raise ProfileError(Problem.NO_BASE)

    async def _build(self, event_id: int) -> Event:
        event = await self._event(event_id)
        if not event.mission_name:
            raise ProfileError(Problem.NO_MISSION)
        hostname, players = await self.effective(event)
        try:
            await self.client.ensure_info()
            profile_id = event.profile_id
            if profile_id is None:
                body: dict[str, Any] = {"name": f"EVT {await self.events.channel_name(event)} {event.title}"[:NAME_LIMIT]}
                if self.client.info.has("managed_refs"):
                    body |= {"managed_by": MANAGED_BY, "external_ref": f"event:{event.id}"}
                profile_id = (await self.client.clone_profile((await self._base())["id"], body))["id"]
                await self._save(event_id, profile_id=profile_id)
            server: dict[str, Any] = {"missions": [{"template": template(event.mission_name)}], "hostname": hostname,
                                      "verify_signatures": 2 if event.verify_signatures else 0}
            if players:
                server["max_players"] = players
            await self.client.patch_profile_config(profile_id, "server", server)
            job_id = None
            preset = Path(event.preset_file) if event.preset_file else None
            if preset is not None and preset.is_file():
                job_id = (await self.client.import_preset(profile_id, preset.read_bytes(), preset.name))["job"]["id"]
        except SupervisorError as exc:
            await self._failed(event_id, exc.message)
            raise ProfileError(Problem.SUPERVISOR, exc.message) from exc
        await self._save(event_id, profile_state="building" if job_id else "built", profile_job_id=job_id,
                         profile_error=None, profile_progress={})
        if job_id:
            await self.scheduler.schedule(JOB, self.clock(), key=f"{JOB}:{event_id}", event_id=event_id)
        else:
            await self.events.refresh_reservation(event_id)  # the reservation carries the profile now
        return await self._event(event_id)

    async def _failed(self, event_id: int, reason: str) -> None:
        await self._save(event_id, profile_state="failed", profile_error=reason[:2000])
        await self.notifier.profile_failed(event_id, reason)

    async def _job(self, action: Action, now: datetime) -> datetime | None:
        event = await self._event(action.event_id)
        if event.profile_job_id is None or event.profile_state != "building":
            return None
        try:
            job = await self.client.job(event.profile_job_id)
        except SupervisorUnreachable:
            return now + JOB_POLL
        progress = job.get("progress") or {}
        if job.get("status") == "succeeded":
            await self._save(event.id, profile_state="built", profile_progress={})
            await self.events.refresh_reservation(event.id)
            return None
        if job.get("status") in FINISHED:
            await self._failed(event.id, job.get("error") or job.get("message") or job.get("status"))
            return None
        await self._save(event.id, profile_progress={
            "status": job.get("status"), "items_total": progress.get("items_total"),
            "items_pending": progress.get("items_pending"), "bytes_done": progress.get("bytes_done"),
            "bytes_total": progress.get("bytes_total")})
        return now + JOB_POLL

    async def _index(self, action: Action, now: datetime) -> datetime | None:
        """The addon index the first report queued: once its job ends, the report is read again."""
        event = await self._event(action.event_id)
        if not event.mission_name:
            return None
        try:
            job = await self.client.job(int(action.payload["job"]))
            if job.get("status") not in FINISHED:
                return now + INDEX_POLL
            report = await self.client.mission_report(event.mission_name)
        except SupervisorError:
            return now + INDEX_POLL
        trimmed = trim(report)
        trimmed["index_job_id"] = None if trimmed.get("index_job_id") == action.payload["job"] else trimmed["index_job_id"]
        await self._save(event.id, mission_report=trimmed)
        await self._report_read(event.id)  # (a new index job: it reschedules this action with that job)
        return now + INDEX_POLL if trimmed["index_job_id"] else None

    # ---------------------------------------------------------------- the event's life

    async def published(self, event: Event) -> None:
        """Published, or its times moved: build it if it hasn't been; the swap fallback's times."""
        if event.kind not in RESERVED:
            return
        if event.mission_name and event.profile_id is None:
            await self._build_quietly(event.id)
        booked = await self.events.reservations.get(event.id)
        if booked is not None:
            minutes = self.settings.supervisor.profile_swap_minutes
            await self.scheduler.schedule(SWAP, booked.starts_at - timedelta(minutes=minutes), key=f"{SWAP}:{event.id}",
                                          event_id=event.id)
            await self.scheduler.schedule(RESTORE, booked.ends_at, key=f"{RESTORE}:{event.id}", event_id=event.id)
        await self.scheduler.schedule(CLEANUP, event.start_at + timedelta(
            minutes=event.duration_minutes) + timedelta(days=self.settings.supervisor.profile_keep_days),
            key=f"{CLEANUP}:{event.id}", event_id=event.id)

    async def cancelled(self, event_id: int) -> None:
        """Cancelled: no swap, and the profile goes at once (after the reservation, which the supervisor needs gone)."""
        await self.scheduler.cancel(f"{SWAP}:{event_id}")
        await self.scheduler.cancel(f"{JOB}:{event_id}")
        await self.scheduler.schedule(CLEANUP, self.clock(), key=f"{CLEANUP}:{event_id}", event_id=event_id,
                                      payload={"cancelled": True})

    async def _swap(self, action: Action, now: datetime) -> None:
        event = await self._event(action.event_id)
        if event.status != "published":
            return
        await self.client.ensure_info()
        if self.client.info.has("reservation_profile"):
            return  # the reservation carries the profile
        if event.profile_state != "built" or event.profile_id is None:
            await self.notifier.profile_note(event.id, "not_built")
            return
        slug = self.settings.supervisor.event_server
        current = await self._assigned(slug)
        await self.client.assign_profile(slug, event.profile_id)
        await self._save(event.id, previous_profile_id=current, profile_swapped=True)

    async def _assigned(self, slug: str | None) -> int | None:
        for row in await self.client.servers():
            info = row.get("server", {})
            if slug in (info.get("slug"), str(info.get("id"))):
                return (info.get("assigned_profile") or {}).get("id")
        raise LookupError(f"The supervisor has no server {slug}")

    async def _restore(self, action: Action, now: datetime) -> None:
        event = await self._event(action.event_id)
        if not event.profile_swapped:
            return
        slug = self.settings.supervisor.event_server
        if await self._assigned(slug) == event.profile_id:  # changed by hand in between: left alone
            await self.client.assign_profile(slug, event.previous_profile_id)
        await self._save(event.id, profile_swapped=False)

    async def _cleanup(self, action: Action, now: datetime) -> datetime | None:
        event = await self._event(action.event_id)
        if event.profile_id is None or (event.profile_keep and not action.payload.get("cancelled")):
            return None
        if event.profile_swapped:
            await self._restore(action, now)
        try:
            await self.client.delete_profile(event.profile_id)
        except SupervisorUnreachable:
            raise  # the scheduler tries again
        except SupervisorError as exc:
            if exc.status != 404:
                refusals = int(action.payload.get("refusals", 0)) + 1
                # (the count goes into the row's payload; the returned time is what the scheduler keeps)
                await self.scheduler.schedule(CLEANUP, now + DELETE_RETRY, key=f"{CLEANUP}:{event.id}", event_id=event.id,
                                              payload={**action.payload, "refusals": refusals})
                if refusals == WARN_AFTER:
                    await self.notifier.profile_note(event.id, "delete_refused")
                return now + DELETE_RETRY
        await self._save(event.id, profile_id=None, profile_state=None, profile_job_id=None, profile_progress={})
        return None

    # ---------------------------------------------------------------- storage

    async def _save(self, event_id: int, **values: Any) -> None:
        async with self.db.session() as session, session.begin():
            row = await session.get(Event, event_id)
            for name, value in values.items():
                setattr(row, name, value)
