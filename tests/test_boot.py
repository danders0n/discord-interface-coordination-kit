"""The boot step of a bot update (app/boot.py, docs/PLAN-remote-update.md): the marker's states over a real git
checkout (pip replaced by a recorder), the watchdog, the database restore, the console loop, restarter.detect, and the
entry point's way to it."""

import json
import logging
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import pytest

from app import __main__ as entry, boot, restarter
from app.boot import RESTART_CODE, Boot


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(root), *args],
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A checkout at its first commit; the second changes requirements.txt and app.txt."""
    root = tmp_path / "install"
    root.mkdir()
    git(root, "init", "-q")
    (root / "requirements.txt").write_text("a==1\n")
    (root / "app.txt").write_text("one\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "one")
    first = git(root, "rev-parse", "HEAD")
    (root / "requirements.txt").write_text("a==2\n")
    (root / "app.txt").write_text("two\n")
    git(root, "commit", "-q", "-am", "two")
    second = git(root, "rev-parse", "HEAD")
    git(root, "reset", "-q", "--hard", first)
    return root, first, second


class Recorder:
    """The runner: git for real, pip recorded (failing while ``pip_fails`` is set)."""

    def __init__(self) -> None:
        self.pip: list[str] = []
        self.pip_fails = 0
        self.exits: list[int] = []

    def run(self, args: list[str], timeout: float) -> tuple[int, str]:
        if args[1:3] == ["-m", "pip"]:
            self.pip.append(args[-1])
            if self.pip_fails:
                self.pip_fails -= 1
                return 1, "ERROR: No matching distribution found for a==2"
            return 0, "Successfully installed a"
        return boot.run_command(args, timeout)

    def boot(self, root: Path, **kwargs) -> Boot:
        return Boot(root, python="python", run=self.run, exit=self.exits.append, **kwargs)


def write_marker(root: Path, tmp_path: Path, first: str, second: str, **fields) -> Path:
    data_dir = tmp_path / "data"
    marker = {"id": "20261122-201500", "kind": "update", "from": first, "to": second, "branch": "master",
              "actor": "admin (1)", "requested_at": "2026-11-22T20:15:00+00:00", "state": "pending", "attempts": 0,
              "requirements": True, "migrations": False, "restore_db": None, "data_dir": str(data_dir),
              "db_file": None, "backup": None, "error": None} | fields
    (root / ".app-update").mkdir(exist_ok=True)
    (root / ".app-update" / "pending.json").write_text(json.dumps(marker))
    return data_dir


def history(data_dir: Path) -> list[dict]:
    path = data_dir / "updates" / "history.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture(autouse=True)
def no_current():
    yield
    boot._current = None
    restarter.set_exit_handler(None)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if isinstance(h, boot._ErrorKeeper)]:  # starts that "crashed"
        root.removeHandler(handler)


# ---------------------------------------------------------------- the states


def test_no_marker_starts_the_bot(repo):
    root, first, _ = repo
    assert Recorder().boot(root).prepare() is None
    assert git(root, "rev-parse", "HEAD") == first
    assert not (root / ".app-update").exists()


def test_an_update_is_applied_then_started_and_closed_when_ready(repo, tmp_path):
    root, first, second = repo
    data_dir = write_marker(root, tmp_path, first, second)
    rec = Recorder()

    assert rec.boot(root).prepare() == RESTART_CODE  # files changed: start again with a fresh interpreter
    assert git(root, "rev-parse", "HEAD") == second
    assert (root / "app.txt").read_text() == "two\n"
    assert rec.pip == [str(root / "requirements.txt")]
    marker = rec.boot(root).marker()
    assert (marker["state"], marker["attempts"]) == ("applied", 0)

    started = rec.boot(root, ready_timeout=30)
    assert started.prepare() is None
    assert started.marker()["attempts"] == 1
    started.ready()
    assert not (root / ".app-update" / "pending.json").exists()
    [entry_] = history(data_dir)
    assert (entry_["outcome"], entry_["from"], entry_["to"], entry_["reported"]) == ("ok", first, second, False)
    assert entry_["migrations"] is False
    assert "attempt 1 of 2" in (root / ".app-update" / "boot.log").read_text()


def test_unchanged_requirements_skip_pip(repo, tmp_path):
    root, first, second = repo
    write_marker(root, tmp_path, first, second, requirements=False)
    rec = Recorder()
    assert rec.boot(root).prepare() == RESTART_CODE
    assert rec.pip == []


def test_two_failed_starts_go_back_and_two_more_give_up(repo, tmp_path):
    root, first, second = repo
    data_dir = write_marker(root, tmp_path, first, second, state="applied")
    git(root, "reset", "-q", "--hard", second)
    rec = Recorder()
    for _ in range(2):  # each start crashes before ready()
        assert rec.boot(root, ready_timeout=30).prepare() is None
    assert rec.boot(root).prepare() == RESTART_CODE  # the third: back to the old commit
    assert git(root, "rev-parse", "HEAD") == first
    assert rec.pip == [str(root / "requirements.txt")]  # the old pins again
    assert rec.boot(root).marker()["state"] == "rolled_back_applied"

    for _ in range(2):
        assert rec.boot(root, ready_timeout=30).prepare() is None
    assert rec.boot(root).prepare() is None  # gives up: start as it is
    assert rec.boot(root).marker()["state"] == "gave_up"
    assert rec.boot(root).prepare() is None  # and stays given up
    assert [e["outcome"] for e in history(data_dir)] == ["gave_up"]


def test_a_rolled_back_start_that_comes_up_is_recorded_with_the_reason(repo, tmp_path):
    root, first, second = repo
    data_dir = write_marker(root, tmp_path, first, second, state="applied", attempts=2,
                            error="on_ready failed: RuntimeError: boom")
    git(root, "reset", "-q", "--hard", second)
    rec = Recorder()
    assert rec.boot(root).prepare() == RESTART_CODE
    started = rec.boot(root, ready_timeout=30)
    assert started.prepare() is None
    started.ready()
    [entry_] = history(data_dir)
    assert entry_["outcome"] == "rolled_back"
    assert "boom" in entry_["error"]
    assert not (root / ".app-update" / "pending.json").exists()


def test_a_failed_pip_goes_back_at_once(repo, tmp_path):
    root, first, second = repo
    write_marker(root, tmp_path, first, second)
    rec = Recorder()
    rec.pip_fails = 1
    assert rec.boot(root).prepare() == RESTART_CODE
    assert git(root, "rev-parse", "HEAD") == first
    assert len(rec.pip) == 2  # the new pins failed, the old ones went back in
    marker = rec.boot(root).marker()
    assert marker["state"] == "rolled_back_applied"
    assert "No matching distribution" in marker["error"]


def test_a_failed_way_back_gives_up(repo, tmp_path):
    root, first, second = repo
    data_dir = write_marker(root, tmp_path, first, second)
    rec = Recorder()
    rec.pip_fails = 2
    assert rec.boot(root).prepare() is None
    assert rec.boot(root).marker()["state"] == "gave_up"
    assert [e["outcome"] for e in history(data_dir)] == ["gave_up"]


def test_an_unknown_commit_goes_back(repo, tmp_path):
    root, first, _ = repo
    write_marker(root, tmp_path, first, "0" * 40, requirements=False)
    rec = Recorder()
    assert rec.boot(root).prepare() == RESTART_CODE
    marker = rec.boot(root).marker()
    assert marker["state"] == "rolled_back_applied"
    assert "git reset" in marker["error"]
    assert git(root, "rev-parse", "HEAD") == first


def test_an_unknown_state_is_ignored(repo, tmp_path):
    root, first, second = repo
    write_marker(root, tmp_path, first, second, state="sideways")
    assert Recorder().boot(root).prepare() is None
    assert "Unknown state" in (root / ".app-update" / "boot.log").read_text()


# ---------------------------------------------------------------- watching a start


def test_the_watchdog_ends_a_start_that_never_gets_ready(repo, tmp_path):
    root, first, second = repo
    write_marker(root, tmp_path, first, second, state="applied")
    rec = Recorder()
    watched = rec.boot(root, ready_timeout=0.05)
    assert watched.prepare() is None
    for _ in range(100):
        if rec.exits:
            break
        watched._ready.wait(0.02)
    assert rec.exits == [1]
    assert "not ready after" in watched.marker()["error"]
    watched.ready()  # detach the error handler


def test_errors_logged_during_a_start_are_kept_as_the_reason(repo, tmp_path):
    root, first, second = repo
    write_marker(root, tmp_path, first, second, state="applied")
    watched = Recorder().boot(root, ready_timeout=30)
    watched.prepare()
    try:
        raise RuntimeError("no such table")
    except RuntimeError:
        logging.getLogger("discord.client").exception("Ignoring exception in on_ready")
    logging.getLogger("app").warning("only a warning")
    assert watched.marker()["error"] == "Ignoring exception in on_ready: RuntimeError: no such table"

    logging.getLogger("app").error('Traceback (most recent call last):\n  File "app/bot.py", line 3\n'
                                   "RuntimeError: broken on purpose\n")
    assert watched.marker()["error"] == "RuntimeError: broken on purpose"
    watched.ready()
    logging.getLogger("x").error("after ready")
    assert not (root / ".app-update" / "pending.json").exists()


def test_ready_without_a_boot_does_nothing(repo, tmp_path):
    root, first, second = repo
    write_marker(root, tmp_path, first, second, state="applied")
    boot.ready()  # this process did not go through boot.start (the tests, a plain on_ready)
    assert Recorder().boot(root).marker()["state"] == "applied"


# ---------------------------------------------------------------- database restore


def database(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE t (v TEXT)")
        db.execute("INSERT INTO t VALUES (?)", (value,))
        db.commit()
    return path


def value(path: Path) -> str:
    with closing(sqlite3.connect(path)) as db:
        return db.execute("SELECT v FROM t").fetchone()[0]


@pytest.fixture
def go_back(repo, tmp_path):
    """A go back from ``second`` to ``first`` with the database restored from the update's backup."""
    root, first, second = repo
    git(root, "reset", "-q", "--hard", second)
    data = tmp_path / "data"
    live = database(data / "bot.db", "newer")
    Path(f"{live}-wal").write_text("stale journal")
    older = database(data / "updates" / "20261120-100000-aaaaaaa.db", "older")
    replaced = database(data / "updates" / "20261122-201500-bbbbbbb.db", "newer")
    write_marker(root, tmp_path, second, first, kind="back", requirements=False, migrations=True,
                 restore_db=str(older), db_file=str(live), backup=str(replaced))
    return root, first, second, live


def test_a_go_back_restores_the_database(go_back):
    root, first, _, live = go_back
    rec = Recorder()
    assert rec.boot(root).prepare() == RESTART_CODE
    assert git(root, "rev-parse", "HEAD") == first
    assert value(live) == "older"
    assert not Path(f"{live}-wal").exists()  # the replaced database's journal is gone
    marker = rec.boot(root).marker()
    assert marker["db_restored"] is True and marker["db_error"] is None


def test_a_rolled_back_go_back_puts_the_replaced_database_back(go_back):
    root, _, second, live = go_back
    rec = Recorder()
    rec.boot(root).prepare()
    for _ in range(2):
        rec.boot(root, ready_timeout=30).prepare()  # the older code refuses the database: crashes
    assert rec.boot(root).prepare() == RESTART_CODE
    assert git(root, "rev-parse", "HEAD") == second
    assert value(live) == "newer"


def test_the_history_line_says_the_database_was_put_back(go_back, tmp_path):
    root, _, _, live = go_back
    rec = Recorder()
    rec.boot(root).prepare()
    for _ in range(2):
        rec.boot(root, ready_timeout=30).prepare()
    rec.boot(root).prepare()
    started = rec.boot(root, ready_timeout=30)
    started.prepare()
    started.ready()
    [line] = history(tmp_path / "data")
    assert (line["outcome"], line["db_restored"], line["db_rolled_back"]) == ("rolled_back", True, True)


def test_a_backup_that_is_not_a_database_is_not_restored(go_back):
    root, first, _, live = go_back
    marker = json.loads((root / ".app-update" / "pending.json").read_text())
    Path(marker["restore_db"]).write_bytes(b"not a database" * 100)
    rec = Recorder()
    assert rec.boot(root).prepare() == RESTART_CODE  # the code goes back all the same
    assert git(root, "rev-parse", "HEAD") == first
    assert value(live) == "newer"
    assert rec.boot(root).marker()["db_error"].startswith("not restored")
    assert not list(live.parent.glob("*.restore.tmp"))


def test_without_the_backup_of_what_it_replaces_nothing_is_restored(go_back):
    root, _, _, live = go_back
    marker = json.loads((root / ".app-update" / "pending.json").read_text())
    Path(marker["backup"]).unlink()
    rec = Recorder()
    rec.boot(root).prepare()
    assert value(live) == "newer"
    assert "no backup of the database it would replace" in rec.boot(root).marker()["db_error"]


# ---------------------------------------------------------------- the console loop


def test_console_loop_starts_again_on_the_restart_code(repo):
    root, _, _ = repo
    codes = [(RESTART_CODE, False), (RESTART_CODE, False), (0, False)]
    seen = []

    def spawn(args, env):
        seen.append((args[-2:], env[boot.RESTARTER_ENV]))
        return codes.pop(0)

    assert boot.console_loop(["run"], spawn=spawn, boot=Recorder().boot(root), pause=0) == 0
    assert seen == [(["app", "run"], "console")] * 3


def test_console_loop_starts_a_failed_child_again_only_during_an_update(repo, tmp_path):
    root, first, second = repo
    codes = [(1, False), (1, False)]
    b = Recorder().boot(root)
    assert boot.console_loop([], spawn=lambda a, e: codes.pop(0), boot=b, pause=0) == 1
    assert codes == [(1, False)]  # no update under way: the failure ends the loop

    write_marker(root, tmp_path, first, second, state="applied")
    codes = [(1, False), (2, False), (RESTART_CODE, True)]  # 2: an invalid .env in the new version
    assert boot.console_loop([], spawn=lambda a, e: codes.pop(0), boot=b, pause=0) == RESTART_CODE
    assert codes == []  # Ctrl+C ended it

    write_marker(root, tmp_path, first, second, state="gave_up")
    codes = [(1, False), (0, False)]
    assert boot.console_loop([], spawn=lambda a, e: codes.pop(0), boot=b, pause=0) == 1


class Tty:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_the_console_loop_is_only_for_a_terminal():
    assert boot.wants_console_loop({}, Tty(True))
    assert not boot.wants_console_loop({}, Tty(False))  # a service: no console
    assert not boot.wants_console_loop({boot.RESTARTER_ENV: "nssm"}, Tty(True))
    assert not boot.wants_console_loop({boot.RESTARTER_ENV: "console"}, Tty(True))  # the loop's own child
    assert not boot.wants_console_loop({"INVOCATION_ID": "x"}, Tty(True))
    assert not boot.wants_console_loop({}, object())


# ---------------------------------------------------------------- restarter and the entry point


def test_detect_who_starts_the_bot_again():
    assert restarter.detect({boot.RESTARTER_ENV: "console"}) == "console"
    assert restarter.detect({boot.RESTARTER_ENV: "NSSM "}) == "nssm"
    assert restarter.detect({"INVOCATION_ID": "abc"}) == "systemd"
    assert restarter.detect({boot.RESTARTER_ENV: "cron"}) is None
    assert restarter.detect({}) is None


def test_request_exit_needs_the_handler():
    assert not restarter.request_exit()
    assert not restarter.exit_requested()
    ended = []
    restarter.set_exit_handler(lambda: ended.append(True))
    assert restarter.can_exit()
    assert restarter.request_exit()
    assert ended == [True] and restarter.exit_requested()


def test_only_running_the_bot_goes_through_the_boot_step():
    assert entry.command([]) == "run"
    assert entry.command(["--env-file", "x.env"]) == "run"
    assert entry.command(["--env-file", "check-config"]) == "run"  # (the value, not the command)
    assert entry.command(["--env-file", "x.env", "check-config"]) == "check-config"
    assert entry.command(["migrate"]) == "migrate"


def test_main_boots_first_and_exits_with_its_code(monkeypatch):
    from app import cli

    monkeypatch.setattr(boot, "start", lambda argv: RESTART_CODE)
    monkeypatch.setattr(cli, "main", lambda argv: pytest.fail("the bot ran before the boot step finished"))
    with pytest.raises(SystemExit) as caught:
        entry.main([])
    assert caught.value.code == RESTART_CODE

    ran = []
    monkeypatch.setattr(boot, "start", lambda argv: pytest.fail("a one-off command booted"))
    monkeypatch.setattr(cli, "main", ran.append)
    entry.main(["migrate"])
    assert ran == [["migrate"]]


def test_a_requested_restart_exits_with_the_restart_code(tmp_path, monkeypatch):
    from app import cli

    monkeypatch.setattr(cli, "load_or_exit", lambda env_file: object())

    async def job(settings, env_file):
        restarter.set_exit_handler(lambda: None)
        restarter.request_exit()

    monkeypatch.setattr(cli, "run", job)
    with pytest.raises(SystemExit) as caught:
        cli.main(["run"])
    assert caught.value.code == RESTART_CODE
