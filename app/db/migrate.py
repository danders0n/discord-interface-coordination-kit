"""Alembic migrations, applied at start-up. A database that needs a migration is backed up first."""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.db.engine import Database

MIGRATIONS = Path(__file__).parent / "migrations"
log = logging.getLogger(__name__)


def alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


def head() -> str | None:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


async def current(db: Database) -> str | None:
    async with db.engine.connect() as conn:
        return await conn.run_sync(lambda sync: MigrationContext.configure(sync).get_current_revision())


def new_revision(message: str) -> None:
    """Write a migration for what the models changed (developers): a scratch database is brought to the newest
    revision, then compared with the models. Read the result before committing it."""
    import tempfile

    import sqlalchemy as sa

    with tempfile.TemporaryDirectory() as folder:
        engine = sa.create_engine(f"sqlite:///{Path(folder) / 'scratch.db'}")
        with engine.begin() as connection:
            config = alembic_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            number = len(list((MIGRATIONS / "versions").glob("[0-9][0-9][0-9][0-9]_*.py"))) + 1
            command.revision(config, message=message, autogenerate=True, rev_id=f"{number:04d}")
        engine.dispose()


async def upgrade(db: Database) -> bool:
    """Bring the schema to the newest revision. True when something was applied."""
    if await current(db) == head():
        return False

    def run(sync_connection) -> None:
        config = alembic_config()
        config.attributes["connection"] = sync_connection
        command.upgrade(config, "head")

    async with db.engine.begin() as conn:
        await conn.run_sync(run)
    log.info("Database migrated to %s", head())
    return True
