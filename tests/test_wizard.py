"""The wizard, the organiser panel and the header (docs/PLAN.md, *Creating an event*, *Organiser panel*), on fakes."""

from datetime import timedelta

import discord
import pytest
from conftest import ANNOUNCE, DRAFTS, EVENTS, HEADER, MEMBER, MISSION_MAKER, MODERATOR
from fakes import FakeInteraction, FakeMember, FakeRole

from sqlalchemy import func, select

from app.db.models import Event
from app.eventchannel.service import EventChannels
from app.events import ui
from app.events.header import Header
from app.events.service import EventService
from app.orbat.parser import parse
from app.signup.service import Member, SignupService

ENGLISH = discord.Locale.british_english


@pytest.fixture
def stack(services, fake, guild, clock):
    channels = EventChannels(services.db, services.store, lambda: services.ids, services.scheduler, lambda: guild,
                             interval=0)
    events = EventService(services.db, services.store, lambda: services.ids, services.scheduler,
                          services.reservations, channels, link=channels.link)
    signups = SignupService(services.db, services.store, lambda: services.ids, services.scheduler, channels,
                            lambda _m: frozenset({MEMBER}))
    channels.events, channels.signups = events, signups
    channels.header = Header(services.db, services.store, lambda: services.ids, lambda: guild, clock, interval=0)
    services.events, services.signups, services.channels = events, signups, channels
    return services


def person(member_id: int, *roles: int) -> FakeMember:
    return FakeMember(member_id, f"m{member_id}", roles=[FakeRole(r) for r in roles])


def asking(services, guild, who: FakeMember) -> FakeInteraction:
    return FakeInteraction(services, who, ENGLISH, guild=guild)


async def create(stack, guild, clock, who, kind="internal", date_days=5) -> tuple:
    """Go through the wizard; returns the event and the reply."""
    start = asking(stack, guild, who)
    await ui.CreateEvent().callback(start)
    choices = start.replies[0].kwargs["view"]
    await choices._chosen(asking(stack, guild, who), "kind", [kind])
    await choices._chosen(asking(stack, guild, who), "type", ["Coop"])
    go = asking(stack, guild, who)
    await choices._go(go)
    modal = go.response.modals[0]
    day = (clock.now + timedelta(days=date_days)).astimezone(stack.settings.tz)
    modal.name._value, modal.date._value = "Operation X", f"{day:%d.%m.%Y}"
    modal.assembly._value, modal.start._value, modal.duration._value = "19:00", "19:30", "3h"
    submit = asking(stack, guild, who)
    await modal.on_submit(submit)
    async with stack.db.session() as session:
        newest = (await session.execute(select(func.max(Event.id)))).scalar()
    event = await stack.events.get(newest) if newest is not None else None
    return event, submit


async def test_the_wizard_makes_a_draft_and_its_channel(stack, guild, clock):
    event, submit = await create(stack, guild, clock, person(20, MEMBER, MISSION_MAKER))
    assert event is not None and event.title == "Operation X" and event.kind == "internal"
    channel = guild.get_channel(event.channel_id)
    assert channel.category_id == DRAFTS and channel.mention in submit.replies[0].content
    local = event.assembly_at.astimezone(stack.settings.tz)
    assert (local.hour, local.minute) == (19, 0) and event.duration_minutes == 180


async def test_the_wizard_answers_mistakes_and_needs(stack, guild, clock):
    outsider = asking(stack, guild, person(5))
    await ui.CreateEvent().callback(outsider)
    assert outsider.replies[0].content == "Creating events is for members."
    start = asking(stack, guild, person(10, MEMBER))
    await ui.CreateEvent().callback(start)
    choices = start.replies[0].kwargs["view"]
    kinds = [o.value for o in choices.children[0].options]
    assert kinds == ["internal", "hosted", "zeus"]  # a member can't make a guest event
    early = asking(stack, guild, person(10, MEMBER))
    await choices._go(early)
    assert early.replies[0].content == "Pick the kind and the mission type first."
    _, submit = await create(stack, guild, clock, person(10, MEMBER), date_days=400)
    assert submit.replies[0].content == "That date is too far ahead."


async def test_a_wizard_from_before_a_restart_says_so(stack, guild):
    old = asking(stack, guild, person(10, MEMBER))
    old.data = {"custom_id": "wizard:0123456789ab:go"}
    assert await ui.expired(old) is True
    assert "expired" in old.replies[0].content
    live = asking(stack, guild, person(10, MEMBER))
    live.data = {"custom_id": "event:create"}
    assert await ui.expired(live) is False


async def test_the_panel_is_for_organisers_and_guides_to_publishing(stack, guild, clock):
    maker = person(20, MEMBER, MISSION_MAKER)
    event, _ = await create(stack, guild, clock, maker)
    stranger = asking(stack, guild, person(11, MEMBER))
    await ui.open_panel(stranger, event.id)
    assert stranger.replies[0].content == "The panel is for this event's organisers."
    opened = asking(stack, guild, maker)
    await ui.open_panel(opened, event.id)
    assert "Still missing: the briefing, the ORBAT." in opened.replies[0].content
    assert "Server reserved." in opened.replies[0].content
    buttons = {item.action: item for item in opened.replies[0].kwargs["view"].children}
    assert "publish" in buttons and buttons["publish"].item.label == "Publish"
    refused = asking(stack, guild, maker)
    await buttons["publish"].callback(refused)
    assert refused.replies[0].content == "Still missing: the briefing, the ORBAT."

    brief = asking(stack, guild, maker)
    await buttons["briefing"].callback(brief)
    modal = brief.response.modals[0]
    modal.situation._value, modal.mission._value = "Night", "Take the bridge"
    await modal.on_submit(asking(stack, guild, maker))
    orbat = asking(stack, guild, maker)
    await buttons["orbat"].callback(orbat)
    tools = orbat.replies[0].kwargs["view"]
    paste = asking(stack, guild, maker)
    await tools._paste(paste)
    text = paste.response.modals[0]
    text.text._value = "# Alpha\nLeader\nRifleman x2\n"
    saved = asking(stack, guild, maker)
    await text.on_submit(saved)
    assert saved.replies[0].content == "Saved."
    published = asking(stack, guild, maker)
    await buttons["publish"].callback(published)
    assert published.replies[0].content == "Published."
    await stack.channels.redrawer.settle()
    assert guild.get_channel(event.channel_id).category_id == EVENTS and guild.get_channel(ANNOUNCE).messages


async def test_a_members_event_is_sent_for_approval_and_orbat_mistakes_name_the_line(stack, guild, clock):
    member = person(10, MEMBER)
    event, _ = await create(stack, guild, clock, member)
    opened = asking(stack, guild, member)
    await ui.open_panel(opened, event.id)
    buttons = {item.action: item for item in opened.replies[0].kwargs["view"].children}
    assert buttons["publish"].item.label == "Send for approval"
    await stack.events.set_briefing(event.id, Member(10, frozenset({MEMBER})), "x", "", "")
    tools = ui.OrbatTools(asking(stack, guild, member), event)
    paste = asking(stack, guild, member)
    await tools._paste(paste)
    modal = paste.response.modals[0]
    modal.text._value = "Rifleman\n"
    wrong = asking(stack, guild, member)
    await modal.on_submit(wrong)
    assert wrong.replies[0].content.startswith("Line 1:")
    modal.text._value = "# A\nRifleman\n"
    await modal.on_submit(asking(stack, guild, member))
    sent = asking(stack, guild, member)
    await buttons["publish"].callback(sent)
    assert sent.replies[0].content == "Sent to the moderators for approval."


async def test_cancelling_from_the_panel(stack, guild, clock):
    maker = person(20, MEMBER, MISSION_MAKER)
    event, _ = await create(stack, guild, clock, maker)
    cancel = asking(stack, guild, maker)
    await ui.PanelAction(event.id, "cancel").callback(cancel)
    modal = cancel.response.modals[0]
    modal.text._value = "no Zeus available"
    done = asking(stack, guild, maker)
    await modal.on_submit(done)
    assert done.replies[0].content == "Cancelled." and guild.get_channel(event.channel_id) is None


async def test_the_info_message_carries_the_panel_button(stack, guild, clock):
    event, _ = await create(stack, guild, clock, person(20, MEMBER, MISSION_MAKER))
    info = guild.get_channel(event.channel_id).messages[0]
    (button,) = info.kwargs["view"].children
    assert button.custom_id == f"event:panel:{event.id}"


async def test_the_header_lists_the_coming_events_and_keeps_one_message(stack, guild, clock):
    maker = person(20, MEMBER, MISSION_MAKER)
    event, _ = await create(stack, guild, clock, maker)
    header = stack.channels.header
    await header.draw()
    channel = guild.get_channel(HEADER)
    (message,) = channel.messages
    assert message.kwargs["view"].children[0].custom_id == "event:create"
    assert message.kwargs["embed"].fields[0].value == "Jeszcze nic nie zaplanowano."  # the public language: pl
    as_maker = Member(20, frozenset({MEMBER, MISSION_MAKER}))
    await stack.events.set_briefing(event.id, as_maker, "x", "", "")
    await stack.signups.set_orbat(event.id, parse("# A\nR x2\n", []))
    await stack.events.publish(event.id, as_maker)
    await header.draw()
    assert len(channel.messages) == 1  # edited in place
    assert "Operation X" in message.edits[-1]["embed"].fields[0].value and "0/2" in message.edits[-1]["embed"].fields[0].value
