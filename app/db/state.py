"""Small values the bot keeps between restarts (the ``bot_state`` table)."""

from __future__ import annotations

from typing import Any

from app.db.engine import Database
from app.db.models import BotState


async def get(db: Database, key: str) -> dict[str, Any] | None:
    async with db.session() as session:
        row = await session.get(BotState, key)
        return dict(row.value) if row is not None else None


async def put(db: Database, key: str, value: dict[str, Any]) -> None:
    async with db.session() as session, session.begin():
        row = await session.get(BotState, key)
        if row is None:
            session.add(BotState(key=key, value=value))
        else:
            row.value = value
