import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import backup
from app.settings import SettingsStore

WARSAW = ZoneInfo("Europe/Warsaw")


async def test_backup_copies_the_live_database_and_keeps_the_newest(db, settings):
    folder = settings.data_dir / "backups"
    start = datetime(2026, 11, 1, 3, 0, tzinfo=UTC)
    for day in range(5):
        await backup.backup(settings.database_path, folder, 3, start + timedelta(days=day))
    kept = sorted(p.name for p in folder.glob("bot-*.db"))
    assert kept == ["bot-20261103-030000.db", "bot-20261104-030000.db", "bot-20261105-030000.db"]
    copy = sqlite3.connect(folder / kept[-1])
    assert copy.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0001"
    copy.close()


async def test_no_database_no_backup(tmp_path):
    assert await backup.backup(tmp_path / "none.db", tmp_path / "b", 3, datetime.now(UTC)) is None


def test_next_run_is_four_in_the_morning_local_time():
    before = datetime(2026, 11, 2, 1, 0, tzinfo=UTC)  # 02:00 in Warsaw
    after = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    assert backup.next_run(before, WARSAW) == datetime(2026, 11, 2, 3, 0, tzinfo=UTC)
    assert backup.next_run(after, WARSAW) == datetime(2026, 11, 3, 3, 0, tzinfo=UTC)
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert backup.next_run(summer, WARSAW) == datetime(2026, 7, 2, 2, 0, tzinfo=UTC)


async def test_registered_once_and_repeats_daily(services, clock):
    store: SettingsStore = services.store
    await backup.register(services.scheduler, store)
    await backup.register(services.scheduler, store)  # a restart doesn't move or add it
    clock.now = datetime(2026, 11, 3, 3, 0, tzinfo=UTC)
    assert await services.scheduler.run_due() == 1
    assert list((services.settings.data_dir / "backups").glob("bot-*.db"))
    assert await services.scheduler.run_due() == 0
    clock.now = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)
    assert await services.scheduler.run_due() == 1
