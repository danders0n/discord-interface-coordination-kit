"""The header channel's message (docs/PLAN.md, *Creating an event*, *Entry point*, *Calendar*): how to start an
operation, and the published operations of the next ``calendar_days`` (date, kind, title, link), drawn from the
operations as last drawn (``operation_channel``). Posted when missing, edited in place (its id is kept in
``bot_state``), redrawn in batches whenever an operation changes. At start the channel is searched once, so a
header the database doesn't know is adopted and extra ones deleted.

Its *Create operation* button opens the wizard (app/operations/ui.py); it keeps working after a restart."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import discord
from sqlalchemy import select

from app.channel.render import plain, ts, when
from app.channel.service import Redrawer
from app.db import Database, state
from app.db.models import OperationChannel
from app.i18n import t
from app.operations.ui import header_view
from app.resolve import Ids
from app.settings import SettingsStore

KEY = "header"
SWEEP = 50  # recent messages searched for a header at start; the channel is read-only for members
log = logging.getLogger(__name__)


class Header:
    def __init__(self, db: Database, store: SettingsStore, ids: Callable[[], Ids], guild: Callable[[], Any],
                 clock: Callable[[], datetime], interval: float = 2.0) -> None:
        self.db, self.store, self.ids, self.guild, self.clock = db, store, ids, guild, clock
        self.redrawer = Redrawer(lambda _key: self.draw(), interval)
        self._swept = False  # the channel was searched for headers the database doesn't know

    def request(self) -> None:
        self.redrawer.request(0)

    async def calendar(self) -> list[str]:
        now = self.clock()
        until = now + timedelta(days=self.store.current.times.calendar_days)
        async with self.db.session() as session:
            rows = (await session.execute(select(OperationChannel).where(
                OperationChannel.status == "published", OperationChannel.gone_at.is_(None),
                OperationChannel.start_at > now).order_by(OperationChannel.start_at))).scalars().all()
        lines = []
        for row in rows:
            op = row.operation
            assembly = when(op, "assembly_at")
            if assembly is None or assembly > until:
                continue
            link = f" · <#{row.channel_id}>" if row.channel_id else ""
            lines.append(f"{ts(assembly, 'f')} · {plain(op.get('kind_name') or op['kind'])} · "
                         f"**{plain(op['title'])}**{link}")
        return lines

    async def message(self) -> discord.Embed:
        language = self.store.current.language
        lines = await self.calendar()
        embed = discord.Embed(title=t("Operations", language),
                              description=t("Press Create operation to plan one (or make it in the app); each gets its "
                                            "channel here.", language))
        embed.add_field(name=t("The next {days} days", language, days=self.store.current.times.calendar_days),
                        value="\n".join(lines)[:1024] if lines else t("Nothing planned yet.", language), inline=False)
        return embed

    async def found(self, channel: Any, me: int) -> list[int]:
        """The ids of the bot's header messages among the channel's recent ones, newest first: the bot's messages
        with an embed (the channel is read-only for members, and the bot posts nothing else there)."""
        ids = []
        try:
            async for message in channel.history(limit=SWEEP):
                if getattr(message.author, "id", None) == me and getattr(message, "embeds", None):
                    ids.append(message.id)
        except discord.HTTPException:
            log.warning("Could not read the header channel's history to find the header message")
        return ids

    async def draw(self) -> None:
        guild = self.guild()
        channel = guild.get_channel(self.ids().header) if guild is not None and self.ids().header else None
        if channel is None:
            return
        embed = await self.message()
        stored = await state.get(self.db, KEY)
        known = stored["id"] if stored and stored.get("channel", channel.id) == channel.id else None
        if not self._swept:
            # Once per start: a header the database doesn't know (another copy of the data, a restored backup) is
            # adopted rather than joined by a second one, and any extra headers are deleted.
            self._swept = True
            headers = await self.found(channel, guild.me.id)
            if headers:
                keep = known if known in headers else headers[0]
                for other in headers:
                    if other != keep:
                        try:
                            await channel.get_partial_message(other).delete()
                        except discord.HTTPException:
                            log.warning("Could not delete the extra header message %s", other)
                known = keep
        if known:
            try:
                await channel.get_partial_message(known).edit(embed=embed, view=header_view(self.store.current.language))
                if not stored or stored.get("id") != known or stored.get("channel") != channel.id:
                    await state.put(self.db, KEY, {"id": known, "channel": channel.id})
                return
            except discord.NotFound:
                pass
        sent = await channel.send(embed=embed, view=header_view(self.store.current.language))
        await state.put(self.db, KEY, {"id": sent.id, "channel": channel.id})
