from datetime import UTC, datetime

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text

from app.db import Base, migrate
from app.db.models import BotState


async def test_every_connection_uses_wal_and_waits_for_locks(db):
    async with db.engine.connect() as conn:
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await conn.execute(text("PRAGMA busy_timeout"))).scalar() == 5000
        assert (await conn.execute(text("PRAGMA foreign_keys"))).scalar() == 1


async def test_migrations_match_the_models(db):
    assert await migrate.current(db) == migrate.head()
    async with db.engine.connect() as conn:
        diff = await conn.run_sync(lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata))
    assert diff == [], 'the models changed without a migration: python -m app new-migration "what changed"'


def test_migrations_keep_the_events_that_exist(tmp_path):
    """A column added later must not break the rows already there (0004 gave its NOT NULL columns defaults)."""
    import sqlalchemy as sa
    from alembic import command

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        config = migrate.alembic_config()
        config.attributes["connection"] = conn
        command.upgrade(config, "0003")
        conn.execute(sa.text("INSERT INTO event (kind, title, status, assembly_at, start_at, duration_minutes, "
                             "required_dlc, created_by, created_at) VALUES ('internal', 'Old', 'published', "
                             "'2026-11-01 18:00:00', '2026-11-01 18:30:00', 180, '[]', 1, '2026-10-01 00:00:00')"))
        command.upgrade(config, "head")
        row = conn.execute(sa.text("SELECT title, how_to_join, dont_end_automatically, messages "
                                   "FROM event")).one()
    engine.dispose()
    assert tuple(row) == ("Old", "", 0, "{}")


async def test_upgrade_twice_is_a_no_op(db):
    assert await migrate.upgrade(db) is False


async def test_times_come_back_as_utc_and_naive_ones_are_refused(db):
    when = datetime(2026, 10, 18, 19, 0, tzinfo=UTC)
    async with db.session() as session, session.begin():
        session.add(BotState(key="k", value={"a": 1}, updated_at=when))
    async with db.session() as session:
        row = await session.get(BotState, "k")
        assert row.updated_at == when and row.updated_at.tzinfo is UTC
    with pytest.raises(Exception, match="Naive datetime"):
        async with db.session() as session, session.begin():
            session.add(BotState(key="n", value={}, updated_at=datetime(2026, 1, 1)))
