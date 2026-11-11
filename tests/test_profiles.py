"""Phase 7: the event profile on the supervisor (docs/PLAN.md, *Supervisor: event profile*), on the fakes."""

import asyncio
from datetime import timedelta

import pytest
from conftest import MEMBER, MISSION_MAKER
from fakes import FakeAttachment, invoke
from test_attendance import MAKER, asking, at, person, published, run, stack  # noqa: F401
from test_signup_ui import PRESET

from app.events.cog import EventCog
from app.events.service import Draft
from app.events.times import Times
from app.events.ui import ProfileSettings, ProfileTools, profile_panel
from app.profiles.service import CLEANUP, WARN_AFTER, ProfileError, ProfileService, Problem
from app.signup.service import Member


@pytest.fixture
async def profiles(stack):
    settings = stack.settings
    stack.store.current = settings.model_copy(update={"supervisor": settings.supervisor.model_copy(
        update={"base_profile": "IBC base"})})
    service = ProfileService(stack.db, stack.store, stack.scheduler, stack.supervisor, stack.events, stack.channels)
    stack.events.profiles = stack.profiles = service
    return service


async def upload(stack, guild, event, who, filename="op_x.Altis.pbo"):
    interaction = asking(stack, guild, who)
    interaction.channel = guild.get_channel(event.channel_id)
    await invoke(EventCog(stack).mission, EventCog(stack), interaction, file=FakeAttachment(filename, b"PBO"),
                 on_server=None)
    return interaction.replies[0].content


async def notes(stack) -> list[str]:
    await asyncio.sleep(0)
    return stack.staff.drain()


async def test_a_mission_is_uploaded_with_its_report_and_warnings(stack, profiles, guild, clock, fake):
    event = await published(stack, clock)  # three ORBAT slots
    member = person(guild, 10, MEMBER)
    await stack.events.add_organiser(event.id, MAKER, 10)  # an organiser, but no mission maker
    assert (await upload(stack, guild, event, member)).startswith("The server profile is for organisers")
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    answer = await upload(stack, guild, event, maker)
    assert answer == "Mission set: op_x.Altis.pbo (Altis, 10 playable)."
    fake.missions["tiny.Stratis.pbo"] = {"report": fake.mission_report("tiny.Stratis.pbo", playable=2) | {
        "requirements": [{"kind": "addon", "providers": [{"kind": "dlc", "key": "vn", "name": "S.O.G. Prairie Fire"},
                                                         {"kind": "mod", "key": "@ace", "name": "ACE3"}]}]}}
    picked = asking(stack, guild, maker)
    picked.channel = guild.get_channel(event.channel_id)
    await invoke(EventCog(stack).mission, EventCog(stack), picked, file=None, on_server="tiny.Stratis.pbo")
    text = picked.replies[0].content
    assert "S.O.G. Prairie Fire" in text and "there is no preset: ACE3" in text and "2/3" in text
    assert any("Misja Operation X (tiny.Stratis.pbo):" in n for n in await notes(stack))
    choices = await EventCog(stack).mission_choices(picked, "tiny")
    assert [c.value for c in choices] == ["tiny.Stratis.pbo"]
    assert await EventCog(stack).mission_choices(asking(stack, guild, member), "") == []  # not a mission maker


async def test_a_file_name_another_profile_uses_is_refused(stack, profiles, guild, clock, fake):
    fake.profiles[1]["config"]["server"] = {"missions": [{"template": "op_x.Altis"}]}  # the base uses it
    fake.missions["op_x.Altis.pbo"] = {"report": fake.mission_report("op_x.Altis.pbo")}
    event = await published(stack, clock)
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    assert (await upload(stack, guild, event, maker)).startswith("Another profile uses a mission of that file name")
    assert (await upload(stack, guild, event, maker, "op_x_v2.Altis.pbo")).startswith("Mission set")


async def test_building_clones_the_base_imports_the_preset_and_follows_the_job(stack, profiles, guild, clock, fake):
    event = await published(stack, clock)
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    tools = ProfileTools(asking(stack, guild, maker), event)
    refused = asking(stack, guild, maker)
    await tools._build(refused)
    assert refused.replies[0].content == "Set the mission first: /event mission."
    await stack.events.set_preset(event.id, MAKER, PRESET.encode())
    await upload(stack, guild, event, maker)
    built = asking(stack, guild, maker)
    await tools._build(built)
    assert built.replies[0].content == "Profile: Being built."
    event = await stack.events.get(event.id)
    profile = fake.profiles[event.profile_id]
    assert profile["name"] == f"EVT {await stack.events.channel_name(event)} Operation X"
    assert (profile["managed_by"], profile["external_ref"]) == ("discord-bot", f"event:{event.id}")
    assert profile["config"]["server"] == {"missions": [{"template": "op_x.Altis"}], "hostname": "IBC | Operation X",
                                           "verify_signatures": 2, "max_players": 10}
    assert profile["presets"] == [{"mode": "replace", "download": "missing", "file": f"{event.id}.html"}]
    assert all(r.get("profile_id") is None for r in fake.reservations.values())  # not before the mods are in
    fake.jobs[event.profile_job_id].update(status="running", progress={"items_total": 2, "items_pending": 1})
    await run(stack)
    content, _ = await profile_panel(asking(stack, guild, maker), await stack.events.get(event.id))
    assert "Downloading mods: 1 of 2." in content
    fake.jobs[event.profile_job_id]["status"] = "succeeded"
    at(clock, fake, clock.now + timedelta(seconds=31))
    await run(stack)
    assert (await stack.events.get(event.id)).profile_state == "built"
    assert [r["profile_id"] for r in fake.reservations.values()] == [event.profile_id]  # reservation_profile

    settings = asking(stack, guild, maker)
    modal = ProfileSettings(settings, await stack.events.get(event.id))
    modal.hostname._value, modal.players._value, modal.signatures._value = "IBC | Night ops", "40", "off"
    await modal.on_submit(settings)
    assert profile["config"]["server"]["hostname"] == "IBC | Night ops" and profile["config"]["server"]["max_players"] == 40
    assert profile["config"]["server"]["verify_signatures"] == 0 and len(profile["presets"]) == 2  # built again
    assert len(fake.profiles) == 2  # the same profile, not a second clone


async def test_a_failed_import_tells_the_organisers_and_staff(stack, profiles, guild, clock, fake):
    event = await published(stack, clock)
    await stack.events.set_preset(event.id, MAKER, PRESET.encode())
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    await upload(stack, guild, event, maker)
    await profiles.build(event.id, MAKER)
    event = await stack.events.get(event.id)
    fake.jobs[event.profile_job_id].update(status="failed", error="Steam login needed")
    await run(stack)
    assert (await stack.events.get(event.id)).profile_error == "Steam login needed"
    thread = guild.get_thread(event.thread_id)
    assert "Steam login needed" in thread.sent[-1].content and "<@20>" in thread.sent[-1].content
    assert any("Steam login needed" in n for n in await notes(stack))


async def test_without_reservation_profile_the_server_profile_is_swapped_and_given_back(stack, profiles, guild,
                                                                                         clock, fake):
    fake.capabilities.remove("reservation_profile")
    await stack.supervisor.refresh_info()
    fake.server("event")["server"]["assigned_profile"] = {"id": 1, "name": "IBC base"}
    event = await published(stack, clock)
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    await upload(stack, guild, event, maker)
    await profiles.build(event.id, MAKER)  # no preset: built at once
    await profiles.published(await stack.events.get(event.id))
    booked = await stack.reservations.get(event.id)
    event = await stack.events.get(event.id)
    at(clock, fake, booked.starts_at - timedelta(minutes=5))
    await run(stack)
    assert fake.server("event")["server"]["assigned_profile"]["id"] == event.profile_id
    assert (await stack.events.get(event.id)).previous_profile_id == 1
    at(clock, fake, booked.ends_at)
    await run(stack)
    assert fake.server("event")["server"]["assigned_profile"]["id"] == 1


async def test_profiles_are_deleted_after_the_event_retried_while_running_and_at_once_when_cancelled(
        stack, profiles, guild, clock, fake):
    event = await published(stack, clock)
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    await upload(stack, guild, event, maker)
    await profiles.build(event.id, MAKER)
    event = await stack.events.get(event.id)
    fake.running_profiles.add(event.profile_id)
    at(clock, fake, event.start_at + timedelta(minutes=event.duration_minutes) + timedelta(days=14))
    for day in range(WARN_AFTER):
        await run(stack)
        at(clock, fake, clock.now + timedelta(days=1))
    assert event.profile_id in fake.profiles and await stack.scheduler.exists(f"{CLEANUP}:{event.id}")
    assert any("7 dni" in n for n in await notes(stack))
    fake.running_profiles.clear()
    await run(stack)
    assert event.profile_id not in fake.profiles and (await stack.events.get(event.id)).profile_id is None

    start = clock.now + timedelta(days=3)
    draft = await stack.events.create(Draft("internal", "Op Y", Times(start, start + timedelta(minutes=30), 120)),
                                      MAKER)
    await stack.channels.open_draft(draft.id)
    await profiles.pick_mission(draft.id, MAKER, "op_x.Altis.pbo")
    built = await profiles.build(draft.id, MAKER)
    await stack.events.cancel(draft.id, MAKER, "no")
    await run(stack)
    assert built.profile_id not in fake.profiles


async def test_publishing_builds_the_profile_and_kept_profiles_stay(stack, profiles, guild, clock, fake):
    start = clock.now + timedelta(days=2)
    draft = await stack.events.create(Draft("internal", "Op Z", Times(start, start + timedelta(minutes=30), 120)),
                                      MAKER)
    await stack.channels.open_draft(draft.id)
    fake.missions["op_z.Altis.pbo"] = {"report": fake.mission_report("op_z.Altis.pbo")}
    await profiles.pick_mission(draft.id, MAKER, "op_z.Altis.pbo")
    assert (await stack.events.get(draft.id)).profile_id is None
    await stack.events.set_briefing(draft.id, MAKER, "x", "", "")
    from app.orbat.parser import parse
    await stack.signups.set_orbat(draft.id, parse("# A\nR\n", []))
    await stack.events.publish(draft.id, MAKER)
    event = await stack.events.get(draft.id)
    assert event.profile_state == "built" and event.profile_id in fake.profiles
    await profiles.set_keep(event.id, MAKER, True)
    with pytest.raises(ProfileError) as caught:
        await profiles.set_keep(event.id, Member(10, frozenset({MEMBER})), False)
    assert caught.value.problem == Problem.NOT_ALLOWED
    at(clock, fake, event.start_at + timedelta(days=15))
    await run(stack)
    assert event.profile_id in fake.profiles  # kept


async def test_the_report_is_read_again_once_the_addon_index_is_built(stack, profiles, guild, clock, fake):
    event = await published(stack, clock)
    fake.jobs[9] = {"id": 9, "status": "running", "progress": {}, "error": None}
    report = fake.mission_report("idx.Altis.pbo")
    report["index"] = {"built": False, "built_at": None, "job_id": 9}
    fake.missions["idx.Altis.pbo"] = {"report": report}
    await profiles.pick_mission(event.id, MAKER, "idx.Altis.pbo")
    assert ("index_incomplete", "") in await profiles.warnings(await stack.events.get(event.id))
    fake.jobs[9]["status"] = "succeeded"
    fake.missions["idx.Altis.pbo"]["report"] = fake.mission_report("idx.Altis.pbo")
    at(clock, fake, clock.now + timedelta(minutes=2))
    await run(stack)
    assert await profiles.warnings(await stack.events.get(event.id)) == []
