"""`/privacy-erase` (docs/PLAN.md, *Privacy*, *Erasure*; docs/PLAN-front-end.md, phase 5): the erasure runs in the
supervisor in the staff member's name, and its ``erased`` notice empties what the bot holds; on the fakes."""

import discord
import pytest
from conftest import ADMIN, MEMBER
from fakes import FakeInteraction, FakeMember, FakeRole, invoke

from app.discord_util import on_view_error
from app.links.service import LinkService
from app.notices.deliver import Delivery
from app.notices.service import Notices
from app.privacy.cog import PrivacyCog
from app.privacy.erase import pseudonym
from app.roster.service import Roster

ENGLISH = discord.Locale.british_english


@pytest.fixture
async def erasing(services, fake, guild):
    services.roster = Roster(services.supervisor, services.scheduler, lambda: None, lambda: None, lambda _id: None)
    services.notices = Notices(services.db, services.supervisor, services.scheduler.clock)
    Delivery(services, lambda: guild).install(services.notices)
    services.links = LinkService(services.db, services.supervisor, services.roster.as_member, services.scheduler.clock)
    fake.person(1, "anna")
    fake.person(30, "admin", permissions=["users.manage"])
    fake.person(31, "mod", permissions=[])
    yield services


def asking(services, who) -> FakeInteraction:
    return FakeInteraction(services, who, ENGLISH)


async def confirm(services, cog, who, target) -> FakeInteraction:
    """``/privacy-erase target`` as ``who``, then *Erase* (its error handler on a refusal)."""
    asked = asking(services, who)
    await invoke(cog.erase, cog, asked, member=target)
    question = asked.replies[0]
    assert "can't be undone" in question.content
    pressed = asking(services, who)
    button = question.kwargs["view"].children[0]
    try:
        await button.callback(pressed)
    except Exception as exc:  # noqa: BLE001
        await on_view_error(None, pressed, exc, button)
    return pressed


def answer(interaction) -> str:
    return interaction.original_edits[-1]["content"] if interaction.original_edits else interaction.replies[-1].content


async def test_an_erasure_runs_in_the_app_and_its_notice_empties_the_bots_data(erasing, fake):
    services = erasing
    await services.links.reminded([1])
    cog = PrivacyCog(services)
    admin = FakeMember(30, "admin", roles=[FakeRole(ADMIN)])
    done = await confirm(services, cog, admin, FakeMember(1, "anna", roles=[FakeRole(MEMBER)]))
    assert answer(done) == "Erased: the member is now former:1."
    assert fake.persons["1"]["erased"] == "former:1"
    await services.notices.run_once()  # the erased notice
    assert 1 not in await services.links.reminders() and pseudonym(-1) == "former:1"
    assert fake.notices[-1]["state"] == "delivered"
    notes = services.staff.drain()
    assert any("former:1" in n for n in notes) and not any("<@1>" in n for n in notes)


async def test_the_app_decides_who_may_erase(erasing):
    cog = PrivacyCog(erasing)
    refused = await confirm(erasing, cog, FakeMember(31, "mod"), FakeMember(1, "anna"))
    assert answer(refused) == "You don't have the permission for this."


async def test_a_member_the_app_never_knew_loses_only_the_bots_data_and_only_for_bot_admin(erasing):
    services = erasing
    await services.links.reminded([7])
    cog = PrivacyCog(services)
    refused = await confirm(services, cog, FakeMember(31, "mod"), FakeMember(7, "ghost"))
    assert answer(refused) == "You don't have the permission for this."
    done = await confirm(services, cog, FakeMember(30, "admin", roles=[FakeRole(ADMIN)]), FakeMember(7, "ghost"))
    assert answer(done) == "Erased: the member is now former:1."
    assert 7 not in await services.links.reminders()
