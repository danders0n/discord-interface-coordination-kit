"""``python -m app [run|migrate|backup|check-config|new-migration "message"]``, after the boot step (app/__main__.py).

``run`` (the default) validates `.env`, migrates the database (backing it up first when a migration is due), and
starts the bot. The other commands do one step and exit, for ``update.ps1``, for checking a new `.env`, and for
developers writing a migration. A restart asked for from inside (``/update``, app/restarter.py) closes the bot and
exits with ``RESTART_CODE``, for NSSM or the console loop to start it again."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime

from pydantic import ValidationError

from app import logs, restarter, settings as settings_module
from app.db import Database, backup, migrate
from app.scheduler import Scheduler
from app.settings import Settings, SettingsStore
from app.staff_log import StaffLog
from app.supervisor_client import SupervisorClient

log = logging.getLogger("app")
COMMANDS = ("run", "migrate", "backup", "check-config", "new-migration")


def load_or_exit(env_file: str) -> Settings:
    unknown = settings_module.unknown_variables(env_file)
    if unknown:
        print(f"Unknown settings (typos, or renamed in a newer version; see .env.example): {', '.join(unknown)}",
              file=sys.stderr)
    try:
        return settings_module.load(env_file)
    except ValidationError as exc:
        print(f"{env_file} has errors:", file=sys.stderr)
        for line in settings_module.describe_errors(exc):
            print(f"  {line}", file=sys.stderr)
        raise SystemExit(2) from None


async def prepare_database(settings: Settings) -> Database:
    existed = settings.database_path.is_file()  # (connecting creates the file)
    db = Database(settings.database_path)
    if existed and await migrate.current(db) != migrate.head():
        await backup.backup(settings.database_path, settings.data_dir / "backups", settings.backup.keep,
                            datetime.now(UTC))
    await migrate.upgrade(db)
    return db


async def run(settings: Settings, env_file: str) -> None:
    from app.bot import EventBot  # (discord.py only for the command that needs it)
    from app.services import Services
    from app.staff_threads import StaffThreads

    staff = StaffLog()
    loop = asyncio.get_running_loop()
    staff.bind(loop)
    store = SettingsStore(settings, env_file)
    logs.setup(settings.data_dir, settings.logs.keep_days, staff, lambda: store.current.language)
    db = await prepare_database(settings)
    scheduler = Scheduler(db)
    await backup.register(scheduler, store)
    token = settings.supervisor.token.get_secret_value() if settings.supervisor.token else None
    client = SupervisorClient(settings.supervisor.url, token)
    services = Services(store, db, scheduler, staff, client, StaffThreads(db, store))
    async with EventBot(services) as bot:
        closing: set[asyncio.Task[None]] = set()

        def close() -> None:  # (from the loop or a thread: the client is closed on the loop)
            def go() -> None:
                task = loop.create_task(bot.close())
                closing.add(task)
                task.add_done_callback(closing.discard)
            loop.call_soon_threadsafe(go)

        restarter.set_exit_handler(close)
        try:
            await bot.start(settings.discord_token.get_secret_value())
        finally:
            if not restarter.exit_requested():  # (clearing it would forget the request main() exits on)
                restarter.set_exit_handler(None)


async def migrate_only(settings: Settings) -> None:
    db = await prepare_database(settings)
    await db.close()
    print(f"Database at revision {migrate.head()}")


async def backup_only(settings: Settings) -> None:
    target = await backup.backup(settings.database_path, settings.data_dir / "backups", settings.backup.keep,
                                 datetime.now(UTC))
    print(f"Backed up to {target}" if target else "No database yet: nothing to back up")


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(prog="python -m app")
    out.add_argument("command", nargs="?", default="run", choices=COMMANDS)
    out.add_argument("message", nargs="?", help="new-migration: what it changes")
    out.add_argument("--env-file", default=settings_module.ENV_FILE)
    return out


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    if args.command == "new-migration":  # (a developer's tool: needs no .env)
        if not args.message:
            parser().error("new-migration needs a message")
        migrate.new_revision(args.message)
        return
    settings = load_or_exit(args.env_file)
    if args.command == "check-config":
        print(f"{args.env_file} is valid")
        return
    if args.command != "run":
        logs.setup(settings.data_dir, settings.logs.keep_days)
    job = {"run": lambda: run(settings, args.env_file), "migrate": lambda: migrate_only(settings),
           "backup": lambda: backup_only(settings)}[args.command]
    try:
        asyncio.run(job())
    except KeyboardInterrupt:
        pass
    if restarter.exit_requested():
        sys.exit(restarter.RESTART_CODE)  # NSSM (or boot.console_loop) starts it again
