"""The staff log channel (docs/PLAN.md, *Notifications*): short notes for staff, in the public language.

Anything can :meth:`StaffLog.post` from any thread, before or after the bot is connected; one task sends the queue
to the channel. A failure to send goes to the log file only (``app.staff_log`` is never sent back here)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

MAX_QUEUE = 200
MAX_MESSAGE = 2000
log = logging.getLogger(__name__)


def chunks(text: str, size: int = MAX_MESSAGE) -> list[str]:
    """Discord messages of at most ``size`` characters, split at line ends where possible."""
    parts: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


class StaffLog:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue(MAX_QUEUE)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def post(self, text: str) -> None:
        """Queue a note; safe from any thread. When the queue is full, the oldest note is dropped."""
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._put, text)

    def _put(self, text: str) -> None:
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(text)

    def drain(self) -> list[str]:
        """Take every queued note without sending it (for tests and for a last look at shutdown)."""
        notes = []
        while not self._queue.empty():
            notes.append(self._queue.get_nowait())
        return notes

    async def run(self, send: Callable[[str], Awaitable[None]]) -> None:
        while True:
            text = await self._queue.get()
            for part in chunks(text):
                try:
                    await send(part)
                except Exception as exc:  # noqa: BLE001 - the staff log must never take the bot down
                    log.warning("Could not post to the staff log: %s", exc)
