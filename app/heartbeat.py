"""The bot reports in to the supervisor every minute (docs/PLAN.md, the ``heartbeat`` capability). When it falls
silent for five intervals, the supervisor alerts staff through its own Discord webhook, which still works when
the bot is down. Without the capability the loop only re-checks it now and then.

With the ``heartbeat_state`` capability (docs/PLAN-remote-update.md, *Heartbeat*) a report also carries the bot's
``version`` and ``health``, and :func:`announce` sends one out of turn: ``updating`` (with ``to`` and ``eta_s``) just
before a restart for ``/update``, so the supervisor holds its alert while the bot is away as announced, and
``stopping`` before a plain stop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from app.supervisor_client import SupervisorClient, SupervisorError

NAME = "discord-bot"
INTERVAL = 60
RECHECK = 3600  # how often to look for the capability again when the supervisor lacks it
ANNOUNCE_SECONDS = 5.0  # an announcement must not hold up a restart or a stop
ETA, ETA_PIP = 60, 600  # seconds away for an update: a plain restart, or one installing new dependencies
log = logging.getLogger(__name__)

Health = Callable[[], dict[str, Any]]


def _extra(client: SupervisorClient, version: str, health: Health | None) -> dict[str, Any]:
    if not client.info.has("heartbeat_state"):
        return {}
    extra: dict[str, Any] = {"version": version[:40] or None}
    if health is not None:
        try:
            extra["health"] = health()
        except Exception:  # noqa: BLE001 - a report without its health is better than none
            log.exception("Reading the bot's health failed")
    return extra


async def beat_once(client: SupervisorClient, refresh: bool, version: str = "",
                    health: Health | None = None) -> bool:
    """One round: (re)read the capabilities when asked, then report if the supervisor listens. True if sent."""
    if refresh:
        await client.refresh_info()
    if not client.info.has("heartbeat"):
        return False
    await client.heartbeat(NAME, INTERVAL, **_extra(client, version, health))
    return True


async def announce(client: SupervisorClient, state: str, version: str = "", to: str = "", eta_s: int = 0,
                   health: Health | None = None) -> bool:
    """One report out of turn (``updating`` / ``stopping``), within :data:`ANNOUNCE_SECONDS`. False when the
    supervisor doesn't take it (an older one, or away): the restart or stop goes on regardless."""
    try:
        async with asyncio.timeout(ANNOUNCE_SECONDS):
            info = await client.ensure_info()
            if not (info.has("heartbeat") and info.has("heartbeat_state")):
                return False
            extra = _extra(client, version, health) | {"state": state}
            if state == "updating":
                extra |= {"to": to[:40] or None, "eta_s": max(0, min(eta_s, 3600))}
            await client.heartbeat(NAME, INTERVAL, **extra)
            return True
    except (SupervisorError, TimeoutError) as exc:
        log.warning("Could not tell the supervisor the bot is %s: %s", state, exc)
        return False


async def run(client: SupervisorClient, version: Callable[[], str] = lambda: "",
              health: Health | None = None) -> None:
    refresh, unreachable = True, False
    while True:
        try:
            sent = await beat_once(client, refresh, version(), health)
            if unreachable:
                log.info("The supervisor answers again")
            # Without the capability, read the capabilities again at the next check: the supervisor may have been
            # updated meanwhile.
            refresh, unreachable = not sent, False
            delay = INTERVAL if sent else RECHECK
        except SupervisorError as exc:
            if not unreachable:
                # a refused token is a configuration error staff must see; a supervisor that is down is not news
                level = logging.ERROR if exc.status in (401, 403) else logging.WARNING
                log.log(level, "Heartbeat failed: %s", exc.message)
            refresh, unreachable = True, True  # after a reconnect, read the capabilities again
            delay = INTERVAL
        await asyncio.sleep(delay)
