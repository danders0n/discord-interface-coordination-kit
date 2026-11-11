"""The daily online backup (docs/PLAN.md, *Configuration and deployment*): the sqlite3 backup API copies the
database while the bot keeps using it. Only the newest ``keep`` files stay. Folders next to the database
(images, archived attachments) and ``.env`` belong in the host's own backups."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

from app.scheduler import Action, Scheduler
from app.settings import SettingsStore

PREFIX = "bot-"
log = logging.getLogger(__name__)


def backup_file(database: Path, folder: Path, keep: int, now: datetime) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{PREFIX}{now:%Y%m%d-%H%M%S}.db"
    source = sqlite3.connect(database)
    try:
        copy = sqlite3.connect(target)
        try:
            source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    for old in sorted(folder.glob(f"{PREFIX}*.db"))[:-keep]:
        old.unlink()
    return target


async def backup(database: Path, folder: Path, keep: int, now: datetime) -> Path | None:
    """A copy of the database, or None when there is no database yet."""
    if not database.is_file():
        return None
    target = await asyncio.to_thread(backup_file, database, folder, keep, now)
    log.info("Database backed up to %s", target)
    return target


KIND = "backup"
KEY = "backup:daily"
HOUR = 4  # local time: after the evening's events, before anyone is about


def next_run(now: datetime, tz: tzinfo) -> datetime:
    local = now.astimezone(tz)
    run = local.replace(hour=HOUR, minute=0, second=0, microsecond=0)
    if run <= local:
        run = (local + timedelta(days=1)).replace(hour=HOUR, minute=0, second=0, microsecond=0)
    return run.astimezone(UTC)


async def register(scheduler: Scheduler, store: SettingsStore) -> None:
    """The daily backup as a scheduled action; a backup missed while the bot was down runs at start-up."""

    async def run(action: Action, now: datetime) -> datetime:
        settings = store.current
        await backup(settings.database_path, settings.data_dir / "backups", settings.backup.keep, now)
        pruned = await scheduler.prune()  # (after the copy: the backup still holds them)
        if pruned:
            log.info("Deleted %d scheduled actions done over 30 days ago", pruned)
        return next_run(now, settings.tz)

    scheduler.register(KIND, run)
    if not await scheduler.exists(KEY):
        await scheduler.schedule(KIND, next_run(scheduler.clock(), store.current.tz), key=KEY)
