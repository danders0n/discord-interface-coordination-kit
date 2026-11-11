import pytest
from aiohttp.test_utils import TestServer
from fake_supervisor import TOKEN, State, make_app

from app import heartbeat
from app.supervisor_client import SupervisorClient, SupervisorError, SupervisorUnreachable


@pytest.fixture
async def supervisor():
    state = State()
    server = TestServer(make_app(state))
    await server.start_server()
    client = SupervisorClient(str(server.make_url("")), TOKEN)
    yield state, client
    await client.close()
    await server.close()


async def test_capabilities_gate_the_heartbeat(supervisor):
    state, client = supervisor
    assert await heartbeat.beat_once(client, refresh=True) is True
    assert state.heartbeats == [{"name": "discord-bot", "interval_s": 60}]
    state.capabilities = ["reservations"]
    assert await heartbeat.beat_once(client, refresh=True) is False
    assert len(state.heartbeats) == 1


async def test_an_older_supervisor_offers_nothing_new(supervisor):
    state, client = supervisor
    state.has_system_info = False
    info = await client.refresh_info()
    assert info.capabilities == frozenset() and not info.has("heartbeat")


async def test_errors_carry_the_supervisors_message(supervisor):
    _, client = supervisor
    client.token = "wrong"
    with pytest.raises(SupervisorError) as caught:
        await client.refresh_info()
    assert caught.value.status == 401 and caught.value.message == "Not authenticated"
    client.token = TOKEN
    with pytest.raises(SupervisorError) as caught:
        await client.request("POST", "/api/integrations/heartbeat", json={"name": "x", "interval_s": 1})
    assert caught.value.status == 422 and "interval_s" in caught.value.message


async def test_an_unreachable_supervisor_is_its_own_error():
    client = SupervisorClient("http://127.0.0.1:9", None)
    with pytest.raises(SupervisorUnreachable):
        await client.refresh_info()
    await client.close()


async def test_a_slug_is_one_path_segment_whatever_it_holds(supervisor):
    """``..``, ``/``, ``?`` and ``#`` in a slug are quoted, so the request can't land on another route."""
    state, client = supervisor
    state.server("event")["state"] = "running"
    for slug in ("event/stop#", "../reservations/current/end#", "event/stop?"):
        with pytest.raises(SupervisorError) as caught:
            await client.say(slug, "hi")
        assert caught.value.status == 404
    assert state.server("event")["state"] == "running"


async def test_the_heartbeat_notices_a_supervisor_updated_meanwhile(supervisor, monkeypatch):
    """Without the capability it used to keep the first answer until a restart."""
    import asyncio

    state, client = supervisor
    state.capabilities = ["reservations"]
    rounds = []

    async def sleep(_delay):
        rounds.append(1)
        if len(rounds) == 1:
            state.capabilities = ["reservations", "heartbeat"]  # updated during the hour's wait
        else:
            raise asyncio.CancelledError

    monkeypatch.setattr(heartbeat.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await heartbeat.run(client)
    assert state.heartbeats == [{"name": "discord-bot", "interval_s": 60}]
