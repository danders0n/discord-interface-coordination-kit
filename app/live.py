"""The supervisor's live events (docs/PLAN-front-end.md, *Sync and drawing*, *Wake-up*): ``GET /api/events/stream``
kept open, reconnecting with the last event id. Events only wake workers; the truth is always re-read through the
normal routes, so a lost event costs a minute at most (the workers also poll). A ``hello`` with ``gap: true`` (the
supervisor restarted, or the bot was away too long) wakes every worker.

So far it listens for ``notice``; ``operation`` joins when the operations API exists (the supervisor answers 422
to a kind it doesn't know)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from app.supervisor_client import StreamEvent, SupervisorClient, SupervisorError

log = logging.getLogger(__name__)
MAX_DELAY = 60.0


class Live:
    def __init__(self, client: SupervisorClient, listeners: dict[str, Callable[[StreamEvent], None]]) -> None:
        self.client, self.listeners = client, listeners
        self.last_id: str | None = None

    @property
    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self.listeners))

    def _all(self) -> None:
        for listener in self.listeners.values():
            listener(StreamEvent("gap"))

    def handle(self, event: StreamEvent) -> None:
        if event.event == "hello":
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
