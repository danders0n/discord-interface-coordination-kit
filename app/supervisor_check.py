"""Is the supervisor ready for this bot? (docs/PLAN-front-end.md, *Start-up checks*.)

- ``GET /api/system/info`` reports an ``api_level`` of at least :data:`API_LEVEL` and the capabilities in
  :data:`CAPABILITIES` (the operations, their change feed and files);
- the bot's own account holds ``integration.act_as`` (``GET /api/auth/me``), which the roster push, the notices and
  every act-as call need;
- the supervisor's zone has the same UTC offset now as ``BOT_TIMEZONE`` (a warning only: times typed here are sent
  zone-aware, but staff reading both should see the same clock).

Until the check passes, the bot stays on Discord but holds back its supervisor work (the roster push, the notices,
the operations feed, the live events) and checks again every minute: refusing to start would only make the service
manager restart it in
a loop. A blocking problem goes to the staff log once (not a supervisor that is merely down: the supervisor's own
webhook reports that); so does a warning, and the moment it passes after being held back."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from app.i18n import t
from app.supervisor_client import SupervisorClient, SupervisorError, SupervisorUnreachable

log = logging.getLogger(__name__)
API_LEVEL = 2  # the operations API (the supervisor's phase 3); raised when the bot needs newer routes
CAPABILITIES = ("operations", "operation_feed", "operation_files")
ACT_AS = "integration.act_as"
RECHECK = 60.0


@dataclass(frozen=True)
class Verdict:
    blocking: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    unreachable: bool = False

    @property
    def ok(self) -> bool:
        return not self.blocking and not self.unreachable


def offset(tz: ZoneInfo, now: datetime) -> str:
    return now.astimezone(tz).strftime("%z")


async def check(client: SupervisorClient, tz: ZoneInfo, language: str, now: datetime) -> Verdict:
    try:
        info = await client.refresh_info()
        me = await client.me()
    except SupervisorUnreachable as exc:
        log.warning("Supervisor check: %s", exc.message)
        return Verdict(unreachable=True)
    except SupervisorError as exc:
        return Verdict((t("The supervisor refused the bot ({status}): {reason}. Check BOT_SUPERVISOR__TOKEN.",
                          language, status=exc.status, reason=exc.message),))
    blocking, warnings = [], []
    if info.api_level < API_LEVEL:
        blocking.append(t("The supervisor's API level is {have}; this bot needs {need}. Update the supervisor.",
                          language, have=info.api_level, need=API_LEVEL))
    missing = [name for name in CAPABILITIES if not info.has(name)]
    if missing:
        blocking.append(t("The supervisor lacks what this bot needs ({capabilities}). Update the supervisor.",
                          language, capabilities=", ".join(missing)))
    if ACT_AS not in (me.get("permissions") or []):
        blocking.append(t("The bot's supervisor account ({account}) lacks the permission {permission}: give it the "
                          "integration role.", language, account=me.get("username") or "?", permission=ACT_AS))
    ours = offset(tz, now)
    if info.utc_offset and info.utc_offset != ours:
        warnings.append(t("The supervisor's time zone ({theirs}, UTC{their_offset}) differs from BOT_TIMEZONE "
                          "({ours}, UTC{our_offset}): check both.", language, theirs=info.timezone or "?",
                          their_offset=info.utc_offset, ours=str(tz), our_offset=ours))
    return Verdict(tuple(blocking), tuple(warnings))


async def gate(client: SupervisorClient, tz: Callable[[], ZoneInfo], language: Callable[[], str],
               clock: Callable[[], datetime], post: Callable[[str], None], start: Callable[[], Awaitable[None]],
               sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
    """Check until the supervisor is ready, then ``start()`` the supervisor work, once."""
    told: tuple[str, ...] = ()
    held = False
    while True:
        verdict = await check(client, tz(), language(), clock())
        if verdict.ok:
            if held:
                post(t("The supervisor is ready: the bot starts its work with it.", language()))
            for warning in verdict.warnings:
                log.warning("Supervisor check: %s", warning)
                post(warning)
            await start()
            return
        held = True
        if verdict.blocking and verdict.blocking != told:
            for problem in verdict.blocking:
                log.error("Supervisor check: %s", problem)
            post(t("The bot holds back its work with the supervisor until this is fixed:", language()) + "\n"
                 + "\n".join(f"- {problem}" for problem in verdict.blocking))
            told = verdict.blocking
        await sleep(RECHECK)
