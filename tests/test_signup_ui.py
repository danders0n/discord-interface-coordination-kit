"""Phases 4c-2 and 4d on the Discord fakes: presets and images, the sign-up buttons and /slot, offers in DMs or
the thread, approval in the moderation thread, the pings, and /events."""

from datetime import timedelta

import discord
import pytest
from conftest import ANNOUNCEMENT, MEMBER, MISSION_MAKER, MODERATOR, STAFF
from fakes import FakeAttachment, FakeInteraction, FakeMember, FakeRole, invoke

from app.approval.ui import ApprovalAction
from app.eventchannel.service import EventChannels
from app.events.cog import EventCog
from app.events.header import Header
from app.events.service import Draft, EventService
from app.events.times import Times
from app.orbat.parser import parse
from app.presets import parse_preset_html
from app.signup.service import Member, SignupService
from app.signup.ui import OfferAction, SignupAction
from app.staff_threads import StaffThreads

ENGLISH = discord.Locale.british_english
MAKER = Member(20, frozenset({MEMBER, MISSION_MAKER}))
PRESET = """<html><head><meta name="arma:PresetName" content="IBC Cold War" /></head><body><table>
<tr data-type="ModContainer"><td data-type="DisplayName">CBA_A3</td>
<td><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=450814997">link</a></td></tr>
<tr data-type="ModContainer"><td data-type="DisplayName">ACE3</td>
<td><a href="https://steamcommunity.com/sharedfiles/filedetails/?id=463939057">link</a></td></tr>
<tr data-type="DlcContainer"><td data-type="DisplayName">S.O.G. Prairie Fire</td>
<td><a href="https://store.steampowered.com/app/1227700">link</a></td></tr>
</table></body></html>"""


@pytest.fixture
def stack(services, fake, guild, clock):
    channels = EventChannels(services.db, services.store, lambda: services.ids, services.scheduler, lambda: guild,
                             interval=0)
    events = EventService(services.db, services.store, lambda: services.ids, services.scheduler,
                          services.reservations, channels, link=channels.link)
    signups = SignupService(services.db, services.store, lambda: services.ids, services.scheduler, channels,
                            lambda member_id: frozenset(r.id for r in guild.members[member_id].roles)
                            if member_id in guild.members else None)
    channels.events, channels.signups = events, signups
    channels.header = Header(services.db, services.store, lambda: services.ids, lambda: guild, clock, interval=0)
    channels.staff, channels.services = services.staff, services
    channels.threads = services.threads = StaffThreads(services.db, services.store)
    channels.roles_of = signups.roles_of
    services.events, services.signups, services.channels = events, signups, channels
    return services


def person(guild, member_id: int, *roles: int, dms_closed: bool = False) -> FakeMember:
    member = FakeMember(member_id, f"m{member_id}", roles=[FakeRole(r) for r in roles], dms_closed=dms_closed)
    guild.members[member_id] = member
    return member


def asking(stack, guild, who, channel=None) -> FakeInteraction:
    interaction = FakeInteraction(stack, who, ENGLISH, guild=guild)
    interaction.channel = channel
    return interaction


async def published(stack, clock, orbat="# Alpha\nLeader\nMedic [Medic]\nRifleman\n", days=5, creator=MAKER):
    start = clock.now + timedelta(days=days)
    event = await stack.events.create(Draft("internal", "Operation X", "Coop", Times(start, start + timedelta(minutes=30), 180)),
                                      creator)
    await stack.channels.open_draft(event.id)
    await stack.events.set_briefing(event.id, creator, "Night", "Bridge", "")
    await stack.signups.set_orbat(event.id, parse(orbat, [(3, "Medic")]))
    await stack.events.publish(event.id, creator)
    await stack.channels.redrawer.settle()
    return await stack.events.get(event.id)


# ------------------------------------------------------------------ presets and images

def test_the_ported_preset_parser():
    preset = parse_preset_html(PRESET)
    assert preset.name == "IBC Cold War" and [m.workshop_id for m in preset.mods] == [450814997, 463939057]
    assert preset.cdlc == ["vn"]
    with pytest.raises(ValueError):
        parse_preset_html("<html>nothing here</html>")


async def test_event_preset_sets_the_modset_dlc_and_its_message(stack, guild, clock):
    event = await published(stack, clock)
    channel = guild.get_channel(event.channel_id)
    cog = EventCog(stack)
    interaction = asking(stack, guild, person(guild, 20, MEMBER, MISSION_MAKER), channel)
    await invoke(cog.preset, cog, interaction, file=FakeAttachment("modset.html", PRESET.encode()))
    assert interaction.replies[0].content == "Preset saved: 2 mods."
    saved = await stack.events.get(event.id)
    assert saved.required_dlc == ["vn"] and saved.preset_name == "IBC Cold War"
    await stack.channels.redrawer.settle()
    keys = list((await stack.events.get(event.id)).messages)
    assert keys == ["info", "briefing", "preset", "orbat:0"]  # the ORBAT went after the late preset
    messages = channel.messages
    assert messages[2].kwargs["files"][0].filename == f"{event.id}.html"
    modset = next(f for f in messages[0].embeds[0].fields if f.name == "Modset")
    assert modset.value == "IBC Cold War: 2 mody\nCreator DLC: S.O.G. Prairie Fire"  # the public language: pl
    await stack.channels.redraw_now(event.id)  # nothing changed: the file isn't uploaded again
    assert "attachments" not in messages[2].edits[-1]
    stranger = asking(stack, guild, person(guild, 11, MEMBER), channel)
    await invoke(cog.preset, cog, stranger, file=FakeAttachment("modset.html", PRESET.encode()))
    assert stranger.replies[0].content == "You don't have the permission for this."


async def test_event_image_goes_into_the_briefing(stack, guild, clock):
    event = await published(stack, clock)
    channel = guild.get_channel(event.channel_id)
    cog = EventCog(stack)
    maker = person(guild, 20, MEMBER, MISSION_MAKER)
    wrong = asking(stack, guild, maker, channel)
    await invoke(cog.image, cog, wrong, file=FakeAttachment("map.txt", b"x"))
    assert wrong.replies[0].content == "Send a PNG, JPG, WEBP or GIF image."
    await invoke(cog.image, cog, asking(stack, guild, maker, channel), file=FakeAttachment("map.png", b"\x89PNG"))
    await stack.channels.redrawer.settle()
    briefing = channel.messages[1]
    assert briefing.embeds[0].image.url == f"attachment://{event.id}.png"
    assert briefing.edits[-1]["attachments"][0].filename == f"{event.id}.png"
    elsewhere = asking(stack, guild, maker, guild.get_channel(STAFF))
    await invoke(cog.image, cog, elsewhere, file=FakeAttachment("map.png", b"x"))
    assert elsewhere.replies[0].content == "Use this in an event's channel or its thread."


# ------------------------------------------------------------------ the sign-up buttons and /slot

async def test_sign_up_through_the_buttons(stack, guild, clock):
    event = await published(stack, clock)
    orbat = guild.get_channel(event.channel_id).messages[-1]
    actions = [item.custom_id for item in orbat.kwargs.get("view", orbat.edits[-1].get("view")).children]
    assert actions == [f"signup:{event.id}:{a}" for a in ("take", "reserve", "maybe", "cant", "leave")]
    anna = person(guild, 30, MEMBER)
    press = asking(stack, guild, anna)
    await SignupAction(event.id, "take").callback(press)
    picker = press.replies[0].kwargs["view"]
    assert [o.label for o in picker.children[0].options] == ["Alpha (2)"]  # the Medic slot needs a role she lacks
    squad = asking(stack, guild, anna)
    await picker._squad(squad, int(picker.children[0].options[0].value))
    slots = picker.children[1].options
    assert [o.label for o in slots] == ["Leader", "Rifleman"]
    taken = asking(stack, guild, anna)
    await picker._slot(taken, int(slots[0].value))
    assert taken.replies[0].content == "You have the slot. See you there!"
    leave = asking(stack, guild, anna)
    await SignupAction(event.id, "leave").callback(leave)
    assert leave.replies[0].content == "You are off this event."
    maybe = asking(stack, guild, anna)
    await SignupAction(event.id, "maybe").callback(maybe)
    assert maybe.replies[0].content == "Noted."


async def test_slot_take_with_autocomplete_and_the_dlc_warning(stack, guild, clock, settings):
    services = stack
    services.ids = type(services.ids)(**{**services.ids.__dict__, "dlc": {"vn": 777}})
    event = await published(stack, clock)
    async with stack.db.session() as session, session.begin():
        from app.db.models import Event
        (await session.get(Event, event.id)).required_dlc = ["vn"]
    cog = EventCog(stack)
    anna = person(guild, 30, MEMBER)
    here = asking(stack, guild, anna, guild.get_channel(event.channel_id))
    choices = await cog.free_slots(here, "rif")
    assert [c.name for c in choices] == ["Alpha · Rifleman"]
    took = asking(stack, guild, anna, guild.get_channel(event.channel_id))
    await invoke(cog.take, cog, took, slot=choices[0].value)
    assert took.replies[0].content.startswith("You have the slot.") and "S.O.G. Prairie Fire" in took.replies[0].content


# ------------------------------------------------------------------ offers

async def test_a_freed_slot_is_offered_by_dm_or_in_the_thread(stack, guild, clock):
    event = await published(stack, clock, orbat="# A\nRifleman\n")
    holder, first = person(guild, 30, MEMBER), person(guild, 31, MEMBER, dms_closed=True)
    second = person(guild, 32, MEMBER)
    slot = (await stack.signups.board(event.id)).slots[0]
    await stack.signups.take(event.id, slot.id, Member(30, frozenset({MEMBER})))
    await stack.signups.reserve(event.id, Member(31, frozenset({MEMBER})))
    await stack.signups.reserve(event.id, Member(32, frozenset({MEMBER})))
    await stack.signups.leave(event.id, Member(30, frozenset({MEMBER})))
    thread = guild.threads[event.thread_id]
    assert "<@31>" in thread.sent[-1].content and holder.dms == []  # DMs closed: pinged in the thread
    offer_view = thread.sent[-1].kwargs["view"]
    decline = asking(stack, guild, first)
    await offer_view.children[1].callback(decline)
    assert len(second.dms) == 1  # the next one, by DM
    take = asking(stack, guild, second)
    offer_id = int(second.dms[0].kwargs["view"].children[0].custom_id.split(":")[1])
    await OfferAction(offer_id, "take").callback(take)
    assert take.replies[0].content == "You have the slot. See you there!"
    assert (await stack.signups.board(event.id)).slots[0].holder_id == 32


# ------------------------------------------------------------------ approval and pings

async def test_approval_in_the_moderation_thread(stack, guild, clock):
    member = Member(10, frozenset({MEMBER}))
    start = clock.now + timedelta(days=5)
    event = await stack.events.create(Draft("internal", "Op M", "Coop", Times(start, start + timedelta(minutes=30), 180)), member)
    await stack.channels.open_draft(event.id)
    await stack.events.set_briefing(event.id, member, "x", "", "")
    await stack.signups.set_orbat(event.id, parse("# A\nR\n", []))
    assert await stack.events.publish(event.id, member) == "pending"
    moderation = next(t for t in guild.threads.values() if t.name == "Moderacja")
    (request,) = moderation.sent
    approve, reject = request.kwargs["view"].children
    assert not approve.item.disabled and reject.custom_id.endswith(":reject")
    press = asking(stack, guild, person(guild, 40, MEMBER, MODERATOR))
    await ApprovalAction(int(approve.custom_id.split(":")[1]), "approve").callback(press)
    assert press.replies[0].content == "Approved."
    assert (await stack.events.get(event.id)).status == "published"
    decision = request.edits[-1]
    assert decision["view"] is None and decision["embed"].fields[-1].value == "Zatwierdzone przez <@40>"
    thread = guild.threads[(await stack.events.get(event.id)).thread_id]
    assert "<@10>" in thread.sent[-1].content


async def test_reminders_and_the_sign_up_ping_go_to_the_thread(stack, guild, clock):
    services = stack
    start = clock.now + timedelta(days=3)
    event = await stack.events.create(Draft("internal", "Op R", "Coop", Times(start, start + timedelta(minutes=30), 180)), MAKER)
    await stack.channels.open_draft(event.id)
    await stack.events.set_signup_start(event.id, MAKER, clock.now + timedelta(days=1))
    await stack.events.set_briefing(event.id, MAKER, "x", "", "")
    await stack.signups.set_orbat(event.id, parse("# A\nR\n", []))
    await stack.events.publish(event.id, MAKER)
    clock.now += timedelta(days=1)
    await services.scheduler.run_due()
    thread = guild.threads[(await stack.events.get(event.id)).thread_id]
    assert thread.sent[-1].content.startswith(f"<@&{ANNOUNCEMENT}>")
    slot = (await stack.signups.board(event.id)).slots[0]
    await stack.signups.take(event.id, slot.id, Member(30, frozenset({MEMBER})))
    clock.now = event.assembly_at - timedelta(minutes=59)
    await services.scheduler.run_due()
    assert "<@30>" in thread.sent[-1].content
