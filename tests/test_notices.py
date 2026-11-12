"""Delivering the supervisor's notices (docs/PLAN-front-end.md, *Notices*) against the fake supervisor."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fakes import FakeMember

from app.db import state
from app.db.models import NoticeDelivery
from app.notices.deliver import Delivery
from app.notices.service import CURSOR, Later, Notices, delivered

ANNA = 11


def make(services, guild) -> Notices:
    notices = Notices(services.db, services.supervisor, services.scheduler.clock)
    Delivery(services, lambda: guild).install(notices)
    return notices


async def cursor(services) -> int:
    return (await state.get(services.db, CURSOR) or {}).get("after", 0)


async def test_a_login_link_is_claimed_and_sent_by_dm(services, fake, guild):
    guild.members[ANNA] = member = FakeMember(ANNA, "anna")
    fake.person(ANNA, "anna")
    notice = fake.login_link(ANNA, url="https://app.example/ui/#/set-password/abc")
    assert await make(services, guild).run_once() == 1
    [dm] = member.dms
    assert "https://app.example/ui/#/set-password/abc" in dm.content and "<t:" in dm.content
    assert notice["state"] == "delivered" and fake.link_urls == {}
    assert await cursor(services) == notice["id"]


async def test_closed_dms_are_reported_so_staff_see_the_link_in_the_app(services, fake, guild):
    guild.members[ANNA] = FakeMember(ANNA, "anna", dms_closed=True)
    notice = fake.login_link(ANNA)
    await make(services, guild).run_once()
    assert (notice["state"], notice["reason"]) == ("failed", "dm_closed")


async def test_a_member_not_on_the_server_gets_no_link(services, fake, guild):
    notice = fake.login_link(ANNA)
    await make(services, guild).run_once()
    assert (notice["state"], notice["reason"]) == ("failed", "not_in_guild")
    assert notice["id"] in fake.link_urls  # never claimed


async def test_a_link_claimed_already_is_a_failure(services, fake, guild):
    guild.members[ANNA] = member = FakeMember(ANNA, "anna")
    notice = fake.login_link(ANNA)
    fake.link_urls.clear()
    await make(services, guild).run_once()
    assert notice["state"] == "failed" and notice["reason"].startswith("claim:")
    assert member.dms == []


async def test_a_notice_cut_off_by_a_crash_is_never_said_twice(services, fake, guild):
    guild.members[ANNA] = member = FakeMember(ANNA, "anna")
    notice = fake.login_link(ANNA)
    async with services.db.session() as session, session.begin():
        session.add(NoticeDelivery(notice_id=notice["id"], kind="login_link", state="started",
                                   started_at=services.scheduler.clock()))
    await make(services, guild).run_once()
    assert (notice["state"], notice["reason"]) == ("failed", "bot_crashed")
    assert member.dms == []


async def test_an_outcome_the_supervisor_missed_is_reported_without_saying_it_again(services, fake, guild):
    guild.members[ANNA] = member = FakeMember(ANNA, "anna")
    first, second = fake.login_link(ANNA), fake.login_link(ANNA)
    notices = make(services, guild)
    real = services.supervisor.notice_done

    async def down(*_args, **_kwargs):
        from app.supervisor_client import SupervisorUnreachable
        raise SupervisorUnreachable("down")

    services.supervisor.notice_done = down
    assert await notices.run_once() == 0  # said, but not reported: the batch stops there
    assert len(member.dms) == 1 and first["state"] == "new" and await cursor(services) == 0
    services.supervisor.notice_done = real
    assert await notices.run_once() == 2
    assert len(member.dms) == 2  # the first once, the second once
    assert first["state"] == second["state"] == "delivered"


async def test_an_expired_notice_is_not_said(services, fake, guild, clock):
    guild.members[ANNA] = member = FakeMember(ANNA, "anna")
    notice = fake.notice("login_link", persons=[ANNA], expires_at=clock.now + timedelta(minutes=1))
    clock.now += timedelta(minutes=2)  # (the bot's clock is past it; the fake's isn't)
    await make(services, guild).run_once()
    assert (notice["state"], notice["reason"]) == ("failed", "expired") and member.dms == []


async def test_a_kind_without_a_handler_is_reported_unsupported(services, fake, guild):
    notice = fake.notice("something_new")
    await make(services, guild).run_once()
    assert (notice["state"], notice["reason"]) == ("failed", "unsupported")


async def test_a_held_roster_push_is_a_staff_note(services, fake, guild):
    notice = fake.notice("roster_guard", data={"push_id": 3, "count": 4, "accounts": ["a", "b", "c", "d"]})
    await make(services, guild).run_once()
    await asyncio.sleep(0)
    [note] = services.staff.drain()
    assert "4 konta" in note and "a, b, c, d" in note  # (the configured language: Polish, the "few" form)
    assert notice["state"] == "delivered"


async def test_a_handler_that_must_wait_leaves_the_notice_for_later(services, fake, guild):
    notice = fake.notice("roster_guard")
    notices = make(services, guild)
    calls = []

    async def not_yet(_notice):
        calls.append(1)
        if len(calls) == 1:
            raise Later("no guild")
        return delivered()

    notices.register("roster_guard", not_yet)
    assert await notices.run_once() == 0
    assert notice["state"] == "new"
    async with services.db.session() as session:
        assert await session.get(NoticeDelivery, notice["id"]) is None
    assert await notices.run_once() == 1 and notice["state"] == "delivered"


async def test_a_failing_handler_is_a_failed_delivery_and_the_rest_go_on(services, fake, guild):
    broken, fine = fake.notice("roster_guard"), fake.notice("something_new")
    notices = make(services, guild)

    async def boom(_notice):
        raise RuntimeError("bad data")

    notices.register("roster_guard", boom)
    assert await notices.run_once() == 2
    assert (broken["state"], broken["reason"]) == ("failed", "error: bad data")
    assert fine["state"] == "failed"
