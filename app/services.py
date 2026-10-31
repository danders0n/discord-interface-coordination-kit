"""What the cogs share: one object built at start-up and handed to every cog."""

from __future__ import annotations

from dataclasses import dataclass

from app.db import Database
from app.scheduler import Scheduler
from app.settings import Settings, SettingsStore
from app.staff_log import StaffLog
from app.supervisor_client import SupervisorClient


@dataclass
class Services:
    store: SettingsStore
    db: Database
    scheduler: Scheduler
    staff: StaffLog
    supervisor: SupervisorClient

    @property
    def settings(self) -> Settings:
        return self.store.current
