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
