"""The supervisor's health as the bot sees it (docs/PLAN-remote-update.md, *The supervisor's health*). No discord.py.

| state | meaning |
|---|---|
| ``up`` | it answers |
| ``updating`` | it announced its restart (the ``system`` live event ``restarting``) and is not back yet |
| ``down`` | no answer, nothing announced, or an update that overstayed |
| ``blocked`` | it answers, but after coming back the supervisor check fails (an older API after a go back) |

The signals: every client call (``SupervisorClient.on_call``: an answer, a refused connection, a 502-504), the live
stream's ``system`` events and its ``hello`` (a new ``boot``: it restarted), and while it is away a poll of
``GET /api/system/info`` every :data:`POLL` seconds. Coming back after an update or a restart runs the supervisor
check again (the API level may have changed) before it counts as ``up``; ``listeners`` hear every change (the queue
of members' actions, step 5).

Staff hear of an announced update, of its end and of an update that does not end; a supervisor that is merely down
without a word is not news here (its own webhook says so), as ``supervisor_check`` decided."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from app.i18n import t
from app.supervisor_client import DOWN_STATUSES, SupervisorClient, SupervisorError

log = logging.getLogger(__name__)
POLL = 10.0
OVERSTAY = timedelta(minutes=10)  # past the announced time: staff hear it once and it counts as down
DEFAULT_ETA = 90

Check = Callable[[], Awaitable[tuple[str, ...]]]  # the supervisor check's blocking problems (empty: fine)


def short_version(version: Any) -> str | None:
    """The supervisor's commit from ``GET /api/system/info``'s ``version`` (``build_info()``), short."""
    commit = version.get("commit") if isinstance(version, dict) else None
    return str(commit)[:7] if commit else None


class SupervisorHealth:
    def __init__(self, client: SupervisorClient, post: Callable[[str], None], language: Callable[[], str],
                 clock: Callable[[], datetime], check: Check | None = None) -> None:
        self.client, self.post, self.language, self.clock = client, post, language, clock
        self.check = check
        self.state = "up"
        self.since = clock()
        self.eta: datetime | None = None  # updating: when it said it would be back
        self.to: str | None = None
        self.boot: str | None = None
        self.version: str | None = None  # its commit when last read
        self.told_overstay = False
        self.restarted = False  # it announced a restart or came back with a new boot: check it before `up`
        self.listeners: list[Callable[[str], None]] = []
        self._wake = asyncio.Event()
        self._checking: asyncio.Task[None] | None = None
        client.on_call = self.call_seen

    # ---------------------------------------------------------------- what others read

    @property
    def away(self) -> bool:
        return self.state in ("updating", "down")

    def minutes_left(self) -> int | None:
        """Updating: whole minutes until the announced time (0 once it passed); else None."""
        if self.state != "updating" or self.eta is None:
            return None
        return max(0, int((self.eta - self.clock()).total_seconds() + 59) // 60)

    # ---------------------------------------------------------------- signals

    def call_seen(self, status: int | None, sent: bool | None) -> None:
        if status is None or status in DOWN_STATUSES:
            if self.state in ("up", "blocked"):
                self._set("down")
        elif self.state in ("down", "updating"):
            self._back()

    def system(self, data: dict[str, Any]) -> None:
        """The supervisor's own update or restart (the ``system`` live event)."""
        state = data.get("state")
        if state == "restarting":
            eta = int(data.get("eta_s") or DEFAULT_ETA)
            self.eta, self.to = self.clock() + timedelta(seconds=eta), data.get("to") or None
            self.told_overstay, self.restarted = False, True
            self._set("updating")
            language = self.language()
            self.post(t("The supervisor is restarting for an update to {commit}; the bot waits for it.", language,
                        commit=str(self.to)[:7]) if self.to and data.get("kind") != "restart" else
                      t("The supervisor is restarting; the bot waits for it.", language))
        elif state in ("cancelled", "failed") and self.state == "updating":
            self.restarted = False
            self._set("up")

    def hello(self, data: dict[str, Any]) -> None:
        """The stream (re)connected: the supervisor answers; a new ``boot`` means it restarted meanwhile."""
        boot = data.get("boot")
        if self.boot is not None and boot is not None and boot != self.boot:
            self.restarted = True
        self.boot = boot or self.boot
        if self.restarted or self.state in ("down", "updating"):
            self._back()

    # ---------------------------------------------------------------- changes

    def _set(self, state: str) -> None:
        if state == self.state:
            return
        log.info("The supervisor is %s (was %s)", state, self.state)
        self.state, self.since = state, self.clock()
        if state != "updating":
            self.eta = None
        self._wake.set()
        for listener in list(self.listeners):
            try:
                listener(state)
            except Exception:  # noqa: BLE001 - one listener must not stop the others
                log.exception("A listener of the supervisor's health failed")

    def _back(self) -> None:
        tell = self.state == "updating" or self.told_overstay
        if self.restarted and self.check is not None:
            if self._checking is None or self._checking.done():
                self._checking = asyncio.get_running_loop().create_task(self._recheck(tell))
            return
        self._up(tell)

    def _up(self, tell: bool) -> None:
        self.told_overstay = self.restarted = False
        self._set("up")
        if tell:
            self.version = short_version(self.client.info.version) or self.version
            language = self.language()
            self.post(t("The supervisor is back on {commit}.", language, commit=self.version) if self.version
                      else t("The supervisor is back.", language))

    async def _recheck(self, tell: bool) -> None:
        try:
            problems = await self.check() if self.check is not None else ()
        except SupervisorError:
            return  # away again: the next answer tries once more
        except Exception:  # noqa: BLE001 - counted as fine rather than holding everything back for a bug
            log.exception("The supervisor check after its restart failed")
            problems = ()
        if problems:
            if self.state != "blocked":
                self._set("blocked")
                self.post(t("The bot holds back its work with the supervisor until this is fixed:", self.language())
                          + "\n" + "\n".join(f"- {problem}" for problem in problems))
            return
        was_blocked = self.state == "blocked"
        self._up(tell and not was_blocked)
        if was_blocked:
            self.post(t("The supervisor is ready: the bot starts its work with it.", self.language()))

    # ---------------------------------------------------------------- the loop

    async def run(self, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        """While the supervisor is away (or blocked), ask it every POLL seconds; otherwise wait for a change."""
        while True:
            if self.state == "up":
                self._wake.clear()
                await self._wake.wait()
                continue
            if self.state == "blocked":
                await self._recheck(tell=False)
            else:
                self._overstayed()
                try:
                    await self.client.refresh_info()  # an answer moves the state on (call_seen)
                except SupervisorError:
                    pass
            await sleep(POLL)

    def _overstayed(self) -> None:
        if self.state != "updating" or self.eta is None or self.told_overstay:
            return
        if self.clock() < self.eta + OVERSTAY:
            return
        self.told_overstay = True
        minutes = int((self.clock() - self.since).total_seconds() // 60)
        self.post(t("The supervisor has not come back from its update for {minutes} min.", self.language(),
                    minutes=minutes))
        self._set("down")
