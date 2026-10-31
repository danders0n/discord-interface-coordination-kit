from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.db import Database, migrate
from app.scheduler import Scheduler
from app.settings import Settings, SettingsStore
from app.staff_log import StaffLog
from app.supervisor_client import SupervisorClient
from app.services import Services

# Ids used across the tests.
GUILD, DRAFTS, EVENTS, HEADER, ANNOUNCE, MODERATION, STAFF = 100, 201, 202, 203, 204, 205, 206
MEMBER, MISSION_MAKER, KADET, ANNOUNCEMENT, ADMIN, MODERATOR = 301, 302, 303, 304, 305, 306


def make_settings(data_dir: Path, **overrides) -> Settings:
    values = {
        "discord_token": "token", "guild_id": GUILD, "data_dir": data_dir,
        "group": {"acronym": "IBC", "name": "Test group"},
        "channels": {"drafts_category": DRAFTS, "events_category": EVENTS, "header": HEADER,
                     "announcements": ANNOUNCE, "moderation": MODERATION, "staff_log": STAFF},
        "roles": {"member": MEMBER, "mission_maker": MISSION_MAKER, "announcement": ANNOUNCEMENT,
                  "signup": [MEMBER, KADET]},
        "permissions": {"bot_admin": [ADMIN], "approve_events": [MODERATOR], "manage_any_event": [ADMIN]},
    } | overrides
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path / "data")


@pytest.fixture
async def db(settings: Settings):
    database = Database(settings.database_path)
    await migrate.upgrade(database)
    yield database
    await database.close()


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 11, 2, 12, 0, tzinfo=UTC))


@pytest.fixture
async def services(settings: Settings, db: Database, clock: Clock):
    staff = StaffLog()
    staff.bind(asyncio.get_running_loop())
    client = SupervisorClient("http://127.0.0.1:1", None)
    yield Services(SettingsStore(settings), db, Scheduler(db, clock), staff, client)
    await client.close()
