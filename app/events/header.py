"""The header channel's message (docs/PLAN.md, *Creating an event*, *Entry point*, *Calendar*): how to start an
event, the published events of the next ``calendar_days`` (date, kind, title, taken / total slots or Coming /
limit, link), and the persistent *Create event* button. Posted when missing, edited in place (its id is kept in
``bot_state``), redrawn in batches whenever an event changes."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import discord
from sqlalchemy import func, select

from app.db import Database, state
from app.db.models import Event, Signup, Slot
from app.eventchannel.render import kind_label, ts
from app.eventchannel.service import Redrawer
from app.i18n import t
from app.resolve import Ids
from app.settings import SettingsStore

KEY = "header"
log = logging.getLogger(__name__)


class Header:
    def __init__(self, db: Database, store: SettingsStore, ids: Callable[[], Ids], guild: Callable[[], Any],
                 clock: Callable[[], datetime], interval: float = 2.0) -> None:
        self.db, self.store, self.ids, self.guild, self.clock = db, store, ids, guild, clock
        self.redrawer = Redrawer(lambda _key: self.draw(), interval)

    def request(self) -> None:
        self.redrawer.request(0)

    async def calendar(self) -> list[str]:
        now = self.clock()
        until = now + timedelta(days=self.store.current.times.calendar_days)
        async with self.db.session() as session:
            events = (await session.execute(select(Event).where(
                Event.status == "published", Event.start_at > now, Event.assembly_at <= until)
                .order_by(Event.assembly_at))).scalars().all()
            lines = []
            for event in events:
                if event.kind == "zeus":
                    taken = (await session.execute(select(func.count()).select_from(Signup).where(
                        Signup.event_id == event.id, Signup.status == "coming"))).scalar()
                    places = f"{taken}/{event.signup_limit}" if event.signup_limit else str(taken)
                else:
                    total, taken = (await session.execute(select(func.count(Slot.id), func.count(Slot.holder_id))
                                                          .where(Slot.event_id == event.id))).one()
                    places = f"{taken}/{total}"
                link = f" · <#{event.channel_id}>" if event.channel_id else ""
                lines.append(f"{ts(event.assembly_at, 'f')} · {kind_label(event.kind, self.store.current.language)} · "
                             f"**{event.title}** · {places}{link}")
        return lines

    async def message(self) -> tuple[discord.Embed, discord.ui.View]:
        from app.events.ui import CreateEvent

        language = self.store.current.language
        lines = await self.calendar()
        embed = discord.Embed(title=t("Events", language),
                              description=t("Press Create event to start one: a short wizard makes a draft, which you "
                                            "finish in its channel with Organiser panel.", language))
        embed.add_field(name=t("The next {days} days", language, days=self.store.current.times.calendar_days),
                        value="\n".join(lines)[:1024] if lines else t("Nothing planned yet.", language), inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(CreateEvent(t("Create event", language)))
        return embed, view

    async def draw(self) -> None:
        guild = self.guild()
        channel = guild.get_channel(self.ids().header) if guild is not None and self.ids().header else None
        if channel is None:
            return
        embed, view = await self.message()
        stored = await state.get(self.db, KEY)
        if stored:
            try:
                await channel.get_partial_message(stored["id"]).edit(embed=embed, view=view)
                return
            except discord.NotFound:
                pass
        sent = await channel.send(embed=embed, view=view)
        await state.put(self.db, KEY, {"id": sent.id})
