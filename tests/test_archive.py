"""Phase 8: the long-term archive (docs/PLAN.md, *Long-term archive*), on the fakes."""

import asyncio
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import ADMIN, MEMBER
from fakes import FakeAttachment, FakeMessage, invoke
from sqlalchemy import select
from test_attendance import asking, at, hold, person, published, run, stack  # noqa: F401

from app.archive.cog import ArchiveCog
from app.archive.export import Exporter
from app.archive.service import ArchiveService, quarter, quarter_name
from app.db.models import ArchivedMessage
from app.privacy.cog import ConfirmErase

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 8000  # inlined about 11 kB, zipped far less


@pytest.fixture
async def archive(stack, guild):
    stack.archive = ArchiveService(stack.db, stack.store, stack.scheduler.clock)
    stack.exporter = Exporter(stack, lambda: guild)
    await stack.exporter.start()
    return stack.archive


async def archived_event(stack, guild, clock):
    """A published event with a slot holder, archived, with two members' messages (one with a picture)."""
    anna = person(guild, 1, MEMBER)
    event = await published(stack, clock)
    await hold(stack, event, 1)
    await stack.channels.archive(event.id)
    event = await stack.events.get(event.id)
    channel = guild.get_channel(event.channel_id)
    channel.posts.append(FakeMessage(anna, "Ready!", created_at=event.start_at))
    thread = guild.threads[event.thread_id]
    thread.posts.append(FakeMessage(anna, "Map attached", [FakeAttachment("map.png", PNG)],
                                    created_at=event.start_at + timedelta(minutes=1)))
    return event


async def notes(stack) -> list[str]:
    await asyncio.sleep(0)
    return stack.staff.drain()


def test_quarters_are_numbered_and_named(settings):
    tz = settings.tz
    assert quarter(datetime(2026, 11, 2, tzinfo=UTC), tz) - quarter(datetime(2025, 12, 31, 12, tzinfo=UTC), tz) == 4
    assert quarter_name(2026 * 4 + 3, "Archiwum Q{quarter} {year}") == "Archiwum Q4 2026"


async def test_the_oldest_quarter_moves_into_the_database_and_its_channel_goes(stack, archive, guild, clock, fake):
    event = await archived_event(stack, guild, clock)
    category = guild.get_channel(event.channel_id).category
    assert category.name == "Archiwum Q4 2026"
    at(clock, fake, clock.now + timedelta(days=200))  # two quarters later: it stays
    await run(stack)
    assert guild.get_channel(event.channel_id) is not None
    at(clock, fake, datetime(2027, 11, 3, 5, 0, tzinfo=UTC))  # Q4 2027: four quarters kept, Q4 2026 leaves
    await run(stack)
    moved = await stack.events.get(event.id)
    assert moved.exported_at is not None and moved.channel_id is None
    assert guild.get_channel(event.channel_id) is None and guild.get_channel(category.id) is None  # emptied: gone
    messages = await archive.messages(event.id)
    assert [m.content for m in messages] == ["Ready!", "Map attached"]  # the bot's own messages aren't copied
    (picture,) = messages[1].attachments
    assert Path(picture["path"]).read_bytes() == PNG and picture["path"].endswith(f"{messages[1].message_id}-map.png")
    assert "1 event przeniesiony do archiwum w bazie." in await notes(stack)


async def test_a_copy_that_doesnt_match_leaves_the_channel(stack, archive, guild, clock, fake):
    event = await archived_event(stack, guild, clock)

    async def short_read():
        return b"cut"

    broken = SimpleNamespace(filename="broken.png", size=999, read=short_read)
    guild.get_channel(event.channel_id).posts.append(FakeMessage(guild.members[1], "x", [broken]))
    at(clock, fake, datetime(2027, 11, 3, 5, 0, tzinfo=UTC))
    await run(stack)
    assert guild.get_channel(event.channel_id) is not None
    assert (await stack.events.get(event.id)).exported_at is None
    assert any("the copy doesn't match" in n for n in await notes(stack))


async def test_archive_event_reads_it_back_for_participants_in_the_size_that_fits(stack, archive, guild, clock, fake):
    event = await archived_event(stack, guild, clock)
    at(clock, fake, datetime(2027, 11, 3, 5, 0, tzinfo=UTC))
    await run(stack)
    cog = ArchiveCog(stack)
    anna = asking(stack, guild, guild.members[1])
    choices = await cog.event_choices(anna, "operation")
    assert [c.value for c in choices] == [str(event.id)]
    await invoke(cog.event, cog, anna, event=str(event.id))
    sent = anna.replies[0]
    assert sent.content == "Operation X, from the archive." and sent.kwargs["file"].filename == "operation-x.html"
    html = sent.kwargs["file"].fp.read().decode("utf-8")
    assert "Ready!" in html and "data:image/png;base64," in html and "m1" in html  # the holder by name

    stranger = asking(stack, guild, person(guild, 7, MEMBER))
    assert await cog.event_choices(stranger, "") == []
    await invoke(cog.event, cog, stranger, event=str(event.id))
    assert stranger.replies[0].content.startswith("No archived event of yours")
    admin = asking(stack, guild, person(guild, 30, ADMIN))
    assert [c.value for c in await cog.event_choices(admin, "")] == [str(event.id)]

    guild.filesize_limit = 4000  # the inlined picture doesn't fit, the ZIP does
    zipped = asking(stack, guild, guild.members[1])
    await invoke(cog.event, cog, zipped, event=str(event.id))
    packed = zipfile.ZipFile(BytesIO(zipped.replies[0].kwargs["file"].fp.read()))
    assert sorted(packed.namelist())[0].startswith("attachments/") and "index.html" in packed.namelist()
    guild.filesize_limit = 300  # nothing with the files fits: the names only
    listed = asking(stack, guild, guild.members[1])
    await invoke(cog.event, cog, listed, event=str(event.id))
    html = listed.replies[0].kwargs["file"].fp.read().decode("utf-8")
    assert "map.png (kept on the host)" in html


async def test_an_erasure_empties_the_members_archived_messages(stack, archive, guild, clock, fake):
    event = await archived_event(stack, guild, clock)
    at(clock, fake, datetime(2027, 11, 3, 5, 0, tzinfo=UTC))
    await run(stack)
    picture = Path((await archive.messages(event.id))[1].attachments[0]["path"])
    view = ConfirmErase(stack, 1, None)
    await view._erase(asking(stack, guild, person(guild, 30, ADMIN)))
    async with stack.db.session() as session:
        rows = (await session.execute(select(ArchivedMessage))).scalars().all()
    assert {(r.author_id, r.author_name, r.content) for r in rows} == {(-1, "", "")}
    assert not picture.exists()
