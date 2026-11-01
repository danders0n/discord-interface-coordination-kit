"""What the cogs share: one object built at start-up and handed to every cog."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.db import Database
from app.reservations.service import ReservationService
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
    reservations: ReservationService
    # The configuration's channels and roles as ids on the server; empty until the bot has seen the guild.
    ids: Ids = field(default_factory=Ids)
    # The event stack, built by the bot (it needs the guild): app.bot.EventBot.build_events.
    events: Any = None
    signups: Any = None
    channels: Any = None
    attendance: Any = None  # phase 6: app.attendance.service.AttendanceService
    feedback: Any = None  # phase 6: app.feedback.service.FeedbackService
    profiles: Any = None  # phase 7: app.profiles.service.ProfileService
    # Steam links and squad.xml (phase 5), also built by the bot: app.bot.EventBot.build_links.
    links: Any = None
    squadxml: Any = None
    link_watch: Any = None
    # A member's role ids now, or None when they are not on the server.
    roles_of: Callable[[int], frozenset[int] | None] = lambda _member: None

    @property
    def settings(self) -> Settings:
        return self.store.current
