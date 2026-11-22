"""Updating the bot from Discord (app/update/, docs/PLAN-remote-update.md, step 2): the check against a local bare
repository as ``origin``, the refusals, the hold and the wait, the marker and backup, go back, the start-up report,
``/update`` and its buttons. ``request_exit`` is a recorder: nothing exits."""

import asyncio
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import discord
import pytest
from conftest import ADMIN, MEMBER
from fakes import FakeInteraction, FakeMember, FakeRole, invoke

from app.scheduler import Action
from app.update.cog import UpdateButton, UpdateCog, check_embed
from app.update.service import UpdateError, Updater

ENGLISH = discord.Locale.british_english


def git(root: Path, *args: str) -> str:
    done = subprocess.run(["git", "-c", "user.name=Dev", "-c", "user.email=d@d", "-C", str(root), *args],
                          capture_output=True, text=True, check=True)
    return done.stdout.strip()


def commit(dev: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (dev / name).parent.mkdir(parents=True, exist_ok=True)
        (dev / name).write_text(text)
    git(dev, "add", ".")
    git(dev, "commit", "-q", "-m", message)
    git(dev, "push", "-q", "origin", "HEAD:main")
    return git(dev, "rev-parse", "HEAD")


class Repo:
    def __init__(self, tmp_path: Path) -> None:
        self.origin, self.dev, self.install = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "install"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.dev)], check=True, capture_output=True)
        git(self.dev, "checkout", "-q", "-b", "main")
        self.first = commit(self.dev, {"requirements.txt": "a==1\n", "app.txt": "one\n"}, "First")
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.origin), str(self.install)], check=True,
                       capture_output=True)

    def push(self, files: dict[str, str], message: str) -> str:
        return commit(self.dev, files, message)


@pytest.fixture
def repo(tmp_path) -> Repo:
    return Repo(tmp_path)


class Exits:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> bool:
        self.count += 1
        return True


@pytest.fixture
def updater(services, repo):
    services.store.current = services.settings.model_copy(update={"language": "en"})  # (staff-log notes in English)
    services.settings.data_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(services.settings.database_path)) as db:  # (the fixture's database is in WAL)
        db.execute("SELECT 1")
    exits = Exits()
    built = Updater(services.store, services.scheduler, services.staff, root=repo.install,
                    detect=lambda: "console", can_exit=lambda: True, request_exit=exits, check_env=lambda: None)
    built.exits = exits
    services.updater = built
    return built


def marker(repo: Repo) -> dict | None:
    path = repo.install / ".app-update" / "pending.json"
    return json.loads(path.read_text()) if path.exists() else None


async def finish(updater: Updater) -> None:
    await asyncio.wait_for(updater._task, 10)


# ---------------------------------------------------------------- check


def test_a_check_lists_what_an_update_brings(updater, repo):
    tip = repo.push({"requirements.txt": "a==2\n", "app/db/migrations/versions/0003_x.py": "x = 1\n"}, "New pins")
    (repo.install / "app.txt").write_text("changed here\n")  # a local change, lost by the update
    found = updater.check()
    assert (found["branch"], found["head"], found["tip"]) == ("main", repo.first, tip)  # the branch checked out
    assert [c["subject"] for c in found["commits"]] == ["New pins"]
    assert found["requirements"] and found["migrations"]
    assert found["dirty"] == ["app.txt"] and found["local_commits"] == []


def test_a_plain_change_brings_no_dependencies_or_migrations(updater, repo):
    repo.push({"app.txt": "two\n"}, "Two")
    found = updater.check()
    assert not found["requirements"] and not found["migrations"]


def test_a_configured_branch_is_followed_and_a_bad_one_refused(updater, repo, services):
    services.store.current = services.settings.model_copy(update={"update": services.settings.update.model_copy(
        update={"branch": "../etc"})})
    with pytest.raises(UpdateError) as caught:
        updater.check()
    assert caught.value.code == "bad_branch"


def test_the_token_never_appears_in_what_git_said(updater, services):
    from pydantic import SecretStr

    services.store.current = services.settings.model_copy(update={"update": services.settings.update.model_copy(
        update={"github_token": SecretStr("ghp_secret")})})
    assert "ghp_secret" not in updater._clean("fatal: could not read from https://ghp_secret@github.com/x")
    env = updater._env(auth=True)
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_secret" not in env["GIT_CONFIG_VALUE_0"]  # (base64 in the header, never on the command line)


# ---------------------------------------------------------------- refusals


async def test_refusals(updater, repo):
    async def code(**kwargs) -> str:
        with pytest.raises(UpdateError) as caught:
            await updater.start("update", "<@1>", **kwargs)
        return caught.value.code

    assert await code() == "up_to_date"
    tip = repo.push({"app.txt": "two\n"}, "Two")
    assert await code(expect="0" * 40) == "moved"
    updater._check_env = lambda: "BOT_LANGUAGE: Input should be 'pl' or 'en'"
    assert await code() == "bad_env"
    updater._check_env = lambda: None
    updater._detect = lambda: None
    assert await code() == "no_restarter"
    updater._detect = lambda: "nssm"
    updater.root = repo.install.parent / "nowhere"
    assert await code() == "not_checkout"
    assert marker(repo) is None and updater.exits.count == 0 and not updater.scheduler.held
    assert tip  # (the push happened)


async def test_only_one_start_at_a_time(updater, repo):
    repo.push({"app.txt": "two\n"}, "Two")
    results = await asyncio.gather(updater.start("update", "<@1>"), updater.start("update", "<@2>"),
                                   return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["UpdateError", "dict"]
    assert next(r for r in results if isinstance(r, UpdateError)).code == "busy"
    await finish(updater)


# ---------------------------------------------------------------- update


async def test_an_update_backs_up_writes_the_marker_and_exits(updater, repo, services):
    tip = repo.push({"requirements.txt": "a==2\n"}, "New pins")
    announced = []

    async def announce(plan):
        announced.append(plan["to"])

    updater.announce = announce
    waiting = await updater.start("update", "<@1>")
    assert (waiting["kind"], waiting["to"], waiting["action"]) == ("update", tip, None)
    await finish(updater)
    found = marker(repo)
    assert (found["state"], found["from"], found["to"], found["requirements"]) == ("pending", repo.first, tip, True)
    assert found["actor"] == "<@1>" and found["db_file"] == str(services.settings.database_path.resolve())
    assert Path(found["backup"]).is_file() and Path(found["backup"]).parent.name == "updates"
    assert updater.exits.count == 1 and announced == [tip]
    notes = services.staff.drain()
    assert any("restarts for the update to" in n and "dependencies change" in n for n in notes)
    assert services.scheduler.held  # (nothing starts any more: the bot is about to exit)


async def test_local_changes_are_kept_as_a_diff(updater, repo):
    repo.push({"app.txt": "two\n"}, "Two")
    (repo.install / "app.txt").write_text("mine\n")
    await updater.start("update", "<@1>")
    await finish(updater)
    diff = Path(marker(repo)["diff"])
    assert "+mine" in diff.read_text()


async def test_an_update_waits_for_the_running_action(updater, repo, services, monkeypatch):
    import app.update.service as service

    monkeypatch.setattr(service, "WAIT_POLL", 0.01)
    repo.push({"app.txt": "two\n"}, "Two")
    services.scheduler.running = Action(1, "archive_export", None, None, services.scheduler.clock(), 0)
    waiting = await updater.start("update", "<@1>")
    assert waiting["action"] == "archive_export" and services.scheduler.held
    await asyncio.sleep(0.05)
    assert not updater._task.done() and marker(repo) is None
    services.scheduler.running = None  # it finished
    await finish(updater)
    assert marker(repo)["state"] == "pending" and updater.exits.count == 1


async def test_restart_now_stops_waiting(updater, repo, services, monkeypatch):
    import app.update.service as service

    monkeypatch.setattr(service, "WAIT_POLL", 0.01)
    services.scheduler.running = Action(1, "archive_export", None, None, services.scheduler.clock(), 0)
    await updater.start("restart", "<@1>")
    await updater.hurry()
    await finish(updater)
    assert updater.exits.count == 1


async def test_a_cancel_releases_the_scheduler(updater, repo, services, monkeypatch):
    import app.update.service as service

    monkeypatch.setattr(service, "WAIT_POLL", 0.01)
    repo.push({"app.txt": "two\n"}, "Two")
    services.scheduler.running = Action(1, "archive_export", None, None, services.scheduler.clock(), 0)
    await updater.start("update", "<@1>")
    await updater.cancel("<@2>")
    assert not services.scheduler.held and updater.waiting is None
    assert marker(repo) is None and updater.exits.count == 0
    await asyncio.sleep(0)  # (post() queues on the loop's next turn)
    assert "The bot's update was cancelled by <@2>." in services.staff.drain()
    with pytest.raises(UpdateError) as caught:
        await updater.cancel("<@2>")
    assert caught.value.code == "nothing_waiting"


async def test_backups_are_trimmed(updater, repo, services):
    updates = services.settings.data_dir / "updates"
    updates.mkdir(parents=True)
    for stamp in ("20260101-000000", "20260102-000000", "20260103-000000", "20260104-000000"):
        (updates / f"{stamp}-aaaaaaa.db").write_text("x")
    repo.push({"app.txt": "two\n"}, "Two")
    await updater.start("update", "<@1>")
    await finish(updater)
    assert len(list(updates.glob("*.db"))) == 3  # keep_backups, the new one among them
    assert Path(marker(repo)["backup"]).is_file()


# ---------------------------------------------------------------- restart and the report


async def test_a_restart_writes_its_history_line_and_is_reported_once(updater, repo):
    await updater.start("restart", "<@1>")
    await finish(updater)
    assert marker(repo) is None and updater.exits.count == 1
    assert updater.report() == ["The bot restarted (asked by <@1>)."]
    assert updater.report() == []


def write_history(updater: Updater, **entry) -> None:
    updater.history_file.parent.mkdir(parents=True, exist_ok=True)
    with updater.history_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"reported": False} | entry) + "\n")


def test_the_report_words_each_outcome(updater, repo):
    tip = repo.push({"app.txt": "two\n"}, "Fix the header")
    git(repo.install, "fetch", "-q", "origin")
    write_history(updater, kind="update", outcome="ok", actor="<@1>", **{"from": repo.first, "to": tip})
    write_history(updater, kind="update", outcome="rolled_back", actor="<@1>", error="RuntimeError: boom",
                  db_rolled_back=True, **{"from": repo.first, "to": tip})
    write_history(updater, kind="update", outcome="gave_up", error="pip install: no", **{"from": repo.first, "to": tip})
    ok, rolled, gave_up = updater.report()
    assert ok.startswith(f"The bot was updated from {repo.first[:7]} to {tip[:7]}") and "\n• Fix the header" in ok
    assert "did not start (RuntimeError: boom): it is back on" in rolled and "put back" in rolled
    assert "going back to" in gave_up and "boot.log" in gave_up


# ---------------------------------------------------------------- go back


async def test_go_back_needs_a_finished_update(updater):
    with pytest.raises(UpdateError) as caught:
        await updater.start("back", "<@1>")
    assert caught.value.code == "nothing_back"


async def prepare_back(updater, repo, services, migrations: bool) -> str:
    files = {"app/db/migrations/versions/0003_x.py": "x = 1\n"} if migrations else {"app.txt": "two\n"}
    tip = repo.push(files, "Two")
    git(repo.install, "pull", "-q", "--ff-only")
    backup = services.settings.data_dir / "updates" / "20261120-100000-aaaaaaa.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text("old database")
    write_history(updater, kind="update", outcome="ok", actor="<@1>", backup=str(backup), reported=True,
                  **{"from": repo.first, "to": tip})
    return tip


async def test_go_back_over_new_migrations_must_restore(updater, repo, services):
    tip = await prepare_back(updater, repo, services, migrations=True)
    with pytest.raises(UpdateError) as caught:
        await updater.start("back", "<@1>")
    assert caught.value.code == "needs_restore"

    services.store.current = services.settings.model_copy(update={"update": services.settings.update.model_copy(
        update={"keep_backups": 1})})
    await updater.start("back", "<@1>", restore_db=True)
    await finish(updater)
    found = marker(repo)
    assert (found["kind"], found["from"], found["to"], found["migrations"]) == ("back", tip, repo.first, True)
    assert Path(found["restore_db"]).is_file()  # kept by the trim although older than the newest one
    assert Path(found["backup"]).is_file() and found["backup"] != found["restore_db"]


async def test_go_back_without_migrations_needs_no_restore(updater, repo, services):
    await prepare_back(updater, repo, services, migrations=False)
    await updater.start("back", "<@1>")
    await finish(updater)
    assert marker(repo)["restore_db"] is None


# ---------------------------------------------------------------- the scheduler's hold and the staff log


async def test_a_held_scheduler_starts_nothing(services, clock):
    ran = []

    async def handler(action, now):
        ran.append(action.kind)

    services.scheduler.register("x", handler)
    await services.scheduler.schedule("x", clock())
    services.scheduler.hold()
    assert await services.scheduler.run_due() == 0
    services.scheduler.release()
    assert await services.scheduler.run_due() == 1 and ran == ["x"]
    assert services.scheduler.running is None


async def test_the_staff_log_is_flushed_before_the_exit():
    from app.staff_log import StaffLog

    staff, sent = StaffLog(), []
    staff.bind(asyncio.get_running_loop())

    async def send(text):
        await asyncio.sleep(0.01)
        sent.append(text)

    task = asyncio.create_task(staff.run(send))
    staff.post("Restarting")
    assert await staff.flush(2)
    assert sent == ["Restarting"]
    task.cancel()


# ---------------------------------------------------------------- /update


def admin(services) -> FakeInteraction:
    return FakeInteraction(services, FakeMember(1, roles=[FakeRole(ADMIN)]), ENGLISH)


async def test_update_is_for_bot_admins_only(services, updater):
    cog = UpdateCog(services)
    interaction = FakeInteraction(services, FakeMember(2, roles=[FakeRole(MEMBER)]), ENGLISH)
    await invoke(cog.now, cog, interaction)
    assert interaction.replies[0].content == "You don't have the permission for this."
    assert updater.exits.count == 0


async def test_update_now_answers_a_refusal_in_words(services, updater):
    cog = UpdateCog(services)
    interaction = admin(services)
    await invoke(cog.now, cog, interaction)
    assert interaction.replies[-1].content == "The bot is up to date."


async def test_update_check_offers_update_now_and_the_button_updates(services, updater, repo):
    tip = repo.push({"app.txt": "two\n"}, "Two")
    cog = UpdateCog(services)
    interaction = admin(services)
    await invoke(cog.check, cog, interaction)
    sent = interaction.replies[-1].kwargs
    assert "Two" in sent["embed"].description
    (button,) = sent["view"].children
    assert button.custom_id == f"update:go:{tip}"

    pressed = admin(services)
    item = await UpdateButton.from_custom_id(pressed, button.item, UpdateButton.__discord_ui_compiled_template__.match(
        button.custom_id))  # (Discord hands over the plain button)
    await item.callback(pressed)
    await finish(updater)
    assert marker(repo)["to"] == tip
    assert "restarts now" in pressed.replies[-1].content


async def test_a_member_pressing_update_now_is_refused(services, updater, repo):
    tip = repo.push({"app.txt": "two\n"}, "Two")
    pressed = FakeInteraction(services, FakeMember(2, roles=[FakeRole(MEMBER)]), ENGLISH)
    await UpdateButton("go", tip).callback(pressed)
    assert pressed.replies[0].content == "You don't have the permission for this."
    assert updater.waiting is None


async def test_a_waiting_start_offers_restart_now_and_cancel(services, updater, repo):
    repo.push({"app.txt": "two\n"}, "Two")
    services.scheduler.running = Action(1, "archive_export", None, None, services.scheduler.clock(), 0)
    cog = UpdateCog(services)
    interaction = admin(services)
    await invoke(cog.now, cog, interaction)
    sent = interaction.replies[-1]
    assert "waiting for the scheduled action archive_export" in sent.content
    assert [b.custom_id for b in sent.kwargs["view"].children] == ["update:hurry", "update:cancel"]
    pressed = admin(services)
    await UpdateButton("cancel").callback(pressed)
    assert pressed.replies[0].content == "Cancelled." and updater.waiting is None


async def test_update_status_says_who_restarts_the_bot(services, updater):
    cog = UpdateCog(services)
    interaction = admin(services)
    await invoke(cog.status, cog, interaction)
    text = interaction.replies[-1].content
    assert "on main" in text and "Started again by: console." in text


def test_the_check_embed_in_polish():
    check = {"branch": "main", "head": "a" * 40, "tip": "b" * 40, "requirements": True, "migrations": True,
             "dirty": ["x", "y"], "local_commits": [],
             "commits": [{"sha": "b" * 40, "subject": "Nowe", "author": "Dev", "date": "2026-11-22T10:00:00+01:00"}]}
    embed = check_embed(check, discord.Locale.polish)
    assert embed.title == "Aktualizacje na main"
    assert "2 pliki zmienione lokalnie" in embed.fields[0].value
