"""The long-term archive (docs/PLAN.md, *Long-term archive*), on the fakes: operations from the fake supervisor,
archived by the bot, moved into the database and read back."""

import asyncio
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from conftest import ADMIN
from fakes import FakeAttachment, FakeInteraction, FakeMember, FakeMessage, FakeRole, invoke
from sqlalchemy import select
from test_channel import at, row, run, stack  # noqa: F401

from app.archive.cog import ArchiveCog
from app.archive.export import Exporter
from app.archive.service import ArchiveService, quarter, quarter_name
from app.db.models import ArchivedMessage
from app.notices.deliver import Delivery

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8000  # inlined about 11 kB, zipped far less
ENGLISH = discord.Locale.british_english


@pytest.fixture
async def archive(stack, guild):
    stack.archive = ArchiveService(stack.db, stack.store, stack.scheduler.clock)
    stack.exporter = Exporter(stack, lambda: guild)
    await stack.exporter.start()
    return stack.archive


def member(guild, member_id: int, *roles: int) -> FakeMember:
    found = FakeMember(member_id, f"m{member_id}", roles=[FakeRole(r) for r in roles])
    guild.members[member_id] = found
    return found


def asking(stack, guild, who) -> FakeInteraction:
    return FakeInteraction(stack, who, ENGLISH, guild=guild)


async def archived_operation(stack, guild, fake, clock):
    """A published operation organised by Anna (person 1), archived, with her messages (one with a picture)."""
    anna = member(guild, 1)
    op = fake.operation("Operation X", status="published", author_person_id=1,
                        opord={"situation": "Enemy north.", "mission": "Take the bridge."})
    await run(stack)
    await stack.channels.archive(op["id"])
    drawn = await row(stack, op["id"])
    guild.get_channel(drawn.channel_id).posts.append(FakeMessage(anna, "Ready!", created_at=drawn.start_at))
    guild.threads[drawn.thread_id].posts.append(FakeMessage(anna, "Map attached", [FakeAttachment("map.png", PNG)],
                                                            created_at=drawn.start_at + timedelta(minutes=1)))
    return drawn


async def notes(stack) -> list[str]:
    await asyncio.sleep(0)
    return stack.staff.drain()


def test_quarters_are_numbered_and_named(settings):
    tz = settings.tz
    assert quarter(datetime(2026, 11, 2, tzinfo=UTC), tz) - quarter(datetime(2025, 12, 31, 12, tzinfo=UTC), tz) == 4
    assert quarter_name(2026 * 4 + 3, "Archiwum Q{quarter} {year}") == "Archiwum Q4 2026"


async def test_the_oldest_quarter_moves_into_the_database_and_its_channel_goes(stack, archive, guild, clock, fake):
    drawn = await archived_operation(stack, guild, fake, clock)
    category = guild.get_channel(drawn.channel_id).category
    assert category.name == "Archiwum Q4 2026"
    at(clock, fake, clock.now + timedelta(days=200))  # two quarters later: it stays
    await run(stack)
    assert guild.get_channel(drawn.channel_id) is not None
    at(clock, fake, datetime(2027, 11, 3, 5, 0, tzinfo=UTC))  # Q4 2027: four quarters kept, Q4 2026 leaves
    await run(stack)
    moved = await row(stack, drawn.operation_id)
    assert moved.exported_at is not None and moved.channel_id is None
    assert guild.get_channel(drawn.channel_id) is None and guild.get_channel(category.id) is None  # emptied: gone
    messages = await archive.messages(drawn.operation_id)
    assert [m.content for m in messages] == ["Ready!", "Map attached"]  # the bot's own messages aren't copied
    (picture,) = messages[1].attachments
    assert Path(picture["path"]).read_bytes() == PNG and picture["path"].endswith(f"{messages[1].message_id}-map.png")
    assert "1 operacja przeniesiona do archiwum w bazie." in await notes(stack)


async def test_a_copy_that_doesnt_match_leaves_the_channel(stack, archive, guild, clock, fake):
    drawn = await archived_operation(stack, guild, fake, clock)

    async def short_read():
        return b"cut"

    broken = SimpleNamespace(filename="broken.png", size=999, read=short_read)
    guild.get_channel(drawn.channel_id).posts.append(FakeMessage(guild.members[1], "x", [broken]))
    at(clock, fake, datetime(2027, 11, 3, 5, 0, tzinfo=UTC))
    await run(stack)
    assert guild.get_channel(drawn.channel_id) is not None
    assert (await row(stack, drawn.operation_id)).exported_at is None
    assert any("the copy doesn't match" in n for n in await notes(stack))


async def test_archive_event_reads_it_back_for_organisers_in_the_size_that_fits(stack, archive, guild, clock, fake):
    drawn = await archived_operation(stack, guild, fake, clock)
    op_id = drawn.operation_id
    at(clock, fake, datetime(2027, 11, 3, 5, 0, tzinfo=UTC))
    await run(stack)
    cog = ArchiveCog(stack)
    anna = asking(stack, guild, guild.members[1])
    choices = await cog.event_choices(anna, "operation")
    assert [c.value for c in choices] == [str(op_id)]
    await invoke(cog.event, cog, anna, event=str(op_id))
    sent = anna.replies[0]
    assert sent.content == "Operation X, from the archive." and sent.kwargs["file"].filename == "operation-x.html"
    html = sent.kwargs["file"].fp.read().decode("utf-8")
    assert "Ready!" in html and "data:image/png;base64," in html and "Take the bridge." in html

    stranger = asking(stack, guild, member(guild, 7))
    assert await cog.event_choices(stranger, "") == []
    await invoke(cog.event, cog, stranger, event=str(op_id))
    assert stranger.replies[0].content.startswith("No archived operation of yours")
    admin = asking(stack, guild, member(guild, 30, ADMIN))
    assert [c.value for c in await cog.event_choices(admin, "")] == [str(op_id)]

    guild.filesize_limit = 4000  # the inlined picture doesn't fit, the ZIP does
    zipped = asking(stack, guild, guild.members[1])
    await invoke(cog.event, cog, zipped, event=str(op_id))
    packed = zipfile.ZipFile(BytesIO(zipped.replies[0].kwargs["file"].fp.read()))
    assert sorted(packed.namelist())[0].startswith("attachments/") and "index.html" in packed.namelist()
    guild.filesize_limit = 300  # nothing with the files fits: the names only
    listed = asking(stack, guild, guild.members[1])
    await invoke(cog.event, cog, listed, event=str(op_id))
    html = listed.replies[0].kwargs["file"].fp.read().decode("utf-8")
    assert "map.png (kept on the host)" in html


async def test_an_erasure_empties_the_members_archived_messages(stack, archive, guild, clock, fake):
    drawn = await archived_operation(stack, guild, fake, clock)
    at(clock, fake, datetime(2027, 11, 3, 5, 0, tzinfo=UTC))
    await run(stack)
    picture = Path((await archive.messages(drawn.operation_id))[1].attachments[0]["path"])
    erased = await Delivery(stack, lambda: guild).erased({"data": {"discord_id": "1", "former": "former:4"}})
    assert erased == ("delivered", None)
    async with stack.db.session() as session:
        rows = (await session.execute(select(ArchivedMessage))).scalars().all()
    assert {(r.author_id, r.author_name, r.content) for r in rows} == {(-4, "", "")}
    assert not picture.exists()
