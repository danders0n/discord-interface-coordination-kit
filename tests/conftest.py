from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import discord
import pytest
from fakes import FakeChannel, FakeGuild, FakeRole

from app.db import Database, migrate
from app.resolve import resolve
from app.scheduler import Scheduler
from app.services import Services
from app.settings import Settings, SettingsStore
from app.staff_log import StaffLog
from app.staff_threads import StaffThreads
from app.supervisor_client import SupervisorClient

# Ids used across the tests.
GUILD, OTHER_CATEGORY, EVENTS, HEADER, ANNOUNCE, STAFF = 100, 201, 202, 203, 204, 206
MEMBER, MISSION_MAKER, KADET, ANNOUNCEMENT, ADMIN, MODERATOR = 301, 302, 303, 304, 305, 306


def make_settings(data_dir: Path, **overrides) -> Settings:
    values = {
        "discord_token": "token", "guild_id": GUILD, "data_dir": data_dir,
        "group": {"acronym": "IBC", "name": "Test group"},
        "channels": {"events_category": EVENTS, "header": HEADER,
                     "announcements": ANNOUNCE, "staff": STAFF},
        "roles": {"announcement": ANNOUNCEMENT},
        "permissions": {"bot_admin": [ADMIN]},
    } | overrides
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def make_guild() -> FakeGuild:
    """The server as `make_settings` describes it."""
    category = discord.ChannelType.category
    channels = [FakeChannel(OTHER_CATEGORY, "Szkice", category), FakeChannel(EVENTS, "Eventy", category),
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
    built = Services(store, db, scheduler, staff, client, StaffThreads(db, store))
    built.ids, _ = resolve(guild, settings)
    yield built
    await client.close()


async def settle(channels) -> None:
    """Let the channels' background work finish (redraws, renames) before the database closes."""
    for _ in range(3):  # (a redraw may ask for another)
        await channels.redrawer.settle()
        await channels.sorter.settle()
        if channels.header is not None:
            await channels.header.redrawer.settle()
        await asyncio.gather(*channels._renames.values(), return_exceptions=True)


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
    state.close_streams()
    await server.close()
