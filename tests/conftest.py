from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import discord
import pytest
from fakes import FakeChannel, FakeGuild, FakeRole

from app.db import Database, migrate
from app.reservations.service import ReservationService
from app.resolve import resolve
from app.scheduler import Scheduler
from app.services import Services
from app.settings import Settings, SettingsStore
from app.staff_log import StaffLog
from app.staff_threads import StaffThreads
from app.supervisor_client import SupervisorClient

# Ids used across the tests.
GUILD, DRAFTS, EVENTS, HEADER, ANNOUNCE, STAFF = 100, 201, 202, 203, 204, 206
MEMBER, MISSION_MAKER, KADET, ANNOUNCEMENT, ADMIN, MODERATOR = 301, 302, 303, 304, 305, 306


def make_settings(data_dir: Path, **overrides) -> Settings:
    values = {
        "discord_token": "token", "guild_id": GUILD, "data_dir": data_dir,
        "group": {"acronym": "IBC", "name": "Test group"},
        "channels": {"drafts_category": DRAFTS, "events_category": EVENTS, "header": HEADER,
                     "announcements": ANNOUNCE, "staff": STAFF},
        "roles": {"member": [MEMBER], "mission_maker": MISSION_MAKER, "announcement": ANNOUNCEMENT,
                  "signup": [MEMBER, KADET]},
        "permissions": {"bot_admin": [ADMIN], "approve_events": [MODERATOR], "manage_any_event": [ADMIN],
                        "server_control": [ADMIN, MISSION_MAKER], "server_message": [MODERATOR],
                        "server_override": [ADMIN]},
        "supervisor": {"event_server": "event"},
    } | overrides
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def make_guild() -> FakeGuild:
    """The server as `make_settings` describes it."""
    category = discord.ChannelType.category
    channels = [FakeChannel(DRAFTS, "Szkice", category), FakeChannel(EVENTS, "Eventy", category),
                FakeChannel(HEADER, "eventy"), FakeChannel(ANNOUNCE, "ogloszenia"), FakeChannel(STAFF, "staff")]
    names = {MEMBER: "Klanowicz", MISSION_MAKER: "Mission Maker", KADET: "Kadet", ANNOUNCEMENT: "Ogloszenia",
             ADMIN: "Admin", MODERATOR: "Moderator"}
    return FakeGuild(GUILD, channels=channels, roles=[FakeRole(i, n) for i, n in names.items()])


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path / "data")


@pytest.fixture
def guild() -> FakeGuild:
    return make_guild()


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
async def services(settings: Settings, db: Database, clock: Clock, guild: FakeGuild):
    staff = StaffLog()
    staff.bind(asyncio.get_running_loop())
    client = SupervisorClient("http://127.0.0.1:1", None)
    store = SettingsStore(settings)
    scheduler = Scheduler(db, clock)
    built = Services(store, db, scheduler, staff, client, StaffThreads(db, store),
                     ReservationService(db, client, store, scheduler, staff.post))
    built.ids, _ = resolve(guild, settings)
    yield built
    await client.close()


@pytest.fixture
async def fake(services, clock):
    """A running fake supervisor, with the services' client pointed at it and its clock tied to the tests'."""
    from aiohttp.test_utils import TestServer
    from fake_supervisor import TOKEN, State, make_app

    state = State(now=clock.now)
    server = TestServer(make_app(state))
    await server.start_server()
    services.supervisor.url, services.supervisor.token = str(server.make_url("")).rstrip("/"), TOKEN
    yield state
    await server.close()
