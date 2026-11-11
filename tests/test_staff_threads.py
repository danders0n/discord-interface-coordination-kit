from conftest import STAFF
from fakes import FakeChannel

from app.db import state


async def test_the_thread_is_made_once_and_reused(services, guild):
    staff = guild.get_channel(STAFF)
    first = await services.threads.get(guild, staff, "staff_log")
    assert first.name == "Staff log" and first.parent_id == STAFF
    again = await services.threads.get(guild, staff, "staff_log")
    assert again is first and len(guild.threads) == 1
    moderation = await services.threads.get(guild, staff, "moderation")
    assert moderation.name == "Moderacja" and len(guild.threads) == 2
    assert (await state.get(services.db, "thread:staff_log")) == {"id": first.id}


async def test_an_archived_thread_is_fetched_and_reopened(services, guild):
    staff = guild.get_channel(STAFF)
    thread = await services.threads.get(guild, staff, "staff_log")
    thread.archived = True  # Discord archives it after a quiet week; it leaves the cache
    await services.threads.send(guild, staff, "staff_log", "note")
    assert thread.archived is False and thread.sent[-1].content == "note"
    assert len(guild.threads) == 1


async def test_a_deleted_thread_is_made_again(services, guild):
    staff = guild.get_channel(STAFF)
    thread = await services.threads.get(guild, staff, "moderation")
    del guild.threads[thread.id]
    fresh = await services.threads.get(guild, staff, "moderation")
    assert fresh.id != thread.id
    assert (await state.get(services.db, "thread:moderation")) == {"id": fresh.id}


async def test_a_thread_the_database_doesnt_know_is_taken_over(services, guild):
    staff = guild.get_channel(STAFF)
    older = await staff.create_thread(name="Staff log", type=None, auto_archive_duration=10080)  # other data's
    await staff.create_thread(name="Staff log", type=None, auto_archive_duration=10080)
    theirs = await staff.create_thread(name="Moderacja", type=None, auto_archive_duration=10080)
    theirs.owner_id = 1  # a member's thread of the same name stays theirs
    older.archived = True
    found = await services.threads.get(guild, staff, "staff_log")
    assert found.id != older.id and len(guild.threads) == 3  # an open one first
    assert (await state.get(services.db, "thread:staff_log")) == {"id": found.id}
    for thread in guild.threads.values():
        thread.archived = thread.name == "Staff log"
    await state.put(services.db, "thread:staff_log", {})
    assert (await services.threads.get(guild, staff, "staff_log")).id == older.id  # the oldest archived one
    moderation = await services.threads.get(guild, staff, "moderation")
    assert moderation.id != theirs.id and len(guild.threads) == 4


async def test_a_new_staff_channel_or_name_from_env(services, guild, settings):
    staff = guild.get_channel(STAFF)
    thread = await services.threads.get(guild, staff, "staff_log")
    services.store.current = settings.model_copy(update={"channels": settings.channels.model_copy(
        update={"staff_log_thread": "Bot log"})})
    renamed = await services.threads.get(guild, staff, "staff_log")
    assert renamed is thread and thread.name == "Bot log"
    other = FakeChannel(777, "staff-2")
    guild.add(other)
    moved = await services.threads.get(guild, other, "staff_log")
    assert moved.parent_id == 777 and moved.id != thread.id
