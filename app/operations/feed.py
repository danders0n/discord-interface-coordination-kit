"""The supervisor's change feed (docs/PLAN-front-end.md, *Sync and drawing*): ``GET /api/operations/changes?after=``
read oldest first, each whole operation handed to the channels (:meth:`Sink.apply`), each tombstone as gone.

- **Durable.** The cursor (``bot_state`` ``operations``) moves past a change only once the sink has kept it (the
  operation is in ``operation_channel``, and a redraw of it is due: the start-up redraw covers a crash meanwhile).
- **Reset.** When the supervisor can't replay what the bot missed (its tombstones were pruned, or the cursor is
  ahead of it), it answers ``reset``: every operation not over for long. The bot draws them all, and an operation it
  still draws that is missing from the list is gone.
- **Echo.** The bot's own changes come back too and are drawn again; drawing is idempotent, so that costs little.
- **Meta** (``GET /api/operations/meta``: the staff roles for the overwrites ...) is read on the first run, after a
  reset, and every ten minutes.

An ``operation`` live event (``app.live``) wakes the worker; it also looks every minute. No discord.py here."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.db import Database, state
from app.supervisor_client import SupervisorClient, SupervisorError

log = logging.getLogger(__name__)
CURSOR = "operations"
POLL = timedelta(minutes=1)
META_EVERY = timedelta(minutes=10)


class Sink(Protocol):
    meta: dict[str, Any]

    async def apply(self, op: dict[str, Any]) -> None: ...

    async def gone(self, op_id: int) -> None: ...

    async def known(self) -> set[int]: ...


class Feed:
    def __init__(self, db: Database, client: SupervisorClient, sink: Sink, clock: Callable[[], datetime],
                 poll: timedelta = POLL) -> None:
        self.db, self.client, self.sink, self.clock, self.poll = db, client, sink, clock, poll
        self._wake = asyncio.Event()
        self._down = False
        self._meta_at: datetime | None = None

    def wake(self) -> None:
        self._wake.set()

    async def cursor(self) -> int:
        return int((await state.get(self.db, CURSOR) or {}).get("after", 0))

    async def _meta(self, force: bool) -> None:
        now = self.clock()
        if force or self._meta_at is None or now - self._meta_at >= META_EVERY:
            self.sink.meta = await self.client.operations_meta()
            self._meta_at = now

    async def run_once(self) -> int:
        """Hand over what changed; returns how many changes. Raises SupervisorError if the feed can't be read."""
        await self._meta(force=False)
        after, count, seen, reset = await self.cursor(), 0, set(), False
        while True:
            body = await self.client.operation_changes(after)
            if body.get("reset") and not reset:
                log.warning("The operations feed was reset (the bot's place, %s, is unknown to the supervisor): "
                            "drawing everything again", after)
                reset = True
                await self._meta(force=True)
            for item in body.get("changes") or []:
                if "operation" in item:
                    seen.add(int(item["operation"]["id"]))
                    await self.sink.apply(item["operation"])
                elif "deleted" in item:
                    await self.sink.gone(int(item["deleted"]["id"]))
                after = int(item["seq"])
                await state.put(self.db, CURSOR, {"after": after})
                count += 1
            if not body.get("more"):
                if not body.get("changes") and int(body.get("seq") or 0) != after:
                    after = int(body.get("seq") or 0)  # (a reset of an empty list, or the supervisor started anew)
                    await state.put(self.db, CURSOR, {"after": after})
                break
        if reset:
            for op_id in await self.sink.known() - seen:
                await self.sink.gone(op_id)
        return count

    async def run(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.run_once()
                if self._down:
                    log.info("Operations feed: the supervisor answers again")
                self._down = False
            except SupervisorError as exc:
                if not self._down:  # a supervisor that is down is not news; a refused token is
                    level = logging.ERROR if exc.status in (401, 403) else logging.WARNING
                    log.log(level, "Reading the operations feed failed: %s", exc.message)
                self._down = True
            except Exception:  # noqa: BLE001 - the loop must survive a database or Discord hiccup
                log.exception("The operations feed failed")
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll.total_seconds())
            except TimeoutError:
                pass
