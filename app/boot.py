"""The first thing ``python -m app`` runs: finishes an update of the bot, or takes it back, before any other part of the
bot is imported (docs/PLAN-remote-update.md, *The boot step*). Standard library only, and kept small: it runs before
pip has installed the new dependencies, and every version must be able to roll back to and from the others. Written
after the supervisor's ``app/boot.py`` (never imported from it).

The running bot asks for an update by writing the marker (``.app-update/pending.json`` in the install folder: a fixed
place, this module reads no settings) and exiting with ``RESTART_CODE``. Whatever starts the bot again (NSSM, or
:func:`console_loop`) comes here, which moves the marker on by its ``state``:

- ``pending``: reset the checkout to ``to``, install the dependencies when they changed, restore the database if asked,
  and exit again: the new code needs a fresh interpreter (this module is the old one);
- ``applied``: count the start and watch it; :func:`ready` (the end of the first ``on_ready``) closes the marker. A
  start that fails ``MAX_ATTEMPTS`` times goes back to ``from`` the same way (``rolling_back``, then
  ``rolled_back_applied``), putting back the database a restore replaced or new migrations may have changed;
- ``gave_up``: the old commit fails too; the bot is started as it is and nothing more is tried.

What happened goes to ``.app-update/boot.log`` and, one line per update, to ``<data_dir>/updates/history.jsonl``,
which the next start reports in the staff log."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ".app-update"
RESTART_CODE = 75  # EX_TEMPFAIL, as the supervisor's
RESTARTER_ENV = "BOT_RESTARTER"  # "console" for console_loop's child; "nssm" set in the service's environment
MAX_ATTEMPTS = 2
READY_TIMEOUT = 180.0
GIT_TIMEOUT = 120.0
PIP_TIMEOUT = 900.0
LOG_LIMIT = 1_000_000
HISTORY_FIELDS = ("id", "kind", "from", "to", "branch", "actor", "requested_at", "requirements", "migrations",
                  "backup", "diff", "restore_db", "error", "db_error", "db_restored", "db_rolled_back")

Runner = Callable[[list[str], float], tuple[int, str]]


def run_command(args: list[str], timeout: float) -> tuple[int, str]:
    """``(exit code, stdout + stderr)``; -1 when it could not run or timed out."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout, check=False,
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout:.0f} s"
    except OSError as exc:
        return -1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def short(sha: Any) -> str:
    return str(sha or "?")[:7]


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1].strip()[:300] if lines else "no output"


class Boot:
    def __init__(self, root: Path = ROOT, python: str = sys.executable, run: Runner = run_command,
                 ready_timeout: float = READY_TIMEOUT, exit: Callable[[int], Any] = os._exit) -> None:
        self.root = root
        self.dir = root / STATE_DIR
        self.marker_file = self.dir / "pending.json"
        self.log_file = self.dir / "boot.log"
        self.python = python
        self._run = run
        self.ready_timeout = ready_timeout
        self._exit = exit
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._errors: _ErrorKeeper | None = None

    # ---------------------------------------------------------------- marker and log

    def marker(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.marker_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            self.log(f"Unreadable {self.marker_file}: {exc}")
            return None
        return data if isinstance(data, dict) else None

    def save(self, marker: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.marker_file.with_name(f"pending.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.marker_file)

    def log(self, text: str) -> None:
        print(f"[boot] {text}", file=sys.stderr, flush=True)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            if self.log_file.is_file() and self.log_file.stat().st_size > LOG_LIMIT:
                tail = self.log_file.read_bytes()[-LOG_LIMIT // 2:]
                self.log_file.write_bytes(tail[tail.find(b"\n") + 1:])
            with self.log_file.open("a", encoding="utf-8") as handle:
                handle.write(f"{datetime.now().astimezone():%Y-%m-%d %H:%M:%S} {text}\n")
        except OSError:
            pass

    def note_error(self, text: str) -> None:
        """The latest error of a watched start, kept in the marker: the reason shown when it is rolled back."""
        with self._lock:
            marker = self.marker()
            if marker is None or marker.get("state") not in ("applied", "rolled_back_applied"):
                return
            marker["error"] = text[:500]
            try:
                self.save(marker)
            except OSError:
                pass

    def _history(self, marker: dict[str, Any], outcome: str) -> None:
        if not marker.get("data_dir"):
            return
        entry = {k: marker.get(k) for k in HISTORY_FIELDS}
        entry |= {"outcome": outcome, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "reported": False}
        path = Path(marker["data_dir"]) / "updates" / "history.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        except OSError as exc:
            self.log(f"Could not write {path}: {exc}")

    # ---------------------------------------------------------------- the steps

    def prepare(self) -> int | None:
        """None: start the bot in this process; else exit with that code (``RESTART_CODE``: start me again)."""
        marker = self.marker()
        if marker is None:
            return None
        state = marker.get("state")
        if state == "pending":
            return self._apply(marker, forward=True)
        if state == "rolling_back":
            return self._apply(marker, forward=False)
        if state in ("applied", "rolled_back_applied"):
            back = state == "rolled_back_applied"
            commit = marker.get("from" if back else "to")
            marker["attempts"] = int(marker.get("attempts") or 0) + 1
            if marker["attempts"] > MAX_ATTEMPTS:
                if back:
                    self.log(f"{short(commit)} does not start either: giving up (fix it by hand, then start again)")
                    marker["state"] = "gave_up"
                    self.save(marker)
                    self._history(marker, "gave_up")
                    return None
                self.log(f"{short(commit)} failed to start {MAX_ATTEMPTS} times: going back to {short(marker.get('from'))}")
                marker.update(state="rolling_back", attempts=0)
                self.save(marker)
                return self._apply(marker, forward=False)
            self.save(marker)
            self.log(f"Starting {short(commit)}, attempt {marker['attempts']} of {MAX_ATTEMPTS}")
            self._watch()
            return None
        if state != "gave_up":
            self.log(f"Unknown state {state!r} in {self.marker_file}: ignored")
        return None

    def _apply(self, marker: dict[str, Any], forward: bool) -> int | None:
        target = marker.get("to") if forward else marker.get("from")
        self.log(f"{'Updating' if forward else 'Going back'} to {short(target)}")
        error = self._checkout(target, bool(marker.get("requirements")))
        if error is None:
            if forward and marker.get("restore_db"):
                marker["db_error"] = self._restore_db(marker)
            elif not forward and (marker.get("db_restored") or marker.get("migrations")) \
                    and not marker.get("db_rolled_back"):
                # The code goes back onto the database it ran on: the one a restore replaced, or the one from before
                # migrations the failed start may have applied (Alembic refuses a revision it does not know).
                self._undo_restore(marker)
            marker.update(state="applied" if forward else "rolled_back_applied", attempts=0)
            self.save(marker)
            return RESTART_CODE
        marker["error"] = error
        if forward:
            self.log(f"The update failed ({error}): going back to {short(marker.get('from'))}")
            marker["state"] = "rolling_back"
            self.save(marker)
            return self._apply(marker, forward=False)
        self.log(f"Going back failed ({error}): giving up (fix it by hand, then start again)")
        marker["state"] = "gave_up"
        self.save(marker)
        self._history(marker, "gave_up")
        return None

    def _checkout(self, commit: Any, requirements: bool) -> str | None:
        """The checkout at ``commit`` with its dependencies installed; None, or what went wrong."""
        if not commit:
            return "no commit to go to"
        code, out = self._run(["git", "-C", str(self.root), "reset", "--hard", str(commit)], GIT_TIMEOUT)
        self.log(f"git reset --hard {short(commit)}: {_last_line(out)}")
        if code != 0:
            return f"git reset --hard {short(commit)}: {_last_line(out)}"
        _drop_bytecode(self.root / "app")
        if requirements:
            self.log("Installing the dependencies (pip install -r requirements.txt)")
            code, out = self._run([self.python, "-m", "pip", "install", "--disable-pip-version-check", "-r",
                                   str(self.root / "requirements.txt")], PIP_TIMEOUT)
            self.log(f"pip (exit code {code}):\n{out}")
            if code != 0:
                return f"pip install: {_last_line(out)}"
        return None

    def _restore_db(self, marker: dict[str, Any]) -> str | None:
        """The database from the backup ``restore_db``; None, or why it was not restored (the go back goes on, code
        only, and the older code then refuses a database migrated past it: two failed starts bring the newer one
        back). The bot's database holds no access rights, so unlike the supervisor's nothing is carried over. The
        live file is only replaced once the copy checked out."""
        source, target = Path(marker["restore_db"]), Path(marker.get("db_file") or "")
        tmp = target.with_name(f"{target.name}.restore.tmp")
        try:
            if not marker.get("db_file"):
                raise ValueError("no database file named")
            if not source.is_file():
                raise ValueError(f"no backup at {source}")
            if not marker.get("backup") or not Path(marker["backup"]).is_file():
                raise ValueError("no backup of the database it would replace")
            shutil.copyfile(source, tmp)
            _check(tmp)
            _swap_in(tmp, target)
        except (OSError, ValueError, sqlite3.Error) as exc:
            _discard(tmp)
            self.log(f"The database was not restored: {exc}")
            return f"not restored: {exc}"
        marker["db_restored"] = True
        self.log(f"Database restored from {source.name}")
        return None

    def _undo_restore(self, marker: dict[str, Any]) -> None:
        """A rollback puts back the database from before the marker's change (``backup``): the one a go back's restore
        replaced, or the one an update's new migrations may have changed."""
        backup, target = Path(marker.get("backup") or ""), Path(marker.get("db_file") or "")
        tmp = target.with_name(f"{target.name}.restore.tmp")
        try:
            if not marker.get("backup") or not backup.is_file() or not marker.get("db_file"):
                raise ValueError(f"no backup at {backup}")
            shutil.copyfile(backup, tmp)
            _swap_in(tmp, target)
        except (OSError, ValueError) as exc:
            _discard(tmp)
            self.log(f"The database from before the go back was not put back: {exc}")
            marker["db_error"] = f"not put back: {exc}"
            return
        marker["db_rolled_back"] = True
        self.log(f"Database put back from {backup.name}")

    # ---------------------------------------------------------------- watching a start

    def _watch(self) -> None:
        self._errors = _ErrorKeeper(self)
        logging.getLogger().addHandler(self._errors)
        previous = sys.excepthook

        def uncaught(kind: type[BaseException], exc: BaseException, tb: Any) -> None:
            """A crash nothing logged (an import error in the new code): its last line is the reason too."""
            if not self._ready.is_set():
                self.note_error(f"{kind.__name__}: {exc}")
            previous(kind, exc, tb)

        sys.excepthook = uncaught
        threading.Thread(target=self._watchdog, name="boot-watchdog", daemon=True).start()

    def _watchdog(self) -> None:
        if self._ready.wait(self.ready_timeout):
            return
        self.note_error(f"not ready after {self.ready_timeout:.0f} s")
        self.log(f"Not ready after {self.ready_timeout:.0f} s: stopping this start")
        self._exit(1)

    def ready(self) -> None:
        """The bot has started: close the marker of the update that got it here."""
        self._ready.set()
        if self._errors is not None:
            logging.getLogger().removeHandler(self._errors)
            self._errors = None
        with self._lock:
            marker = self.marker()
            if marker is None:
                return
            state = marker.get("state")
            if state in ("applied", "rolled_back_applied"):
                outcome = "ok" if state == "applied" else "rolled_back"
                self._history(marker, outcome)
                self.log(f"{short(marker.get('to' if outcome == 'ok' else 'from'))} started ({outcome.replace('_', ' ')})")
            elif state != "gave_up":  # pending / rolling_back: this process did not go through prepare()
                return
            self.marker_file.unlink(missing_ok=True)


def _drop_bytecode(folder: Path) -> None:
    """Python reuses a ``.pyc`` whose source has the same size and whole-second mtime: a file reset to another
    version within the same second (a quick go back) could run the old code. After a reset, compile afresh."""
    for cache in folder.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


# ---------------------------------------------------------------- restoring a database


def _check(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise ValueError(f"integrity check: {result}")


def _swap_in(tmp: Path, target: Path) -> None:
    for suffix in ("-wal", "-shm"):  # the replaced database's journal must not be replayed into this one
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    os.replace(tmp, target)


def _discard(tmp: Path) -> None:
    for path in (tmp, Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


class _ErrorKeeper(logging.Handler):
    """Keeps the latest error logged during a watched start (an exception in ``on_ready``, a failed migration) in the
    marker. A traceback logged as text counts by its last line."""

    def __init__(self, boot: Boot) -> None:
        super().__init__(logging.ERROR)
        self.boot = boot

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage().strip()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                text = f"{text}: {type(exc).__name__}: {exc}"
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if len(lines) > 1 and lines[0].startswith("Traceback"):
                text = lines[-1]
            self.boot.note_error(text)
        except Exception:  # noqa: BLE001 - a log handler must not raise
            pass


# ---------------------------------------------------------------- run from a console

def wants_console_loop(environ: Any = None, stdin: Any = None) -> bool:
    """``python -m app`` typed in a terminal: nothing else would start it again after a restart."""
    environ = os.environ if environ is None else environ
    stdin = sys.stdin if stdin is None else stdin
    if environ.get(RESTARTER_ENV) or environ.get("INVOCATION_ID"):  # the loop's child, NSSM; systemd
        return False
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _run_child(args: list[str], env: dict[str, str]) -> tuple[int, bool]:
    """``(exit code, Ctrl+C pressed)``. Ctrl+C reaches the child as well (same console): wait for it to stop."""
    process = subprocess.Popen(args, env=env)
    interrupted = False
    while True:
        try:
            return process.wait(), interrupted
        except KeyboardInterrupt:
            interrupted = True


def console_loop(argv: list[str], spawn: Callable[[list[str], dict[str, str]], tuple[int, bool]] = _run_child,
                 boot: Boot | None = None, pause: float = 1.0) -> int:
    """Run the bot as a child and start it again when it exits with ``RESTART_CODE``, or fails while an update is
    under way (so the update can go on, or be rolled back)."""
    boot = boot or Boot()
    args = [sys.executable, "-m", "app", *argv]
    env = {**os.environ, RESTARTER_ENV: "console"}
    while True:
        code, interrupted = spawn(args, env)
        if interrupted or code == 0:
            return code
        if code != RESTART_CODE:
            marker = boot.marker()
            if marker is None or marker.get("state") == "gave_up":
                return code
            boot.log(f"The bot exited with code {code} during an update: starting it again")
        time.sleep(pause)


# ---------------------------------------------------------------- entry points

_current: Boot | None = None


def start(argv: list[str]) -> int | None:
    """Called by ``python -m app`` (``run``) before anything else: None to run in this process, else the exit code."""
    global _current
    if wants_console_loop():
        return console_loop(argv)
    _current = Boot()
    return _current.prepare()


def ready() -> None:
    """The bot is on Discord with its guild. Only acts in a process that went through :func:`start`."""
    if _current is not None:
        _current.ready()
