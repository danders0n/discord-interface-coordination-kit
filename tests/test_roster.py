"""The roster push (docs/PLAN-front-end.md, *Roster*) against the fake supervisor."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.bot import roster_member, roster_role
from app.db.models import ScheduledAction
from app.roster.service import DAILY, FULL, GUARDED, MEMBER, NOT_READY, PUSHED, RETRY_FULL, UNREACHABLE, Member, Role, Roster
from app.supervisor_client import SupervisorError

ANNA, BOB, CEZ = Member(11, "anna", "Anna", (301,)), Member(12, "bob", "Bob"), Member(13, "cez", "Cez", (301, 302))
ROLES = [Role(301, "Klanowicz", 5, 0x112233), Role(302, "Mission Maker", 4)]


class Guild:
    """What the bot hands the roster: the members and roles now."""

    def __init__(self, members: list[Member] | None, roles: list[Role] | None = ROLES) -> None:
        self.people, self.role_list = members, roles

    def members(self):
        return self.people

    def roles(self):
        return self.role_list

    def member(self, discord_id: int) -> Member | None:
        return next((m for m in self.people or [] if m.discord_id == discord_id), None)


def make(services, guild: Guild) -> Roster:
    return Roster(services.supervisor, services.scheduler, guild.members, guild.roles, guild.member)


async def actions(services, kind: str) -> list[ScheduledAction]:
    async with services.db.session() as session:
        return list((await session.execute(select(ScheduledAction).where(ScheduledAction.kind == kind))).scalars())


async def test_the_full_push_sends_roles_then_members(services, fake):
    roster = make(services, Guild([ANNA, BOB]))
    assert await roster.push_all() == PUSHED
    assert [r["id"] for r in fake.roles] == ["301", "302"] and fake.roles[0]["color"] == 0x112233
    assert fake.persons["11"]["roles"] == ["301"] and fake.persons["12"]["display_name"] == "Bob"
    pushes = [c for c in fake.calls if c[0] == "PUT"]
    assert pushes == [("PUT", "/api/members/roles"), ("PUT", "/api/members/roster")]


async def test_a_member_missing_from_the_full_push_has_left(services, fake):
    fake.person(13, "cez")
    await make(services, Guild([ANNA])).push_all()
    assert fake.persons["13"]["in_guild"] is False and fake.persons["11"]["in_guild"] is True


async def test_start_pushes_at_once_then_daily(services, fake, clock):
    roster = make(services, Guild([ANNA]))
    await roster.start()
    await services.scheduler.run_due()
    assert "11" in fake.persons
    [action] = await actions(services, FULL)
    assert action.due_at == clock.now + DAILY and action.done_at is None


async def test_a_guarded_push_changes_nothing_and_waits_for_the_next_day(services, fake, clock):
    fake.roster_guard = 1
    fake.person(12, "bob")
    fake.person(13, "cez")
    roster = make(services, Guild([ANNA]))
    assert await roster.push_all() == GUARDED
    assert "11" not in fake.persons and fake.persons["13"]["in_guild"]  # nothing applied
    assert [n["kind"] for n in fake.notices] == ["roster_guard"]  # the supervisor's notice tells staff
    await roster.start()
    await services.scheduler.run_due()
    [action] = await actions(services, FULL)
    assert action.due_at == clock.now + DAILY  # not retried sooner


async def test_full_push_retries_soon_when_down_or_not_ready(services, fake, clock):
    services.supervisor.url = "http://127.0.0.1:1"
    assert await make(services, Guild([ANNA])).push_all() == UNREACHABLE
    assert await make(services, Guild(None)).push_all() == NOT_READY
    roster = make(services, Guild([ANNA]))
    await roster.start()
    await services.scheduler.run_due()
    [action] = await actions(services, FULL)
    assert action.due_at == clock.now + RETRY_FULL


async def test_a_refused_full_push_is_an_error_for_the_scheduler(services, fake):
    fake.down = True
    with pytest.raises(SupervisorError) as caught:
        await make(services, Guild([ANNA])).push_all()
    assert caught.value.status == 503


async def test_one_member_is_pushed_at_once(services, fake):
    roster = make(services, Guild([ANNA]))
    await roster.member_changed(Member(11, "anna", "Anka", (301, 302)))
    assert fake.persons["11"]["display_name"] == "Anka" and fake.persons["11"]["roles"] == ["301", "302"]
    await roster.member_left(Member(11, "anna", "Anka"))
    assert fake.persons["11"]["in_guild"] is False


async def test_a_member_push_waits_while_the_supervisor_is_away(services, fake, clock):
    url = services.supervisor.url
    services.supervisor.url = "http://127.0.0.1:1"
    roster = make(services, Guild([ANNA]))
    await roster.member_changed(Member(11, "anna", "Anna", (301,)))
    await roster.member_changed(Member(11, "anna", "Anka", (301,)))  # the latest state replaces the queued one
    [queued] = await actions(services, MEMBER)
    assert queued.key == "member_push:11" and queued.payload["body"]["display_name"] == "Anka"
    services.supervisor.url = url
    clock.now += timedelta(minutes=5)
    await services.scheduler.run_due()
    assert fake.persons["11"]["display_name"] == "Anka"
    [done] = await actions(services, MEMBER)
    assert done.done_at is not None


async def test_a_push_that_gets_through_drops_the_queued_one(services, fake, clock):
    url = services.supervisor.url
    services.supervisor.url = "http://127.0.0.1:1"
    roster = make(services, Guild([ANNA]))
    await roster.member_changed(Member(11, "anna", "Old"))
    services.supervisor.url = url
    await roster.member_changed(Member(11, "anna", "New"))
    assert await actions(services, MEMBER) == []
    assert fake.persons["11"]["display_name"] == "New"


async def test_roles_are_pushed_again_when_they_change(services, fake):
    guild = Guild([ANNA])
    roster = make(services, guild)
    guild.role_list = [Role(301, "Członek")]
    await roster.roles_changed()
    assert fake.roles == [{"id": "301", "name": "Członek", "position": 0, "color": 0}]


async def test_an_unknown_member_is_pushed_and_the_call_repeated_once(services, fake):
    roster = make(services, Guild([ANNA]))
    me = await roster.as_member(11, lambda member: services.supervisor.me(act_as=member))
    assert me["discord_id"] == "11"
    assert fake.acted == [("GET", "/api/auth/me", "11")]
    assert [c for c in fake.calls if c[0] == "PUT"] == [("PUT", "/api/members/11")]


async def test_an_unknown_member_not_on_the_server_gets_the_refusal(services, fake):
    roster = make(services, Guild([ANNA]))
    with pytest.raises(SupervisorError) as caught:
        await roster.as_member(99, lambda member: services.supervisor.me(act_as=member))
    assert caught.value.code == "unknown_person"


async def test_a_disabled_account_is_not_retried(services, fake):
    fake.person(11, "anna", account={"username": "anna", "disabled": True})
    roster = make(services, Guild([ANNA]))
    with pytest.raises(SupervisorError) as caught:
        await roster.as_member(11, lambda member: services.supervisor.me(act_as=member))
    assert caught.value.code == "account_disabled" and caught.value.status == 403
    assert not [c for c in fake.calls if c[0] == "PUT"]


def test_discord_members_and_roles_become_roster_rows():
    role = SimpleNamespace(id=301, name="Klanowicz", position=3, color=SimpleNamespace(value=0xFF0000))
    everyone = SimpleNamespace(id=100, name="@everyone", position=0, color=SimpleNamespace(value=0))
    member = SimpleNamespace(id=11, name="anna", display_name="Anka", roles=[everyone, role])
    assert roster_member(member, 100) == Member(11, "anna", "Anka", (301,))
    assert roster_role(role).row() == {"id": "301", "name": "Klanowicz", "position": 3, "color": 0xFF0000}
