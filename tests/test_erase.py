"""`/privacy-erase` (docs/PLAN.md, *Privacy*, *Erasure*): what the bot itself holds of a member, on the fakes."""

import discord
from conftest import ADMIN, MEMBER
from fakes import FakeInteraction, FakeMember, FakeRole, invoke

from app.links.service import LinkService
from app.privacy.cog import PrivacyCog
from app.privacy.erase import pseudonym

ENGLISH = discord.Locale.british_english


def asking(services, who) -> FakeInteraction:
    return FakeInteraction(services, who, ENGLISH)


async def test_an_erasure_asks_first_and_numbers_the_pseudonyms(services):
    anna = FakeMember(1, "anna", roles=[FakeRole(MEMBER)])
    admin = FakeMember(30, "admin", roles=[FakeRole(ADMIN)])
    links = LinkService(services.db, services.supervisor, lambda *_: None, services.scheduler.clock)
    await links.reminded([1])
    cog = PrivacyCog(services)
    member = asking(services, anna)
    await invoke(cog.erase, cog, member, member=anna)
    assert member.replies[0].content == "You don't have the permission for this."
    staff = asking(services, admin)
    await invoke(cog.erase, cog, staff, member=anna)
    question = staff.replies[0]
    assert "can't be undone" in question.content and "erase them there" in question.content
    confirmed = asking(services, admin)
    await question.kwargs["view"].children[0].callback(confirmed)
    assert confirmed.response.edited[0]["content"] == "Erased: the member is now former:1."
    assert 1 not in await links.reminders() and pseudonym(-1) == "former:1"
    notes = services.staff.drain()
    assert any("former:1" in n for n in notes) and not any("<@1>" in n for n in notes)

    second = asking(services, admin)
    await invoke(cog.erase, cog, second, member=FakeMember(2, "bob"))
    again = asking(services, admin)
    await second.replies[0].kwargs["view"].children[0].callback(again)
    assert again.response.edited[0]["content"] == "Erased: the member is now former:2."
