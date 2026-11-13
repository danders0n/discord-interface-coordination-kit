"""Sign-ups, offers, reminders and the moderation thread (docs/PLAN-front-end.md, *Done when*, phase 4), on the
Discord fakes and the fake supervisor: members press buttons on Discord (act-as), others act in the app (the fake's
``sign`` / ``set_orbat`` / ``request``), and the bot draws, delivers and pings."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import MODERATOR, STAFF
from fakes import FakeInteraction, FakeMember, FakeRole, invoke
from test_channel import at, row, run, stack, texts  # noqa: F401  (stack: the fixture)

from app.approval.moderation import Moderation
from app.approval.ui import ApprovalAction, RejectModal
from app.discord_util import on_view_error
from app.notices.deliver import Delivery
from app.notices.service import Notices
from app.roster.service import Roster
from app.signup.cog import SlotCog
from app.signup.offers import Offers
from app.signup.ui import OfferAction, SignupAction, SquadList

SIGNUP, APPROVE, PUBLISH = "operations.signup", "operations.approve", "operations.publish"
ORBAT = "# Alpha\nLead\nRifleman x2\n# Bravo\nMedic"
OPORD = {"situation": "S", "mission": "M"}


@pytest.fixture
async def front(stack, guild, fake):  # noqa: F811
    """The channels and the feed, plus the roster (act-as), the notices worker, offers and the moderation thread.
    Anna (1), Bob (2) and Cid (3) may sign up; Mod (4) approves."""
    s = stack
    s.roster = Roster(s.supervisor, s.scheduler, lambda: None, lambda: None, lambda _id: None)
    s.notices = Notices(s.db, s.supervisor, s.scheduler.clock)
    s.offers = Offers(s, lambda: guild, guild.messageable)
    s.moderation = Moderation(s, lambda: guild)
    Delivery(s, lambda: guild).install(s.notices, s.offers, s.moderation)
    s.channels.watchers += [s.offers, s.moderation]
    fake.persons["1"]["permissions"] = [SIGNUP]
    fake.person(2, "bob", permissions=[SIGNUP])
    fake.person(3, "cid", permissions=[SIGNUP])
    fake.person(4, "mod", permissions=[SIGNUP, APPROVE])
    for discord_id, name in ((1, "anna"), (2, "bob"), (3, "cid"), (4, "mod")):
        guild.members[discord_id] = FakeMember(discord_id, name)
    guild.members[4].roles = [FakeRole(MODERATOR, "Moderator")]
    yield s


def acting(front, guild, discord_id: int, channel_id: int | None = None) -> FakeInteraction:
    interaction = FakeInteraction(front, guild.members[discord_id], guild=guild)
    interaction.channel_id = channel_id
    return interaction


async def press(item, interaction) -> None:
    """A button or list as Discord runs it: the callback, its error handler on a failure."""
    try:
        await item.callback(interaction)
    except Exception as exc:  # noqa: BLE001
        await on_view_error(None, interaction, exc, item)


def said(interaction) -> str:
    return interaction.replies[-1].content if interaction.replies else interaction.response.edited[-1]["content"]


def published(fake, title: str = "Ibis", orbat: str = ORBAT, **fields):
    op = fake.operation(title, status="published", opord=OPORD, **fields)
    if orbat:
        fake.set_orbat(op["id"], orbat)
    return fake.operations[op["id"]]


def slots(fake, op) -> dict[str, int]:
    """Slot ids by "Squad/Name n" (n counts repeats from 1)."""
    found, seen = {}, {}
    for squad in fake.operations[op["id"]]["squads"]:
        for slot in squad["slots"]:
            name = f"{squad['name']}/{slot['name']}"
            seen[name] = seen.get(name, 0) + 1
            found[name if seen[name] == 1 else f"{name} {seen[name]}"] = slot["id"]
    return found


def controls(view) -> dict[str, object]:
    """A view's buttons and lists by custom id (a persistent one's are DynamicItems around them)."""
    return {item.custom_id: item for item in (getattr(child, "item", child) for child in view.children)}


async def channel_of(front, guild, op):
    return guild.get_channel((await row(front, op["id"])).channel_id)


# ---------------------------------------------------------------- the ORBAT and its buttons


async def test_a_draft_shows_its_orbat_without_buttons(front, guild, fake):
    op = fake.operation("Ibis")
    fake.set_orbat(op["id"], ORBAT)
    await run(front)
    channel = await channel_of(front, guild, op)
    orbat = channel.get_partial_message((await row(front, op["id"])).messages["orbat:0"])._find()
    assert "Zapisy po publikacji" in orbat.text and "`1.` Lead — *wolny*" in orbat.text
    assert orbat.view is None


async def test_a_press_and_a_sign_up_in_the_app_both_show_and_a_taken_slot_is_refused(front, guild, fake):
    op = published(fake)
    ids = slots(fake, op)
    await run(front)
    fake.sign(op["id"], 2, slot_id=ids["Alpha/Rifleman"])  # Bob, on Home
    pick = acting(front, guild, 1)
    pick.data = {"values": [str(fake.operations[op["id"]]["squads"][0]["id"])]}
    await press(SquadList(op["id"], 0), pick)
    picker = pick.replies[-1].kwargs["view"]
    await picker._slot(acting(front, guild, 1), ids["Alpha/Lead"])
    confirm = acting(front, guild, 1)
    await picker._confirm(confirm)
    assert confirm.response.edited[-1]["content"] == "You have the slot. See you there!"
    await run(front)
    channel = await channel_of(front, guild, op)
    board = "\n".join(texts(channel))
    assert "`1.` Lead — <@1>" in board and "`2.` Rifleman — <@2>" in board
    assert "Zajęte sloty: **2/4**" in board
    taken = acting(front, guild, 3, channel.id)  # Cid tries Bob's slot
    cog = SlotCog(front)
    await invoke(cog.take, cog, taken, slot=str(ids["Alpha/Rifleman"]))
    assert said(taken) == "Someone was faster: that slot is taken."


async def test_the_buttons_follow_the_sign_up_state(front, guild, fake, clock):
    op = published(fake, signup_opens_at=clock.now + timedelta(hours=2))
    await run(front)
    view = (await channel_of(front, guild, op)).messages[-2].view  # (the last ORBAT message; the thread card last)
    disabled = {custom_id: item.disabled for custom_id, item in controls(view).items()}
    assert disabled[f"signup:{op['id']}:maybe"] is False and disabled[f"signup:{op['id']}:reserve"] is True
    assert disabled[f"squad:{op['id']}:0"] is True
    reserve = acting(front, guild, 1)
    await press(SignupAction(op["id"], "maybe"), reserve)
    assert said(reserve) == "Noted."
    at(clock, fake, op["start_at"] + timedelta(minutes=1))
    await run(front)  # the start: drawn again, everything off
    view = (await channel_of(front, guild, op)).messages[-2].view
    assert all(item.disabled for item in controls(view).values())


async def test_picking_needs_the_supervisors_take_and_says_why_not(front, guild, fake, clock):
    op = published(fake, signup_opens_at=clock.now + timedelta(hours=2))
    await run(front)
    pick = acting(front, guild, 1)
    pick.data = {"values": ["1"]}
    await press(SquadList(op["id"], 0), pick)
    assert said(pick) == "Sign-up isn't open yet."


async def test_a_zeus_operation_counts_coming_and_turns_coming_off_when_full(front, guild, fake):
    op = published(fake, kind="zeus", orbat="", signup_limit=1)
    await run(front)
    coming = acting(front, guild, 1)
    await press(SignupAction(op["id"], "coming"), coming)
    assert said(coming) == "You are coming. See you there!"
    await run(front)
    channel = await channel_of(front, guild, op)
    board = "\n".join(texts(channel))
    assert "**Będę** (1/1)\n<@1>" in board
    view = channel.messages[-2].view
    assert controls(view)[f"signup:{op['id']}:coming"].disabled is True
    late = acting(front, guild, 2)
    await press(SignupAction(op["id"], "coming"), late)
    assert said(late) == "The operation is full."


async def test_an_unknown_refusal_shows_the_supervisors_message(front, guild, fake):
    op = published(fake)
    await run(front)
    fake.persons["3"]["permissions"] = []  # Cid may no longer sign up
    leave = acting(front, guild, 3)
    await press(SignupAction(op["id"], "leave"), leave)
    assert said(leave) == "You don't have the permission for this."


# ---------------------------------------------------------------- offers


async def test_a_slot_freed_while_the_bot_was_down_is_offered_by_dm_when_it_is_back(front, guild, fake, clock):
    op = published(fake)
    ids = slots(fake, op)
    for discord_id, slot in ((1, "Alpha/Lead"), (2, "Alpha/Rifleman"), (3, "Alpha/Rifleman 2")):
        fake.sign(op["id"], discord_id, slot_id=ids[slot])
    fake.sign(op["id"], 4, status="reserve")  # (the app's shortcut: the free Medic doesn't stop it)
    await run(front)
    at(clock, fake, clock.now + timedelta(hours=5))  # (the bot is down a while)
    leave = acting(front, guild, 2)
    await press(SignupAction(op["id"], "leave"), leave)  # freed: offered to Mod, who waits for delivery
    offer = fake.operations[op["id"]]["offers"][0]
    assert offer["expires_at"] is None
    await front.notices.run_once()
    (dm,) = guild.members[4].dms
    assert dm.text.startswith("W Ibis zwolnił się slot dla ciebie: Alpha / Rifleman.")
    assert "Oferta czeka na ciebie do <t:" in dm.text  # the 30 minutes start at delivery
    assert offer["delivered_via"] == "dm" and offer["expires_at"] == clock.now + timedelta(minutes=30)
    take = acting(front, guild, 4)
    await press(OfferAction(op["id"], offer["id"], "take"), take)
    assert said(take) == "You have the slot. See you there!"
    await run(front)
    assert dm.text == "Oferta slotu w Ibis wygasła." and dm.view is None


async def test_closed_dms_get_the_offer_in_the_thread(front, guild, fake):
    op = published(fake)
    ids = slots(fake, op)
    fake.sign(op["id"], 1, slot_id=ids["Alpha/Lead"])
    fake.sign(op["id"], 2, status="reserve")
    await run(front)
    guild.members[2].dms_closed = True
    fake.free(fake.operations[op["id"]], fake.slot(fake.operations[op["id"]], ids["Alpha/Lead"]))
    await front.notices.run_once()
    thread = guild.get_thread((await row(front, op["id"])).thread_id)
    ping = thread.sent[-1]
    assert "Bierzesz go? <@2>\nOferta czeka na ciebie do <t:" in ping.text and ping.view is not None
    assert fake.operations[op["id"]]["offers"][0]["delivered_via"] == "thread"


async def test_near_the_assembly_the_whole_reserve_list_is_offered_in_the_thread(front, guild, fake, clock):
    op = published(fake, assembly=clock.now + timedelta(minutes=30))
    ids = slots(fake, op)
    fake.sign(op["id"], 1, slot_id=ids["Alpha/Lead"])
    for discord_id in (2, 3):
        fake.sign(op["id"], discord_id, status="reserve")
    await run(front)
    fake.free(fake.operations[op["id"]], fake.slot(fake.operations[op["id"]], ids["Alpha/Lead"]))
    await front.notices.run_once()
    thread = guild.get_thread((await row(front, op["id"])).thread_id)
    ping = thread.sent[-1]
    assert "<@2> <@3>" in ping.text and [item.label for item in controls(ping.view).values()] == ["Biorę"]
    assert guild.members[2].dms == []


# ---------------------------------------------------------------- reminders and pings


async def test_reminders_ping_the_holders_and_a_late_one_is_skipped(front, guild, fake, clock):
    op = published(fake, assembly=clock.now + timedelta(days=2))
    ids = slots(fake, op)
    fake.sign(op["id"], 1, slot_id=ids["Alpha/Lead"])
    await run(front)
    thread = guild.get_thread((await row(front, op["id"])).thread_id)
    at(clock, fake, op["assembly_at"] - timedelta(minutes=30))  # down through the day's reminder and the hour's
    await run(front)
    reminders = [m.text for m in thread.sent if m.text.startswith("Przypomnienie")]
    assert len(reminders) == 1 and reminders[0].endswith("<@1>")


async def test_cancelled_slot_lost_and_activity_notices(front, guild, fake):
    op = published(fake)
    ids = slots(fake, op)
    fake.sign(op["id"], 1, slot_id=ids["Bravo/Medic"])
    await run(front)
    fake.set_orbat(op["id"], "# Alpha\nLead")  # the Medic is gone: Anna first on the reserve list
    fake.notice("activity", operation_id=op["id"], data={"text": "OP-1 Ibis: @anna took Alpha / Lead"})
    fake.notice("cancelled", operation_id=op["id"], persons=[1], data={"title": "Ibis", "reason": "Rain"})
    await run(front)
    await front.notices.run_once()
    thread = guild.get_thread((await row(front, op["id"])).thread_id)
    pings = [m.text for m in thread.sent]
    assert "Twój slot w Ibis zniknął z ORBAT-u; jesteś pierwszy na liście rezerwowej. <@1>" in pings
    assert "Ibis została odwołana. Powód: Rain <@1>" in pings
    assert "OP-1 Ibis: @anna took Alpha / Lead" in front.staff.drain()


async def test_a_cancelled_operations_ping_reaches_its_archived_thread(front, guild, fake):
    op = published(fake)
    await run(front)
    fake.change(op["id"], status="cancelled", cancel_reason="Rain")
    await run(front)
    thread = guild.get_thread((await row(front, op["id"])).thread_id) or guild.threads[(await row(front, op["id"])).thread_id]
    assert thread.archived
    fake.notice("cancelled", operation_id=op["id"], persons=[1], data={"title": "Ibis", "reason": "Rain"})
    await front.notices.run_once()
    assert thread.sent[-1].text.endswith("<@1>") and thread.archived and thread.locked


# ---------------------------------------------------------------- the moderation thread


async def moderation_thread(front, guild):
    return await front.threads.get(guild, guild.get_channel(STAFF), "moderation")


async def test_a_request_is_approved_on_discord_and_the_channel_turns_public(front, guild, fake):
    op = fake.operation("Ibis", opord=OPORD, author_person_id=1)
    await run(front)
    fake.operations[op["id"]]["status"] = "pending"
    request = fake.request(op["id"], by="anna")
    fake.change(op["id"], revision=False, status="pending")
    await run(front)
    await front.notices.run_once()
    thread = await moderation_thread(front, guild)
    (message,) = thread.sent
    assert message.embeds[0].title == "Opublikować operację: Ibis"
    assert list(controls(message.view)) == [f"approval:{request['id']}:approve", f"approval:{request['id']}:reject"]
    approve = acting(front, guild, 4)
    await press(ApprovalAction(request["id"], "approve"), approve)
    assert said(approve) == "Approved."
    await run(front)
    await front.notices.run_once()
    assert fake.operations[op["id"]]["status"] == "published"
    drawn = await row(front, op["id"])
    assert drawn.thread_id and drawn.announcement_id  # public: the thread made, the announcement out
    decision = next(f for f in message.embeds[0].fields if f.name == "Decyzja")
    assert decision.value == "Zatwierdzone przez mod" and message.view is None
    pinged = guild.get_thread(drawn.thread_id).sent[-1].text
    assert pinged.startswith("Zatwierdzono: Opublikować operację – Ibis.") and pinged.endswith("<@1>")


async def test_a_moderator_rejects_with_a_reason_and_nobody_decides_their_own(front, guild, fake):
    op = fake.operation("Ibis", opord=OPORD, status="pending", author_person_id=1)
    request = fake.request(op["id"], by="mod")
    await run(front)
    await front.notices.run_once()
    own = acting(front, guild, 4)
    await press(ApprovalAction(request["id"], "approve"), own)
    assert said(own) == "Nobody decides their own request."
    fake.persons["2"]["permissions"].append(APPROVE)
    asked = acting(front, guild, 2)
    await press(ApprovalAction(request["id"], "reject"), asked)
    modal = asked.response.modals[0]
    assert isinstance(modal, RejectModal)
    modal.reason._value = "No mission yet"
    submitted = acting(front, guild, 2)
    await modal.on_submit(submitted)
    assert said(submitted) == "Rejected."
    await run(front)
    assert fake.operations[op["id"]]["status"] == "draft"
    message = (await moderation_thread(front, guild)).sent[0]
    decision = next(f for f in message.embeds[0].fields if f.name == "Decyzja")
    assert decision.value == "Odrzucone przez bob: No mission yet"


async def test_a_withdrawn_request_loses_its_buttons(front, guild, fake):
    op = fake.operation("Ibis", opord=OPORD, status="pending")
    request = fake.request(op["id"])
    await run(front)
    await front.notices.run_once()
    request["state"] = "withdrawn"
    fake.change(op["id"], revision=False, status="draft")
    await run(front)
    message = (await moderation_thread(front, guild)).sent[0]
    assert message.view is None
    assert next(f for f in message.embeds[0].fields if f.name == "Decyzja").value == "Wycofane"


async def test_a_stale_pending_draft_is_reminded_and_its_request_posted_again(front, guild, fake, clock):
    op = fake.operation("Ibis", opord=OPORD, status="pending", author_person_id=1)
    fake.request(op["id"])
    await run(front)
    await front.notices.run_once()
    fake.notice("stale_draft", operation_id=op["id"], persons=[1], data={
        "title": "Ibis", "stage": "reminder", "status": "pending",
        "deleted_at": (clock.now + timedelta(hours=24)).isoformat()})
    await front.notices.run_once()
    assert guild.members[1].dms[-1].text.startswith("Ibis wciąż nie jest opublikowana")  # a draft: by DM
    thread = await moderation_thread(front, guild)
    assert len(thread.sent) == 2 and thread.sent[0].view is None and thread.sent[1].view is not None
    assert any(note.startswith("Wciąż nieopublikowana: Ibis") for note in front.staff.drain())
