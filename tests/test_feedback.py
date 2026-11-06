"""Phase 6b: mission ratings, /attendance, /attendance-report, /profile, /mission-maker and /group, with the limits
of *Who sees what* (docs/PLAN.md, *Supervisor: attendance*), on the fakes."""

from datetime import timedelta

from conftest import ADMIN, MEMBER, MISSION_MAKER
from fakes import invoke
from test_attendance import ANNA, BOB, MAKER, asking, at, hold, person, play, published, run, stack  # noqa: F401

from app.attendance.cog import AttendanceCog
from app.attendance.ui import ReportAction
from app.eventchannel.service import RATING_CLOSE_KIND
from app.events.groups import Groups
from app.events.ui import PanelAction, panel


async def reported(stack, guild, clock, fake, kind="internal", host=None):
    """An event whose report is out: 1 attended (linked, played), 2 no-show (linked, didn't play)."""
    for member_id in (1, 2):
        person(guild, member_id, MEMBER)
    event = await published(stack, clock, kind=kind, host=host)
    await hold(stack, event, 1, 2)
    if kind != "guest":
        await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
        await stack.links.link(2, BOB, "Bob", 2, has_rank=True)
        fake.plays = [play(1, event.start_at + timedelta(minutes=5), 120, players=[(ANNA, "Anna", 5000)])]
        at(clock, fake, event.start_at + timedelta(minutes=140))
    else:
        at(clock, fake, event.start_at + timedelta(minutes=211))
    await run(stack)
    return await stack.events.get(event.id)


async def rate(stack, guild, event, member_id, stars, comment=""):
    pressed = asking(stack, guild, guild.members[member_id])
    await ReportAction(event.id, "rate").callback(pressed)
    if not pressed.response.modals:
        return pressed.replies[0].content
    modal = pressed.response.modals[0]
    modal.stars._value, modal.comment._value = stars, comment
    done = asking(stack, guild, guild.members[member_id])
    await modal.on_submit(done)
    return done.replies[0].content


# ---------------------------------------------------------------------------- ratings

async def test_attendees_rate_the_mission_and_the_report_shows_the_average(stack, guild, clock, fake):
    event = await reported(stack, guild, clock, fake)
    report = guild.get_channel(event.channel_id).messages[-2]
    buttons = [b.custom_id for b in report.kwargs["view"].children]
    assert buttons == [f"attendance:{event.id}:rate", f"attendance:{event.id}:edit"]
    assert await rate(stack, guild, event, 2, "5") == "Only those who attended rate the mission."
    assert await rate(stack, guild, event, 1, "7") == "Give 1 to 5 stars."
    assert (await rate(stack, guild, event, 1, "4", "Good fun")).startswith("Thank you")
    assert (await rate(stack, guild, event, 1, "5★")).startswith("Thank you")  # changed, not added
    await run(stack)
    edits = report.edits[-1]
    fields = {f.name: f.value for f in edits["embeds"][0].fields}
    assert fields["Ocena"] == "5.0 ★ (1 ocena)"
    at(clock, fake, clock.now + timedelta(days=3, minutes=1))
    assert await stack.scheduler.exists(f"{RATING_CLOSE_KIND}:{event.id}")
    await run(stack)
    assert [b.custom_id for b in report.edits[-1]["view"].children] == [f"attendance:{event.id}:edit"]
    assert await rate(stack, guild, event, 1, "3") == "Ratings for this event have closed."


async def test_a_correction_that_takes_attended_away_removes_the_rating(stack, guild, clock, fake):
    event = await reported(stack, guild, clock, fake)
    await rate(stack, guild, event, 1, "4")
    await stack.attendance.correct(event.id, MAKER, [1], "excused")
    assert await stack.feedback.ratings(event.id) == []


async def test_the_panel_shows_the_ratings_to_organisers_and_guest_ratings_only_to_staff(stack, guild, clock, fake):
    event = await reported(stack, guild, clock, fake)
    await rate(stack, guild, event, 1, "4", "Good fun")
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    shown = asking(stack, guild, maker)
    await PanelAction(event.id, "ratings").callback(shown)
    embed = shown.replies[0].kwargs["embed"]
    assert embed.title == "4.0 ★ (1 rating)" and "<@1>: Good fun" in embed.description

    abc = await Groups(stack.db).add("ABC", "Alpha Bravo", 20)
    guest = await reported(stack, guild, clock, fake, kind="guest", host=abc.id)
    report = guild.get_channel(guest.channel_id).messages[-2]
    assert report.kwargs["view"].children[0].item.label == "Oceń współpracę z ABC"
    assert (await rate(stack, guild, guest, 2, "3", "Late start")).startswith("Thank you")  # to confirm: may rate
    _, view = await panel(asking(stack, guild, maker), guest)
    assert "ratings" not in {item.action for item in view.children}  # a mission maker isn't staff
    assert {"join", "orbat"} <= {item.action for item in view.children}  # our part of the host's ORBAT
    staff = person(guild, 30, MEMBER, ADMIN)
    _, view = await panel(asking(stack, guild, staff), guest)
    assert "ratings" in {item.action for item in view.children}


# ---------------------------------------------------------------------------- the commands

async def test_attendance_shows_the_details_only_to_oneself_and_the_administration(stack, guild, clock, fake):
    event = await reported(stack, guild, clock, fake)
    cog = AttendanceCog(stack)
    own = asking(stack, guild, guild.members[2])
    await invoke(cog.attendance, cog, own, member=None)
    fields = {f.name: f.value for f in own.replies[0].kwargs["embed"].fields}
    assert fields["Attendance rate"] == "0%" and fields["No-show"] == "1" and event.title in fields["Recent events"]
    other = asking(stack, guild, guild.members[1])
    await invoke(cog.attendance, cog, other, member=guild.members[2])
    fields = {f.name: f.value for f in other.replies[0].kwargs["embed"].fields}
    assert set(fields) == {"Events attended", "Attendance rate"}
    staff = asking(stack, guild, person(guild, 30, ADMIN))
    await invoke(cog.attendance, cog, staff, member=guild.members[2])
    assert "No-show" in {f.name for f in staff.replies[0].kwargs["embed"].fields}
    outsider = asking(stack, guild, person(guild, 40))
    await invoke(cog.attendance, cog, outsider, member=None)
    assert outsider.replies[0].content == "This is for members."


async def test_the_attendance_report_is_for_bot_admins_with_a_csv(stack, guild, clock, fake):
    await reported(stack, guild, clock, fake)
    cog = AttendanceCog(stack)
    member = asking(stack, guild, guild.members[1])
    await invoke(cog.report, cog, member, period="90")
    assert member.replies[0].content == "You don't have the permission for this."
    admin = asking(stack, guild, person(guild, 30, ADMIN))
    await invoke(cog.report, cog, admin, period="all")
    sent = admin.replies[0]
    assert sent.kwargs["embed"].description.splitlines()[0] == "<@1>: 1 · 0 · 0 · 100%"
    csv_text = sent.kwargs["file"].fp.read().decode("utf-8-sig")
    assert csv_text.splitlines()[1] == "1,m1,1,0,0,1.000"


async def test_profile_mission_maker_and_group(stack, guild, clock, fake):
    abc = await Groups(stack.db).add("ABC", "Alpha Bravo", 20)
    event = await reported(stack, guild, clock, fake)
    await rate(stack, guild, event, 1, "4", "Good fun")
    guest = await reported(stack, guild, clock, fake, kind="guest", host=abc.id)
    await rate(stack, guild, guest, 1, "2", "Chaos")
    cog = AttendanceCog(stack)
    maker = person(guild, 20, MEMBER, MISSION_MAKER)

    own = asking(stack, guild, guild.members[1])
    await invoke(cog.profile, cog, own, member=None)
    fields = {f.name: f.value for f in own.replies[0].kwargs["embed"].fields}
    assert fields["squad.xml"] == f"Anna · Steam ID {ANNA}"
    other = asking(stack, guild, guild.members[2])
    await invoke(cog.profile, cog, other, member=guild.members[1])
    fields = {f.name: f.value for f in other.replies[0].kwargs["embed"].fields}
    assert fields["squad.xml"] == "Anna"  # no Steam ID for others
    authored = asking(stack, guild, maker)
    await invoke(cog.profile, cog, authored, member=None)
    fields = {f.name: f.value for f in authored.replies[0].kwargs["embed"].fields}
    assert fields["Missions authored"] == "1 · 4.0 ★ (1 rating)"  # the guest event is another group's

    by_author = asking(stack, guild, maker)
    await invoke(cog.mission_maker, cog, by_author, member=None)
    assert "Good fun" in by_author.replies[0].kwargs["embed"].description
    by_member = asking(stack, guild, guild.members[2])
    await invoke(cog.mission_maker, cog, by_member, member=maker)
    assert "Good fun" not in by_member.replies[0].kwargs["embed"].description

    choices = await cog.group_choices(asking(stack, guild, guild.members[2]), "ab")
    assert [c.value for c in choices] == ["ABC"]
    seen = asking(stack, guild, guild.members[2])
    await invoke(cog.group, cog, seen, acronym="abc")
    embed = seen.replies[0].kwargs["embed"]
    assert {f.name: f.value for f in embed.fields} == {"Events with us": "1"} and not embed.description
    staff = asking(stack, guild, person(guild, 30, ADMIN))
    await invoke(cog.group, cog, staff, acronym="ABC")
    embed = staff.replies[0].kwargs["embed"]
    assert {f.name: f.value for f in embed.fields}["Cooperation"] == "2.0 ★ (1 rating)" and "Chaos" in embed.description
    missing = asking(stack, guild, guild.members[2])
    await invoke(cog.group, cog, missing, acronym="XYZ")
    assert missing.replies[0].content == "No group with that acronym."
