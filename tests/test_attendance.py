"""Attendance, ratings, reports and *End operation* (docs/PLAN-front-end.md, phase 5) on the Discord fakes and the
fake supervisor: the supervisor reports (the fake's ``report``), the bot draws the report and passes members'
presses on in their name."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from fakes import FakeMember, invoke
from test_channel import at, row, run, stack  # noqa: F401  (stack: a fixture)
from test_organise import modal_of, organise, panel_press, submit  # noqa: F401  (organise: a fixture)
from test_signup import APPROVE, OPORD, ORBAT, acting, controls, front, press, said  # noqa: F401

from app.archive.service import ArchiveService
from app.attendance.cog import AttendanceCog
from app.attendance.ui import Editor, ReportAction
from app.channel import render
from app.db.models import OperationChannel


async def reported(organise, fake, guild, clock, lines=None, **fields):
    """A published operation of Anna's with Anna and Bob in slots, then its attendance reported."""
    op = fake.operation("Ibis", status="published", opord=OPORD, author_person_id=1, **fields)
    fake.set_orbat(op["id"], ORBAT)
    slots = fake.slots(fake.operations[op["id"]])
    fake.sign(op["id"], 1, slot_id=slots[0]["id"])
    fake.sign(op["id"], 2, slot_id=slots[1]["id"])
    await run(organise)
    at(clock, fake, op["start_at"] + timedelta(hours=4))
    fake.report(op["id"], lines or [(1, "attended"), (2, "no_show"), ("Ghost", "walk_in")], guests=3)
    await run(organise)
    return op


async def report_message(organise, guild, op):
    drawn = await row(organise, op["id"])
    return guild.get_channel(drawn.channel_id).get_partial_message(drawn.messages["report"])._find()


async def test_the_report_is_drawn_with_rate_and_edit(organise, guild, fake, clock):
    op = await reported(organise, fake, guild, clock)
    message = await report_message(organise, guild, op)
    fields = {f.name: f.value for f in message.embeds[0].fields}
    assert fields["Obecni (1)"] == "<@1>" and fields["Nieobecni (1)"] == "<@2>"
    assert fields["Grali bez slotu (1)"] == "Ghost" and fields["Goście"] == "3 graczy"
    assert list(controls(message.view)) == [f"attendance:{op['id']}:rate", f"attendance:{op['id']}:edit"]
    await organise.notices.run_once()
    assert any(note.startswith("Obecność na Ibis:") for note in organise.staff.drain())


async def test_an_attendee_rates_and_the_report_shows_the_average(organise, guild, fake, clock):
    op = await reported(organise, fake, guild, clock)
    pressed = acting(organise, guild, 1)
    await press(ReportAction(op["id"], "rate"), pressed)
    modal = modal_of(pressed)
    submit(modal, stars="9")
    wrong = acting(organise, guild, 1)
    await modal.on_submit(wrong)
    assert said(wrong) == "Give 1 to 5 stars."
    submit(modal, stars="5", comment="Great")
    saved = acting(organise, guild, 1)
    await modal.on_submit(saved)
    assert said(saved) == "Thank you: your rating is saved. You can change it until the ratings close."
    await run(organise)
    fields = {f.name: f.value for f in (await report_message(organise, guild, op)).embeds[0].fields}
    assert fields["Ocena"] == "5.0 ★ (1 ocena)"
    absent = acting(organise, guild, 2)
    await press(ReportAction(op["id"], "rate"), absent)
    assert said(absent) == "Only those who attended rate the mission."
    at(clock, fake, clock.now + timedelta(days=4))
    await run(organise)  # the window closed: Rate goes
    assert list(controls((await report_message(organise, guild, op)).view)) == [f"attendance:{op['id']}:edit"]


async def test_organisers_correct_the_attendance(organise, guild, fake, clock):
    op = await reported(organise, fake, guild, clock)
    stranger = acting(organise, guild, 3)
    await press(ReportAction(op["id"], "edit"), stranger)
    assert said(stranger) == "Only the operation's organisers edit the attendance."
    opened = acting(organise, guild, 1)
    await press(ReportAction(op["id"], "edit"), opened)
    editor = opened.replies[-1].kwargs["view"]
    assert isinstance(editor, Editor)
    await editor._status(acting(organise, guild, 1), "excused")
    done = acting(organise, guild, 1)
    await editor._members(done, [guild.members[2], guild.members[3]])
    assert said(done) == "2 members are now Excused."
    statuses = {e["person_id"]: e["status"] for e in fake.operations[op["id"]]["entries"]}
    assert statuses[fake.persons["2"]["id"]] == statuses[fake.persons["3"]["id"]] == "excused"


async def test_end_operation_in_the_panel_after_the_start(organise, guild, fake, clock):
    op = fake.operation("Ibis", status="published", opord=OPORD, author_person_id=1)
    early = await panel_press(organise, guild, 1, op["id"], "end")
    assert said(early) == "That can't be done now; open the panel again to see what can."
    at(clock, fake, op["start_at"] + timedelta(minutes=90))
    done = await panel_press(organise, guild, 1, op["id"], "end")
    assert said(done) == "The operation has ended; the attendance report follows shortly."
    assert fake.operations[op["id"]]["status"] == "ended"
    await asyncio.sleep(0)  # (the staff log queues on the next tick)
    notes = organise.staff.drain()
    assert any("zakończył(a) operację Ibis" in note for note in notes)


async def test_attendance_unconfirmed_and_profile_failed_notices(organise, guild, fake, clock):
    op = await reported(organise, fake, guild, clock, kind="guest", how_to_join="ts3")
    fake.notice("attendance_unconfirmed", operation_id=op["id"], persons=[1], data={"title": "Ibis", "to_confirm": 2})
    fake.notice("profile_failed", operation_id=op["id"], data={"title": "Ibis", "reason": "job failed"})
    await organise.notices.run_once()
    thread = guild.threads[(await row(organise, op["id"])).thread_id]
    assert thread.sent[-1].text.startswith("Ibis: 2 graczy wciąż czeka na potwierdzenie") and thread.archived is False
    assert "Nie udało się zbudować profilu serwera dla Ibis: job failed" in organise.staff.drain()


# ---------------------------------------------------------------- reports


async def test_attendance_profile_and_mission_maker(organise, guild, fake, clock):
    op = await reported(organise, fake, guild, clock)
    fake.persons["1"]["steam_uid"], fake.persons["1"]["steam_state"] = "76561198000000001", "confirmed"
    cog = AttendanceCog(organise)
    mine = acting(organise, guild, 1)
    await invoke(cog.attendance, cog, mine)
    fields = {f.name: f.value for f in mine.replies[-1].embeds[0].fields}
    assert (fields["Operations attended"], fields["Attendance rate"], fields["No-show"]) == ("1", "100%", "0")
    about = acting(organise, guild, 3)
    await invoke(cog.profile, cog, about, member=guild.members[2])
    embed = about.replies[-1].embeds[0]
    names = [f.name for f in embed.fields]
    assert embed.title == "Profile: bob" and "No-show" not in names and "squad.xml" not in names  # the basics only
    own = acting(organise, guild, 1)
    await invoke(cog.profile, cog, own)
    squad = next(f for f in own.replies[-1].embeds[0].fields if f.name == "squad.xml")
    assert squad.value == "Linked · Steam ID 76561198000000001"
    fake.operations[op["id"]]["ratings"].append({"person_id": 1, "stars": 4, "comment": "Fun"})
    made = acting(organise, guild, 1)
    await invoke(cog.mission_maker, cog, made)
    assert "**Ibis** · 4.0 ★ (1 rating)" in made.replies[-1].embeds[0].description
    assert "> ★★★★ Fun" in made.replies[-1].embeds[0].description  # the author reads the comments


async def test_the_attendance_report_is_for_staff_with_a_csv(organise, guild, fake, clock):
    await reported(organise, fake, guild, clock)
    cog = AttendanceCog(organise)
    member = acting(organise, guild, 1)
    await invoke(cog.report, cog, member, period="30")
    assert said(member) == "You don't have the permission for this."
    staff = acting(organise, guild, 4)  # Mod approves operations
    await invoke(cog.report, cog, staff, period="all")
    answer = staff.replies[-1]
    assert answer.embeds[0].title == "Attendance, all operations"
    assert answer.embeds[0].description.splitlines()[0] == "<@1>: 1 · 0 · 0 · 100%"
    assert answer.kwargs["file"].filename == "attendance.csv"


async def test_group_lists_our_operations_with_them(organise, guild, fake, clock):
    fake.groups.append({"id": 1, "acronym": "RHS", "name": "Red Hammer"})
    fake.operation("Hammer", status="published", opord=OPORD, kind="hosted", group_ids=[1])
    cog = AttendanceCog(organise)
    asked = acting(organise, guild, 1)
    choices = await cog.group_choices(asked, "rh")
    assert [(c.name, c.value) for c in choices] == [("RHS · Red Hammer", "1")]
    await invoke(cog.group, cog, asked, group="1")
    embed = asked.replies[-1].embeds[0]
    assert embed.title == "RHS · Red Hammer" and "Hammer" in embed.description


# ---------------------------------------------------------------- the archive


async def test_participants_may_read_the_archive(organise, fake, guild, clock):
    op = await reported(organise, fake, guild, clock)
    drawn = await row(organise, op["id"])
    assert render.participants(drawn.operation) == [1, 2]
    readable = ArchiveService._may(OperationChannel(operation=drawn.operation), 2)
    stranger = ArchiveService._may(OperationChannel(operation=drawn.operation), FakeMember(3).id)
    assert readable and not stranger
