"""Phase 6c: `/privacy-erase` (docs/PLAN.md, *Privacy*, *Erasure*), on the fakes."""

from datetime import timedelta

from conftest import ADMIN, MEMBER
from fakes import invoke
from sqlalchemy import select
from test_attendance import ANNA, asking, at, hold, linked, person, play, published, run, stack  # noqa: F401

from app.db.models import AttendanceEntry, Event, MissionRating, Slot
from app.privacy.cog import PrivacyCog
from app.privacy.erase import pseudonym


async def test_an_erasure_replaces_the_member_everywhere_and_frees_coming_places(stack, guild, clock, fake):
    anna = person(guild, 1, MEMBER)
    person(guild, 2, MEMBER)
    past = await published(stack, clock)
    await hold(stack, past, 1)
    linked(fake, 1, ANNA, "Anna")
    fake.plays = [play(1, past.start_at + timedelta(minutes=5), 120, players=[(ANNA, "Anna", 5000)])]
    at(clock, fake, past.start_at + timedelta(minutes=140))
    await run(stack)
    await stack.feedback.rate(await stack.events.get(past.id), 1, "4", "Loved it")
    coming = await published(stack, clock)
    await hold(stack, coming, 1)

    cog = PrivacyCog(stack)
    member = asking(stack, guild, anna)
    await invoke(cog.erase, cog, member, member=anna)
    assert member.replies[0].content == "You don't have the permission for this."
    admin = asking(stack, guild, person(guild, 30, ADMIN))
    await invoke(cog.erase, cog, admin, member=anna)
    question = admin.replies[0]
    assert "can't be undone" in question.content
    confirmed = asking(stack, guild, guild.members[30])
    await question.kwargs["view"].children[0].callback(confirmed)
    assert confirmed.response.edited[0]["content"] == "Erased: the member is now former:1."
    await run(stack)

    async with stack.db.session() as session:
        entry = (await session.execute(select(AttendanceEntry).where(AttendanceEntry.event_id == past.id))).scalar_one()
        rating = (await session.execute(select(MissionRating))).scalar_one()
        coming_slots = (await session.execute(select(Slot.holder_id).where(Slot.event_id == coming.id))).scalars().all()
        past_slots = (await session.execute(select(Slot.holder_id).where(Slot.event_id == past.id))).scalars().all()
        events = (await session.execute(select(Event.created_by))).scalars().all()
    assert (entry.member_id, entry.steam_id, entry.name) == (-1, "former:1", "")
    assert (rating.member_id, rating.comment, rating.stars) == (-1, "", 4)
    assert set(coming_slots) == {None}  # a coming event: the place is freed
    assert -1 in past_slots and pseudonym(-1) == "former:1"
    assert set(events) == {20}  # (the creator wasn't erased)
    report = guild.get_channel(past.channel_id).messages[-2]
    fields = {f.name: f.value for f in (report.edits[-1] if report.edits else report.kwargs)["embeds"][0].fields}
    assert fields["Obecni (1)"] == "former:1"
    notes = stack.staff.drain()
    assert any("former:1" in n for n in notes) and not any("<@1>" in n for n in notes)

    second = asking(stack, guild, guild.members[30])
    await invoke(cog.erase, cog, second, member=guild.members[2])
    again = asking(stack, guild, guild.members[30])
    await second.replies[0].kwargs["view"].children[0].callback(again)
    assert again.response.edited[0]["content"] == "Erased: the member is now former:2."
