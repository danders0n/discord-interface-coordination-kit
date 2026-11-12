"""The supervisor check (docs/PLAN-front-end.md, *Start-up checks*) against the fake supervisor."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from app.supervisor_check import API_LEVEL, check, gate

WARSAW = ZoneInfo("Europe/Warsaw")  # (the clock's 2 November: UTC+0100)


async def test_a_ready_supervisor_passes(services, fake, clock):
    fake.utc_offset = "+0100"
    verdict = await check(services.supervisor, WARSAW, "en", clock.now)
    assert verdict.ok and verdict.warnings == ()


async def test_an_old_supervisor_and_a_bot_without_act_as_are_held_back(services, fake, clock):
    fake.api_level = API_LEVEL - 1
    fake.bot_permissions = ["server.view"]
    verdict = await check(services.supervisor, WARSAW, "en", clock.now)
    assert not verdict.ok and len(verdict.blocking) == 2
    assert f"this bot needs {API_LEVEL}" in verdict.blocking[0]
    assert "lacks the permission integration.act_as" in verdict.blocking[1]


async def test_a_refused_token_is_blocking_and_a_down_supervisor_only_waits(services, fake, clock):
    services.supervisor.token = "wrong"
    refused = await check(services.supervisor, WARSAW, "en", clock.now)
    assert "BOT_SUPERVISOR__TOKEN" in refused.blocking[0]
    services.supervisor.url = "http://127.0.0.1:1"
    down = await check(services.supervisor, WARSAW, "en", clock.now)
    assert down.unreachable and down.blocking == () and not down.ok


async def test_another_time_zone_is_a_warning(services, fake, clock):
    fake.utc_offset, fake.timezone = "+0000", "UTC"
    verdict = await check(services.supervisor, WARSAW, "en", clock.now)
    assert verdict.ok and "differs from BOT_TIMEZONE (Europe/Warsaw, UTC+0100)" in verdict.warnings[0]


async def test_the_gate_holds_back_tells_staff_once_and_starts_when_fixed(services, fake, clock):
    fake.bot_permissions = []
    notes, started, rounds = [], [], []

    async def start():
        started.append(True)

    async def sleep(_seconds):
        rounds.append(1)
        if len(rounds) == 2:
            fake.bot_permissions = ["integration.act_as"]  # staff fixed it

    await gate(services.supervisor, lambda: WARSAW, lambda: "en", lambda: clock.now, notes.append, start, sleep)
    assert started == [True] and len(rounds) == 2
    assert len(notes) == 2  # the problem once (not every minute), then that it is ready
    assert notes[0].startswith("The bot holds back its work with the supervisor") and "integration.act_as" in notes[0]
    assert notes[1] == "The supervisor is ready: the bot starts its work with it."


async def test_the_gate_starts_at_once_when_ready(services, fake, clock):
    notes, started = [], []

    async def start():
        started.append(True)

    await gate(services.supervisor, lambda: WARSAW, lambda: "en", lambda: clock.now, notes.append, start)
    assert started == [True] and notes == []
