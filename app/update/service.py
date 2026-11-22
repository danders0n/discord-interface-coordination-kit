"""Updating and restarting the bot from Discord (docs/PLAN-remote-update.md, *The update service*). No discord.py.

This side runs in the live bot: it fetches the branch, says what an update would bring, holds the scheduler and waits
for its running action, backs up the database, writes the marker for :mod:`app.boot` and asks ``python -m app`` to
exit with the restart code (:mod:`app.restarter`). The files are changed by the boot step of the next start, which
also rolls a failed start back. What happened is read back from ``<data_dir>/updates/history.jsonl`` at the next
start and told in the staff log (:meth:`Updater.report`).

Refusals are :class:`UpdateError` with a ``code`` the cog words (app/update/cog.py) and git's own message as
``detail``, with the token removed."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app import boot, restarter, settings as settings_module
from app.i18n import t

if TYPE_CHECKING:
    from app.scheduler import Scheduler
    from app.settings import Settings, SettingsStore
    from app.staff_log import StaffLog

log = logging.getLogger(__name__)

GIT_TIMEOUT = 120
WAIT_POLL = 2.0
FLUSH_SECONDS = 10.0
MAX_COMMITS = 200
MIGRATIONS = "app/db/migrations/versions"
BRANCH = re.compile(r"^(?!-)(?!.*\.\.)[A-Za-z0-9._/-]+$")
SEP = "\x1f"


class UpdateError(Exception):
    """``code``: not_checkout, no_git, no_restarter, busy, bad_env, bad_branch, git, up_to_date, moved,
    nothing_back, commit_gone, no_backup, needs_restore, nothing_waiting, too_late."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def short(sha: Any) -> str:
    return boot.short(sha)


def env_problem(store: SettingsStore) -> str | None:
    """The first problem of `.env` as it is on disk now, or None: an update or restart would start with it."""
    try:
        settings_module.load(store.env_file)
    except ValidationError as exc:
        return (settings_module.describe_errors(exc) or [str(exc)])[0]
    return None


class Updater:
    def __init__(self, store: SettingsStore, scheduler: Scheduler, staff: StaffLog, root: Path = boot.ROOT,
                 detect: Callable[[], str | None] = restarter.detect,
                 can_exit: Callable[[], bool] = restarter.can_exit,
                 request_exit: Callable[[], bool] = restarter.request_exit,
                 check_env: Callable[[], str | None] | None = None) -> None:
        self.store, self.scheduler, self.staff, self.root = store, scheduler, staff, root
        self.boot = boot.Boot(root)  # only its marker file and log: the steps run at the next start
        self._detect, self._can_exit, self._request_exit = detect, can_exit, request_exit
        self._check_env = check_env or (lambda: env_problem(store))
        # Before the exit, with the plan (kind, to, requirements ...): the presence, later the heartbeat.
        self.announce: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self.last_check: dict[str, Any] | None = None
        self.waiting: dict[str, Any] | None = None  # an update / restart waiting for the scheduler
        self._task: asyncio.Task[None] | None = None
        self._starting = False  # a start between its checks and `waiting`: a second one must not pass meanwhile
        self.exiting = False

    @property
    def settings(self) -> Settings:
        return self.store.current

    # ---------------------------------------------------------------- git

    @property
    def git_exe(self) -> str | None:
        return shutil.which("git")

    @property
    def checkout(self) -> bool:
        return (self.root / ".git").exists()

    def _token(self) -> str:
        token = self.settings.update.github_token
        return token.get_secret_value() if token else ""

    def _env(self, auth: bool) -> dict[str, str]:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        token = self._token()
        if auth and token:  # through the environment: not in the command line, never in .git/config
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            env |= {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}"}
        return env

    def _clean(self, text: str) -> str:
        token = self._token()
        if token:
            text = text.replace(token, "***").replace(base64.b64encode(f"x-access-token:{token}".encode()).decode(), "***")
        return text.strip()

    def _git(self, *args: str, auth: bool = False, check: bool = True) -> tuple[int, str]:
        exe = self.git_exe
        if exe is None:
            raise UpdateError("no_git")
        try:
            done = subprocess.run([exe, "-C", str(self.root), *args], capture_output=True, text=True, errors="replace",
                                  timeout=GIT_TIMEOUT, check=False, env=self._env(auth))
        except subprocess.TimeoutExpired as exc:
            raise UpdateError("git", f"git {args[0]} took longer than {GIT_TIMEOUT} s") from exc
        except OSError as exc:
            raise UpdateError("git", f"git {args[0]}: {exc}") from exc
        out = self._clean(done.stdout if done.returncode == 0 else done.stderr or done.stdout)
        if check and done.returncode != 0:
            raise UpdateError("git", f"git {args[0]}: {out.splitlines()[-1] if out else f'exit code {done.returncode}'}")
        return done.returncode, out

    def head(self) -> str | None:
        try:
            return self._git("rev-parse", "HEAD")[1] or None
        except UpdateError:
            return None

    def commits(self, spec: str) -> list[dict[str, Any]]:
        out = self._git("log", f"--max-count={MAX_COMMITS}", f"--format=%H{SEP}%an{SEP}%cI{SEP}%s", spec)[1]
        found = []
        for line in out.splitlines():
            sha, author, date, subject = (line.split(SEP) + ["", "", ""])[:4]
            found.append({"sha": sha, "author": author, "date": date, "subject": subject})
        return found

    def branch(self) -> str:
        """``update.branch``, or the branch checked out when that is empty."""
        branch = self.settings.update.branch.strip()
        if not branch:
            branch = self._git("rev-parse", "--abbrev-ref", "HEAD")[1]
            if branch == "HEAD":
                raise UpdateError("bad_branch", "the checkout is on no branch: set BOT_UPDATE__BRANCH")
        if not BRANCH.match(branch):
            raise UpdateError("bad_branch", branch)
        return branch

    def _migrations_differ(self, a: str, b: str) -> bool:
        return bool(self._git("diff", "--name-only", a, b, "--", MIGRATIONS)[1].strip())

    # ---------------------------------------------------------------- what is possible

    def restart_problem(self) -> str | None:
        """A code (no_restarter) when the bot can't restart itself."""
        return "no_restarter" if not self._can_exit() or self._detect() is None else None

    def update_problem(self) -> str | None:
        if not self.checkout:
            return "not_checkout"
        if self.git_exe is None:
            return "no_git"
        return self.restart_problem()

    # ---------------------------------------------------------------- status and check

    @property
    def updates_dir(self) -> Path:
        return self.settings.data_dir / "updates"

    @property
    def history_file(self) -> Path:
        return self.updates_dir / "history.jsonl"

    def history(self) -> list[dict[str, Any]]:
        """Newest first."""
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries[::-1]

    def go_back_target(self, head: str | None) -> dict[str, Any] | None:
        """The last update or go back, when it worked and the checkout is still at its commit: what *Go back*
        returns to (``to``) and the backup taken before it, if it is still there."""
        entry = next((e for e in self.history() if e.get("kind") in ("update", "back")), None)
        if entry is None or entry.get("outcome") != "ok" or not head or entry.get("to") != head or not entry.get("from"):
            return None
        backup = entry.get("backup")
        return {"from": entry["to"], "to": entry["from"], "backup": backup if backup and Path(backup).is_file() else None}

    def status(self) -> dict[str, Any]:
        head = self.head() if self.checkout and self.git_exe else None
        marker = self.boot.marker()
        try:
            branch = self.branch() if head else None
        except UpdateError:
            branch = None
        return {
            "head": head,
            "branch": branch,
            "restarter": self._detect(),
            "restart_problem": self.restart_problem(),
            "update_problem": self.update_problem(),
            "waiting": self.waiting,
            "exiting": self.exiting,
            "marker": {k: marker.get(k) for k in ("kind", "state", "from", "to", "error")} if marker else None,
            "go_back": self.go_back_target(head),
            "history": self.history()[:5],
        }

    def check(self) -> dict[str, Any]:
        """Fetch the branch and compare: what an update would bring, and what it would throw away."""
        if not self.checkout:
            raise UpdateError("not_checkout")
        branch = self.branch()
        remote = f"refs/remotes/origin/{branch}"
        self._git("fetch", "--no-tags", "origin", f"+refs/heads/{branch}:{remote}", auth=True)
        head = self._git("rev-parse", "HEAD")[1]
        tip = self._git("rev-parse", remote)[1]
        dirty = self._git("diff", "--name-only", "HEAD")[1].splitlines()  # tracked files, staged or not
        code, _ = self._git("diff", "--quiet", head, tip, "--", "requirements.txt", check=False)
        self.last_check = {
            "branch": branch, "head": head, "tip": tip,
            "commits": self.commits(f"{head}..{tip}"),
            "local_commits": self.commits(f"{tip}..{head}"),
            "dirty": dirty,
            "requirements": code != 0 or "requirements.txt" in dirty,
            "migrations": self._migrations_differ(head, tip),
            "checked_at": _now(),
        }
        return self.last_check

    # ---------------------------------------------------------------- update, go back, restart

    async def start(self, kind: str, actor: str, expect: str | None = None, now: bool = False,
                    restore_db: bool = False) -> dict[str, Any]:
        """``kind``: ``update`` (to the fetched tip; ``expect``: the tip the caller saw), ``back`` or ``restart``.
        Waits for the scheduler's running action in the background (``now``: does not wait). ``actor``: who, as the
        staff log names them."""
        if self._starting or self.waiting is not None or self.exiting:
            raise UpdateError("busy")
        self._starting = True
        try:
            return await self._start(kind, actor, expect, now, restore_db)
        finally:
            self._starting = False

    async def _start(self, kind: str, actor: str, expect: str | None, now: bool, restore_db: bool) -> dict[str, Any]:
        problem = self.restart_problem() if kind == "restart" else self.update_problem()
        if problem:
            raise UpdateError(problem)
        env = await asyncio.to_thread(self._check_env)
        if env:
            raise UpdateError("bad_env", env)
        plan: dict[str, Any] = {"kind": kind, "actor": actor, "requested_at": _now(), "restore_db": None,
                                "requirements": False, "migrations": False}
        if kind == "update":
            check = await asyncio.to_thread(self.check)
            if expect and check["tip"] != expect:
                raise UpdateError("moved")
            if check["tip"] == check["head"]:
                raise UpdateError("up_to_date")
            plan |= {"from": check["head"], "to": check["tip"], "branch": check["branch"],
                     "requirements": check["requirements"], "migrations": check["migrations"],
                     "dirty": bool(check["dirty"])}
        elif kind == "back":
            plan |= await asyncio.to_thread(self._back_plan, restore_db)
        elif kind == "restart":
            plan |= {"from": None, "to": None}
        else:
            raise ValueError(f"Unknown kind {kind!r}")
        self.scheduler.hold()
        running = self.scheduler.running
        self.waiting = {"kind": kind, "actor": actor, "to": plan.get("to"), "since": plan["requested_at"],
                        "action": running.kind if running is not None and not now else None,
                        "minutes": self.settings.update.wait_minutes}
        self._task = asyncio.create_task(self._go(plan, now))
        return dict(self.waiting)

    def _back_plan(self, restore_db: bool) -> dict[str, Any]:
        head = self.head()
        target = self.go_back_target(head)
        if target is None:
            raise UpdateError("nothing_back")
        code, _ = self._git("cat-file", "-e", f"{target['to']}^{{commit}}", check=False)
        if code != 0:
            raise UpdateError("commit_gone", short(target["to"]))
        migrations = self._migrations_differ(target["from"], target["to"])
        if migrations and not restore_db:
            raise UpdateError("needs_restore")  # the older code cannot open a database migrated past it
        if restore_db and not target["backup"]:
            raise UpdateError("no_backup")
        code, _ = self._git("diff", "--quiet", target["from"], target["to"], "--", "requirements.txt", check=False)
        dirty = self._git("diff", "--name-only", "HEAD")[1]
        return {"from": target["from"], "to": target["to"], "branch": self.branch(), "requirements": code != 0,
                "migrations": migrations, "dirty": bool(dirty.strip()),
                "restore_db": target["backup"] if restore_db else None}

    async def hurry(self) -> None:
        """Stop waiting for the running action: it is cancelled by the exit and runs again after the restart."""
        if self.waiting is None or self._task is None:
            raise UpdateError("nothing_waiting")
        self.waiting["now"] = True

    async def cancel(self, actor: str) -> dict[str, Any]:
        if self.waiting is None or self._task is None:
            raise UpdateError("nothing_waiting")
        if self.exiting:
            raise UpdateError("too_late")
        self._task.cancel()
        cancelled, self.waiting = self.waiting, None
        self.scheduler.release()
        self.staff.post(t("The bot's {kind} was cancelled by {member}.", self.settings.language,
                          kind=self.kind_word(cancelled["kind"]), member=actor))
        return cancelled

    def kind_word(self, kind: str) -> str:
        language = self.settings.language
        return {"update": t("update", language), "back": t("go back", language)}.get(kind) or t("restart", language)

    async def _go(self, plan: dict[str, Any], now: bool) -> None:
        language = self.settings.language
        try:
            loop = asyncio.get_running_loop()
            end = loop.time() + self.settings.update.wait_minutes * 60
            while not now and not (self.waiting or {}).get("now") and self.scheduler.running is not None:
                if loop.time() >= end:
                    log.warning("The %s goes on although %s is still running", plan["kind"],
                                self.scheduler.running.kind)
                    break
                await asyncio.sleep(WAIT_POLL)
            self.exiting = True
            await asyncio.to_thread(self._prepare, plan)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - told to staff, the bot goes on
            log.exception("The bot's %s failed", plan["kind"])
            self.exiting, self.waiting = False, None
            self.scheduler.release()
            self.staff.post(t("The bot's {kind} failed: {error}", language, kind=self.kind_word(plan["kind"]),
                              error=str(exc)[:300]))
            return
        if plan["kind"] == "restart":
            text = t("The bot restarts (asked by {member}).", language, member=plan["actor"])
        else:
            text = t("The bot restarts for the {kind} to {commit} (asked by {member}).", language,
                     kind=self.kind_word(plan["kind"]), commit=short(plan["to"]), member=plan["actor"])
            if plan.get("requirements"):
                text += " " + t("Its dependencies change: this takes longer.", language)
        self.staff.post(text)
        if self.announce is not None:
            try:
                await self.announce(plan)
            except Exception:  # noqa: BLE001 - the restart goes on without it
                log.exception("Announcing the restart failed")
        await self.staff.flush(FLUSH_SECONDS)
        self._request_exit()

    def _prepare(self, plan: dict[str, Any]) -> None:
        """Backup, discarded changes and the marker (or, for a restart, its history line)."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        if plan["kind"] == "restart":
            head = self.head()
            self._append_history({"id": stamp, "kind": "restart", "from": head, "to": head, "actor": plan["actor"],
                                  "requested_at": plan["requested_at"], "outcome": "ok", "finished_at": _now(),
                                  "reported": False})
            return
        name = f"{stamp}-{short(plan['from'])}"
        backup = self._backup(self.updates_dir / f"{name}.db")
        diff = None
        if plan.get("dirty"):
            diff = self.updates_dir / f"{name}.diff"
            diff.write_text(self._git("diff", "HEAD")[1] + "\n", encoding="utf-8")
        self._trim(keep=tuple(p for p in (backup, Path(plan["restore_db"]) if plan.get("restore_db") else None) if p))
        self.boot.save({
            "id": stamp, "kind": plan["kind"], "from": plan["from"], "to": plan["to"], "branch": plan.get("branch"),
            "actor": plan["actor"], "requested_at": plan["requested_at"], "state": "pending", "attempts": 0,
            "requirements": plan["requirements"], "migrations": plan["migrations"],
            "restore_db": plan.get("restore_db"), "data_dir": str(self.settings.data_dir.resolve()),
            "db_file": str(self.settings.database_path.resolve()), "backup": str(backup) if backup else None,
            "diff": str(diff) if diff else None, "error": None,
        })
        self.boot.log(f"{plan['kind'].capitalize()} from {short(plan['from'])} to {short(plan['to'])} "
                      f"requested by {plan['actor']}")

    def _backup(self, target: Path) -> Path | None:
        source = self.settings.database_path
        if not source.is_file():
            return None
        with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)  # closed at once: Windows cannot delete a file still open
        return target

    def _trim(self, keep: tuple[Path, ...] = ()) -> None:
        """Keep the newest ``keep_backups`` updates' backups and diffs, and those of ``keep`` (the one just taken, the
        one about to be restored) whatever their age."""
        files = [p for p in self.updates_dir.iterdir() if p.suffix in (".db", ".diff")]
        ids = sorted({p.stem for p in files}, reverse=True)  # <stamp>-<commit>: newest first
        kept = set(ids[:self.settings.update.keep_backups]) | {p.stem for p in keep}
        for path in files:
            if path.stem not in kept:
                path.unlink(missing_ok=True)

    def _append_history(self, entry: dict[str, Any]) -> None:
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with self.history_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    # ---------------------------------------------------------------- at start-up

    def report(self) -> list[str]:
        """History lines not told yet (written by the boot step or before a restart), as staff-log notes in the
        config language; then marked told. Called once the bot is on Discord."""
        try:
            lines = self.history_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        notes, out, changed = [], [], False
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                out.append(line)
                continue
            if isinstance(entry, dict) and entry.get("reported") is False:
                notes.append(self.describe(entry))
                entry["reported"] = True
                changed = True
            out.append(json.dumps(entry))
        if changed:
            tmp = self.history_file.with_name("history.jsonl.tmp")
            tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
            os.replace(tmp, self.history_file)
        return notes

    def describe(self, entry: dict[str, Any]) -> str:
        language = self.settings.language
        kind, outcome = entry.get("kind"), entry.get("outcome")
        old, new, member = short(entry.get("from")), short(entry.get("to")), entry.get("actor") or "?"
        error = str(entry.get("error") or "?")
        if kind == "restart":
            return t("The bot restarted (asked by {member}).", language, member=member)
        if outcome == "ok":
            if kind == "update":
                text = t("The bot was updated from {old} to {new} (asked by {member}).", language, old=old, new=new,
                         member=member)
                try:
                    subjects = [c["subject"] for c in self.commits(f"{entry['from']}..{entry['to']}")][:10]
                except (UpdateError, KeyError):
                    subjects = []
                text += "".join(f"\n• {s}" for s in subjects)
            else:
                text = t("The bot went back from {old} to {new} (asked by {member}).", language, old=old, new=new,
                         member=member)
                if entry.get("db_restored"):
                    text += "\n" + t("Its database is as it was before that update.", language)
        elif outcome == "rolled_back":
            text = t("The bot's {kind} to {new} did not start ({error}): it is back on {old}.", language,
                     kind=self.kind_word(str(kind)), new=new, error=error, old=old)
            if entry.get("db_rolled_back"):
                text += "\n" + t("The database from before it was put back.", language)
        else:
            text = t("The bot's {kind} to {new} did not start ({error}), and going back to {old} failed too. It runs "
                     "as it is; see .app-update/boot.log.", language, kind=self.kind_word(str(kind)), new=new,
                     error=error, old=old)
        if entry.get("db_error"):
            text += "\n" + t("Database: {error}", language, error=entry["db_error"])
        return text
