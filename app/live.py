"""The supervisor's live events (docs/PLAN-front-end.md, *Sync and drawing*, *Wake-up*): ``GET /api/events/stream``
kept open, reconnecting with the last event id. Events only wake workers; the truth is always re-read through the
normal routes, so a lost event costs a minute at most (the workers also poll). A ``hello`` with ``gap: true`` (the
supervisor restarted, or the bot was away too long) wakes every worker.

It listens for ``notice`` (the notices worker) and ``operation`` (the operations feed), and with the supervisor's
``system_events`` capability for ``system`` (its own update or restart: app/health.py). ``on_hello`` hears every
``hello`` (its ``boot`` changes with every start of the supervisor)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.supervisor_client import StreamEvent, SupervisorClient, SupervisorError

log = logging.getLogger(__name__)
MAX_DELAY = 60.0
# Kinds asked for only when the supervisor has the capability: an older one refuses an unknown kind (422).
OPTIONAL = {"system": "system_events"}


class Live:
    def __init__(self, client: SupervisorClient, listeners: dict[str, Callable[[StreamEvent], None]],
                 on_hello: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.client, self.listeners, self.on_hello = client, listeners, on_hello
        self.last_id: str | None = None

    @property
    def kinds(self) -> tuple[str, ...]:
        info = self.client.info
        return tuple(sorted(kind for kind in self.listeners if kind not in OPTIONAL or info.has(OPTIONAL[kind])))

    def _all(self) -> None:
        for kind, listener in self.listeners.items():
            if kind not in OPTIONAL:
                listener(StreamEvent("gap"))

    def handle(self, event: StreamEvent) -> None:
        if event.event == "hello":
            if self.on_hello is not None:
                self.on_hello(event.data)
            if event.data.get("gap"):
                self._all()
            return
        if event.id:
            self.last_id = event.id
        listener = self.listeners.get(event.event)
        if listener is not None:
            listener(event)

    async def run(self) -> None:
        delay, down = 1.0, False
        while True:
            try:
                async for event in self.client.stream(self.kinds, self.last_id):
                    if down:
                        log.info("The supervisor's live events are back")
                        self._all()  # whatever happened while the stream was away
                    delay, down = 1.0, False
                    self.handle(event)
            except SupervisorError as exc:
                if not down:
                    level = logging.ERROR if exc.status in (401, 403, 422) else logging.WARNING
                    log.log(level, "The supervisor's live events: %s", exc.message)
                down = True
            except Exception:  # noqa: BLE001 - the loop must outlive one bad message
                log.exception("The supervisor's live events failed")
                down = True
            await asyncio.sleep(delay)
            delay = min(delay * 2, MAX_DELAY)
