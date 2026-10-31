"""The host reservation of an event (docs/PLAN.md, *Supervisor: reservation*), against the fake supervisor."""

import asyncio
from datetime import timedelta

import pytest

from app.reservations.service import Frozen, Window


def window(clock, days=3, hours=4, name="18-10-ibc Operation X") -> Window:
    start = clock.now + timedelta(days=days)
    return Window(name, start, start + timedelta(hours=hours), link="https://discord.com/channels/1/2")


def at(clock, fake, when) -> None:
    clock.now = fake.now = when


async def staff_notes(services) -> list[str]:
    await asyncio.sleep(0)
    return services.staff.drain()


async def test_booking_makes_a_managed_reservation_with_zoned_times(services, fake, clock):
    w = window(clock, name="x" * 100)
    row = await services.reservations.book(7, w)
    assert row.state == "booked" and row.reservation_id == 1
    stored = fake.reservations[1]
    assert stored["managed_by"] == "discord-bot" and stored["external_ref"] == "event:7"
    assert stored["starts_at"] == w.starts_at and stored["name"] == "x" * 80
    assert stored["server_id"] == 2 and stored["link"] == w.link and stored["quiet_minutes"] == 30


async def test_moving_updates_the_same_reservation(services, fake, clock):
    await services.reservations.book(7, window(clock))
    moved = window(clock, days=4)
    row = await services.reservations.book(7, moved)
    assert row.reservation_id == 1 and len(fake.reservations) == 1
    assert fake.reservations[1]["starts_at"] == moved.starts_at


async def test_a_lost_answer_finds_the_reservation_it_made(services, fake, clock):
    """The bot lost the id (crash between the create and the save): the managed create returns the old one."""
    first = window(clock)
    await services.reservations.book(7, first)
    await services.reservations._save(7, reservation_id=None)
    later = window(clock, days=5)
    row = await services.reservations.book(7, later)
    assert row.reservation_id == 1 and len(fake.reservations) == 1
    assert fake.reservations[1]["starts_at"] == later.starts_at  # and it was moved to the new times


async def test_without_managed_refs_the_name_and_start_find_it(services, fake, clock):
    fake.capabilities = ["reservations"]
    w = window(clock)
    await services.reservations.book(7, w)
    assert "managed_by" not in fake.reservations[1]
    await services.reservations._save(7, reservation_id=None)
    row = await services.reservations.book(7, w)
    assert row.reservation_id == 1 and len(fake.reservations) == 1


async def test_a_clash_fails_with_the_supervisors_message_and_tells_staff(services, fake, clock):
    await services.reservations.book(1, window(clock))
    row = await services.reservations.book(2, window(clock, hours=2))
    assert row.state == "failed" and row.reservation_id is None and "Overlaps" in row.error
    notes = await staff_notes(services)
    assert len(notes) == 1 and "Overlaps" in notes[0]
    assert not await services.scheduler.exists("reservation_retry:2")


async def test_retry_after_staff_resolve_the_clash(services, fake, clock):
    await services.reservations.book(1, window(clock))
    await services.reservations.book(2, window(clock, hours=2))
    await services.reservations.cancel(1)
    row = await services.reservations.retry(2)
    assert row.state == "booked" and row.error is None


async def test_an_unreachable_supervisor_is_retried_until_it_answers(services, fake, clock):
    url = services.supervisor.url
    services.supervisor.url = "http://127.0.0.1:9"
    row = await services.reservations.book(7, window(clock))
    assert row.state == "retrying" and "could not be reached" in row.error
    assert await staff_notes(services) == []  # not news yet: it retries by itself
    at(clock, fake, clock.now + timedelta(minutes=1))
    await services.scheduler.run_due()  # still down: the scheduler backs off
    assert (await services.reservations.get(7)).state == "retrying"
    services.supervisor.url = url
    at(clock, fake, clock.now + timedelta(minutes=5))
    await services.scheduler.run_due()
    row = await services.reservations.get(7)
    assert row.state == "booked" and row.reservation_id == 1


async def test_an_unreachable_supervisor_gives_up_once_the_window_is_over(services, fake, clock):
    services.supervisor.url = "http://127.0.0.1:9"
    w = window(clock, days=0, hours=1)
    w = Window(w.name, clock.now - timedelta(hours=2), clock.now - timedelta(hours=1))
    row = await services.reservations.book(7, w)
    assert row.state == "failed"


async def test_times_are_frozen_from_the_quiet_period(services, fake, clock):
    w = window(clock)
    await services.reservations.book(7, w)
    at(clock, fake, w.starts_at - timedelta(minutes=20))
    with pytest.raises(Frozen):
        await services.reservations.book(7, window(clock, days=2))
    row = await services.reservations.book(7, Window("renamed", w.starts_at, w.ends_at, w.link))
    assert row.state == "booked" and fake.reservations[1]["name"] == "renamed"


async def test_cancel_deletes_and_ends_one_under_way_first(services, fake, clock):
    w = window(clock)
    await services.reservations.book(7, w)
    await services.reservations.book(8, window(clock, days=6))
    at(clock, fake, w.starts_at + timedelta(minutes=5))
    await services.reservations.cancel(7)
    await services.reservations.cancel(8)
    assert fake.reservations == {}
    assert (await services.reservations.get(7)).state == "cancelled"


async def test_cancel_with_the_supervisor_down_is_finished_later(services, fake, clock):
    await services.reservations.book(7, window(clock))
    url = services.supervisor.url
    services.supervisor.url = "http://127.0.0.1:9"
    await services.reservations.cancel(7)
    assert (await services.reservations.get(7)).state == "cancelling" and 1 in fake.reservations
    services.supervisor.url = url
    at(clock, fake, clock.now + timedelta(minutes=2))
    await services.scheduler.run_due()
    assert (await services.reservations.get(7)).state == "cancelled" and fake.reservations == {}


@pytest.mark.parametrize("by_id", [True, False])
async def test_ending_only_ends_this_events_reservation(services, fake, clock, by_id):
    if not by_id:
        fake.capabilities = [c for c in fake.capabilities if c != "reservation_end_by_id"]
    w = window(clock)
    await services.reservations.book(7, w)
    assert await services.reservations.end(7) is False  # not under way yet
    at(clock, fake, w.starts_at + timedelta(hours=1))
    assert await services.reservations.end(7) is True
    assert fake.reservations[1]["ended"] and (await services.reservations.get(7)).state == "ended"


async def test_no_event_server_configured(services, fake, clock, settings):
    services.store.current = settings.model_copy(update={"supervisor": settings.supervisor.model_copy(
        update={"event_server": None})})
    row = await services.reservations.book(7, window(clock))
    assert row.state == "failed" and "BOT_SUPERVISOR__EVENT_SERVER" in row.error


async def test_a_reservation_deleted_in_the_console_is_made_again(services, fake, clock):
    await services.reservations.book(7, window(clock))
    fake.reservations.clear()
    row = await services.reservations.book(7, window(clock, days=4))
    assert row.state == "booked" and row.reservation_id == 2
