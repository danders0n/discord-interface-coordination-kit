"""The supervisor's live events (docs/PLAN-front-end.md, *Wake-up*): the SSE parser, the stream client and the
runner that wakes the workers."""

from __future__ import annotations

import asyncio

import pytest

from app.live import Live
from app.supervisor_client import StreamEvent, SupervisorError
from app.supervisor_client.sse import SseParser


def feed(text: str) -> list[StreamEvent]:
    parser = SseParser()
    return [event for line in text.splitlines(keepends=True) if (event := parser.feed(line)) is not None]


def test_the_parser_reads_messages_and_skips_comments():
    events = feed('event: hello\ndata: {"gap": true}\n\n: keep-alive\n\n'
                  'id: b:7\nevent: notice\ndata: {"kind": "notice", "seq": 7}\n\n')
    assert events == [StreamEvent("hello", {"gap": True}), StreamEvent("notice", {"kind": "notice", "seq": 7}, "b:7")]


async def test_the_stream_says_hello_then_the_events(services, fake):
    seen = []

    async def read():
        async for event in services.supervisor.stream(("notice",)):
            seen.append(event)
            if event.event == "notice":
                return

    reader = asyncio.create_task(read())
    while not fake.streams:
        await asyncio.sleep(0.01)
    fake.notice("roster_guard")
    await asyncio.wait_for(reader, 5)
    assert [e.event for e in seen] == ["hello", "notice"]
    assert seen[1].id == "fake:1" and seen[1].data["data"]["kind"] == "roster_guard"


async def test_a_refused_stream_raises_with_the_message(services, fake):
    services.supervisor.token = "wrong"
    with pytest.raises(SupervisorError) as caught:
        async for _ in services.supervisor.stream(("notice",)):
            pass
    assert caught.value.status == 401


def test_a_gap_wakes_every_listener_and_events_their_own():
    woken = []
    live = Live(None, {"notice": lambda e: woken.append(("notice", e.event)),
                       "operation": lambda e: woken.append(("operation", e.event))})
    live.handle(StreamEvent("hello", {"gap": False}))
    assert woken == []
    live.handle(StreamEvent("hello", {"gap": True}))
    assert sorted(woken) == [("notice", "gap"), ("operation", "gap")]
    woken.clear()
    live.handle(StreamEvent("notice", {}, "b:3"))
    assert woken == [("notice", "notice")] and live.last_id == "b:3"


async def test_the_runner_wakes_the_notices_worker(services, fake):
    woken = asyncio.Event()
    live = Live(services.supervisor, {"notice": lambda _e: woken.set()})
    runner = asyncio.create_task(live.run())
    try:
        while not fake.streams:
            await asyncio.sleep(0.01)
        fake.notice("login_link")
        await asyncio.wait_for(woken.wait(), 5)
        assert live.last_id == "fake:1"
    finally:
        runner.cancel()
