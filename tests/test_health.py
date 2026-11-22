"""The supervisor's health as the bot sees it (app/health.py, docs/PLAN-remote-update.md, step 4): the signals from
calls, the ``system`` live event and ``hello``, the check after a restart, the staff log, the words for members."""

import asyncio
from datetime import timedelta

import discord
import pytest

from app.discord_util import supervisor_words
from app.health import SupervisorHealth
from app.live import Live
from app.supervisor_client import StreamEvent, SupervisorClient, SupervisorUnreachable


class Notes(list):
    def __call__(self, text: str) -> None:
        self.append(text)


@pytest.fixture
async def health(services, fake, clock):
    notes, problems = Notes(), []

    async def check():
        await services.supervisor.refresh_info()
        return tuple(problems)

    built = SupervisorHealth(services.supervisor, notes, lambda: "en", clock, check=check)
    built.notes, built.problems = notes, problems
    return built


async def settled(health: SupervisorHealth) -> None:
    for _ in range(20):
        await asyncio.sleep(0)
    if health._checking is not None:
        await health._checking


def restarting(to: str = "f" * 40, eta_s: int = 90, kind: str = "update") -> dict:
    return {"state": "restarting", "kind": kind, "to": to, "jobs": [], "eta_s": eta_s}


async def test_a_failed_call_is_down_and_an_answer_up_without_a_word(health, fake, services):
    fake.down = True  # 503: restarting behind a proxy
    with pytest.raises(Exception):
        await services.supervisor.operations_meta()
    assert health.state == "down"
    fake.down = False
    await services.supervisor.operations_meta()
    assert health.state == "up"
    assert health.notes == []  # down without an announcement is not news (its webhook says so)


async def test_an_announced_update_waits_and_comes_back(health, fake, services, clock):
    health.hello({"boot": "fake"})
    health.system(restarting(eta_s=600))
    assert health.state == "updating" and health.minutes_left() == 10
    assert health.notes == ["The supervisor is restarting for an update to fffffff; the bot waits for it."]
    fake.down = True
    with pytest.raises(Exception):
        await services.supervisor.operations_meta()
    assert health.state == "updating"  # (an announced update stays one while calls fail)
    fake.down, fake.boot, fake.commit = False, "fake-2", "def5678" + "0" * 33
    health.hello({"boot": "fake-2"})  # the stream is back, after a restart
    await settled(health)
    assert health.state == "up"
    assert health.notes[-1] == "The supervisor is back on def5678."


async def test_minutes_left_count_down(health, clock):
    health.system(restarting(eta_s=150))
    assert health.minutes_left() == 3
    clock.now += timedelta(seconds=200)
    assert health.minutes_left() == 0


async def test_a_cancelled_update_is_up_again(health):
    health.system(restarting())
    health.system({"state": "cancelled", "kind": "update"})
    assert health.state == "up"


async def test_a_restart_is_announced_as_a_restart(health):
    health.system(restarting(to=None, kind="restart"))
    assert health.notes == ["The supervisor is restarting; the bot waits for it."]


async def test_an_overstayed_update_is_told_once_and_its_return_too(health, fake, services, clock):
    health.system(restarting(eta_s=60))
    fake.down = True
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 1:
            clock.now += timedelta(minutes=12)  # past the announced minute and the ten more
        elif len(sleeps) >= 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await health.run(sleep=sleep)
    assert health.state == "down"
    assert health.notes[-1] == "The supervisor has not come back from its update for 12 min."
    assert sum("has not come back" in note for note in health.notes) == 1
    fake.down = False
    await services.supervisor.operations_meta()
    await settled(health)
    assert health.state == "up" and health.notes[-1] == "The supervisor is back on 5edb7e8."


async def test_a_supervisor_that_comes_back_too_old_blocks_until_fixed(health, fake):
    health.hello({"boot": "fake"})
    health.problems.append("The supervisor's API level is 1; this bot needs 2. Update the supervisor.")
    health.hello({"boot": "fake-2"})  # it restarted (a go back)
    await settled(health)
    assert health.state == "blocked"
    assert health.notes[-1].startswith("The bot holds back its work with the supervisor until this is fixed:")
    health.problems.clear()
    await health._recheck(tell=False)
    assert health.state == "up"
    assert health.notes[-1] == "The supervisor is ready: the bot starts its work with it."


async def test_listeners_hear_each_change(health):
    heard = []
    health.listeners.append(heard.append)
    health.system(restarting())
    health.system({"state": "failed"})
    assert heard == ["updating", "up"]


async def test_a_refused_connection_was_never_sent():
    client = SupervisorClient("http://127.0.0.1:1", None)
    seen = []
    client.on_call = lambda status, sent: seen.append((status, sent))
    try:
        with pytest.raises(SupervisorUnreachable) as caught:
            await client.operations_meta()
    finally:
        await client.close()
    assert caught.value.sent is False and seen == [(None, False)]


# ---------------------------------------------------------------- the live stream


async def test_the_stream_asks_for_system_events_only_when_the_supervisor_has_them(services, fake):
    live = Live(services.supervisor, {"notice": lambda e: None, "system": lambda e: None})
    await services.supervisor.refresh_info()
    assert live.kinds == ("notice", "system")
    fake.capabilities = [c for c in fake.capabilities if c != "system_events"]
    await services.supervisor.refresh_info()
    assert live.kinds == ("notice",)  # an older supervisor would refuse the unknown kind


async def test_the_stream_hands_hello_and_system_to_the_health(health):
    live = Live(health.client, {"system": lambda event: health.system(event.data.get("data") or {})},
                on_hello=health.hello)
    live.handle(StreamEvent("hello", {"boot": "b1", "gap": False}))
    live.handle(StreamEvent("system", {"kind": "system", "seq": 3, "data": restarting()}, "b1:3"))
    assert health.boot == "b1" and health.state == "updating"


async def test_a_gap_does_not_wake_the_system_listener(services):
    woken = []
    live = Live(services.supervisor, {"notice": lambda e: woken.append("notice"),
                                      "system": lambda e: woken.append("system")})
    live.handle(StreamEvent("hello", {"boot": "b", "gap": True}))
    assert woken == ["notice"]


# ---------------------------------------------------------------- the words for members


async def test_members_hear_that_the_app_is_updating(health, clock):
    error = SupervisorUnreachable("no answer", sent=False)
    assert supervisor_words(error, discord.Locale.british_english, health) == \
        "The app can't be reached right now. Try again in a few minutes."
    health.system(restarting(eta_s=240))
    assert supervisor_words(error, discord.Locale.british_english, health) == \
        "The app is updating and should be back in about 4 min. Try again then."
    assert "za około 4 min" in supervisor_words(error, discord.Locale.polish, health)
    clock.now += timedelta(minutes=5)
    assert supervisor_words(error, discord.Locale.british_english, health) == \
        "The app is updating and should be back any moment. Try again in a minute."
