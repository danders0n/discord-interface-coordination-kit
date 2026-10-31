"""The bot reports in to the supervisor every minute (docs/PLAN.md, the ``heartbeat`` capability). When it falls
silent for five intervals, the supervisor alerts staff through its own Discord webhook, which still works when
the bot is down. Without the capability the loop only re-checks it now and then."""

from __future__ import annotations

import asyncio
import logging

from app.supervisor_client import SupervisorClient, SupervisorError

NAME = "discord-bot"
INTERVAL = 60
RECHECK = 3600  # how often to look for the capability again when the supervisor lacks it
log = logging.getLogger(__name__)


async def beat_once(client: SupervisorClient, refresh: bool) -> bool:
    """One round: (re)read the capabilities when asked, then report if the supervisor listens. True if sent."""
    if refresh:
        await client.refresh_info()
    if not client.info.has("heartbeat"):
        return False
    await client.heartbeat(NAME, INTERVAL)
    return True


async def run(client: SupervisorClient) -> None:
    refresh, unreachable = True, False
    while True:
        try:
            sent = await beat_once(client, refresh)
            if unreachable:
                log.info("The supervisor answers again")
            refresh, unreachable = False, False
            delay = INTERVAL if sent else RECHECK
        except SupervisorError as exc:
            if not unreachable:
                # a refused token is a configuration error staff must see; a supervisor that is down is not news
                level = logging.ERROR if exc.status in (401, 403) else logging.WARNING
                log.log(level, "Heartbeat failed: %s", exc.message)
            refresh, unreachable = True, True  # after a reconnect, read the capabilities again
            delay = INTERVAL
        await asyncio.sleep(delay)
