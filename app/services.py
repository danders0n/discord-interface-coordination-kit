"""What the cogs share: one object built at start-up and handed to every cog."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.db import Database
from app.resolve import Ids
from app.scheduler import Scheduler
from app.settings import Settings, SettingsStore
from app.staff_log import StaffLog
from app.staff_threads import StaffThreads
from app.supervisor_client import SupervisorClient


@dataclass
class Services:
    store: SettingsStore
    db: Database
    scheduler: Scheduler
    staff: StaffLog
    supervisor: SupervisorClient
    threads: StaffThreads
    # The configuration's channels and roles as ids on the server; empty until the bot has seen the guild.
    ids: Ids = field(default_factory=Ids)
    # Built by the bot (they need the guild): app.bot.EventBot.build_operations, build_front_end, build_links.
    channels: Any = None  # app.channel.service.OperationChannels
    feed: Any = None  # app.operations.feed.Feed
    archive: Any = None  # app.archive.service.ArchiveService
    exporter: Any = None  # app.archive.export.Exporter (moves the oldest quarter into the database)
    links: Any = None  # app.links.service.LinkService
    link_watch: Any = None  # app.links.watch.LinkWatch
    roster: Any = None  # app.roster.service.Roster
    notices: Any = None  # app.notices.service.Notices
    live: Any = None  # app.live.Live
    # A member's role ids now, or None when they are not on the server.
    roles_of: Callable[[int], frozenset[int] | None] = lambda _member: None

    @property
    def settings(self) -> Settings:
        return self.store.current
