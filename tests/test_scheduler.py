from datetime import timedelta

from sqlalchemy import select

from app.db.models import ScheduledAction
from app.scheduler import backoff


async def rows(db):
    async with db.session() as session:
        return (await session.execute(select(ScheduledAction).order_by(ScheduledAction.id))).scalars().all()


async def test_runs_only_what_is_due_and_tells_how_late(services, clock):
    scheduler, seen = services.scheduler, []

    async def handler(action, now):
        seen.append((action.payload["n"], action.late(now)))

    scheduler.register("ping", handler)
    await scheduler.schedule("ping", clock.now - timedelta(hours=2), payload={"n": 1})
    await scheduler.schedule("ping", clock.now + timedelta(hours=1), payload={"n": 2})
    assert await scheduler.run_due() == 1
    assert seen == [(1, timedelta(hours=2))]
    done, waiting = await rows(services.db)
    assert done.done_at == clock.now and waiting.done_at is None


async def test_a_repeating_action_moves_on(services, clock):
    async def handler(action, now):
        return now + timedelta(days=1)

    services.scheduler.register("daily", handler)
    await services.scheduler.schedule("daily", clock.now, key="daily")
    await services.scheduler.run_due()
    (row,) = await rows(services.db)
    assert row.done_at is None and row.due_at == clock.now + timedelta(days=1)


async def test_failures_retry_later_and_reach_the_log_once(services, clock, caplog):
    async def handler(action, now):
        raise RuntimeError("supervisor down")

    services.scheduler.register("flaky", handler)
    await services.scheduler.schedule("flaky", clock.now)
    await services.scheduler.run_due()
    (row,) = await rows(services.db)
    assert row.attempts == 1 and row.last_error == "supervisor down"
    assert row.due_at == clock.now + backoff(1) and row.done_at is None
    clock.now = row.due_at
    await services.scheduler.run_due()
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    (row,) = await rows(services.db)
    assert row.attempts == 2


def test_backoff_grows_to_an_hour():
    assert [backoff(n) for n in (1, 2, 3)] == [timedelta(minutes=1), timedelta(minutes=2), timedelta(minutes=4)]
    assert backoff(20) == timedelta(hours=1)


async def test_a_key_moves_the_existing_action_even_when_done(services, clock):
    async def handler(action, now):
        return None

    services.scheduler.register("open", handler)
    await services.scheduler.schedule("open", clock.now, key="event:1:open")
    await services.scheduler.run_due()
    await services.scheduler.schedule("open", clock.now + timedelta(days=2), key="event:1:open")
    (row,) = await rows(services.db)
    assert row.done_at is None and row.due_at == clock.now + timedelta(days=2)
    assert await services.scheduler.cancel("event:1:open") is True
    assert await rows(services.db) == []


async def test_an_unknown_kind_fails_instead_of_vanishing(services, clock):
    await services.scheduler.schedule("nobody", clock.now)
    await services.scheduler.run_due()
    (row,) = await rows(services.db)
    assert row.attempts == 1 and "No handler" in row.last_error
