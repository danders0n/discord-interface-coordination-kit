"""The wizard, the organiser panel and the header (docs/PLAN.md, *Creating an event*, *Organiser panel*), on fakes."""

from datetime import timedelta

import discord
import pytest
from conftest import ANNOUNCE, DRAFTS, EVENTS, HEADER, MEMBER, MISSION_MAKER, MODERATOR, settle
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
async def stack(services, fake, guild, clock):
    channels = EventChannels(services.db, services.store, lambda: services.ids, services.scheduler, lambda: guild,
                             interval=0)
    events = EventService(services.db, services.store, lambda: services.ids, services.scheduler,
                          services.reservations, channels, link=channels.link)
    signups = SignupService(services.db, services.store, lambda: services.ids, services.scheduler, channels,
                            lambda _m: frozenset({MEMBER}))
    channels.events, channels.signups = events, signups
    channels.header = Header(services.db, services.store, lambda: services.ids, lambda: guild, clock, interval=0)
    services.events, services.signups, services.channels = events, signups, channels
    yield services
    await settle(channels)


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


async def test_the_wizard_refuses_while_a_category_is_full(stack, guild):
    for n in range(50):
        await guild.create_text_channel(f"old-{n}", category=guild.get_channel(DRAFTS))
    start = asking(stack, guild, person(20, MEMBER, MISSION_MAKER))
    await ui.CreateEvent().callback(start)
    assert start.replies[0].content.startswith("The category Szkice is full")
    assert "view" not in start.replies[0].kwargs


async def test_the_panel_shows_what_needs_attention_and_the_invited_groups(stack, guild, clock):
    from app.db.models import Slot
    from app.events.groups import Groups

    maker = person(20, MEMBER, MISSION_MAKER)
    event, _ = await create(stack, guild, clock, maker, kind="hosted")
    abc = await Groups(stack.db).add("ABC", "Alpha Bravo", 20)
    await stack.events.update(event.id, Member(20, frozenset({MEMBER, MISSION_MAKER})), guest_group_ids=[abc.id])
    await stack.signups.set_orbat(event.id, parse("# Alpha\nMedic\n", []))
    async with stack.db.session() as session, session.begin():
        slot = (await session.execute(select(Slot).where(Slot.event_id == event.id))).scalar_one()
        slot.holder_id, slot.flagged = 30, True  # the holder lost the slot's role
    opened = asking(stack, guild, maker)
    await ui.open_panel(opened, event.id)
    assert "⚠ Medic (Alpha): <@30> lost the role it needs." in opened.replies[0].content
    shown = asking(stack, guild, maker)
    await ui.PanelAction(event.id, "choices").callback(shown)
    choices = shown.replies[0].kwargs["view"]
    groups = next(c for c in choices.children if getattr(c, "custom_id", "").endswith(":groups"))
    assert [o.default for o in groups.options] == [True]  # the current invited group is ticked
    await choices._chosen(asking(stack, guild, maker), "groups", [])  # all of them unticked
    await choices._go(asking(stack, guild, maker))
    assert await stack.events.guest_groups(event.id) == []


async def test_orbat_tools_are_locked_while_waiting_for_approval(stack, guild, clock):
    from app.orbat.templates import Templates

    member = person(10, MEMBER)
    event, _ = await create(stack, guild, clock, member)
    as_member = Member(10, frozenset({MEMBER}))
    await stack.events.set_briefing(event.id, as_member, "x", "", "")
    await stack.signups.set_orbat(event.id, parse("# A\nRifleman\n", []))
    await stack.events.publish(event.id, as_member)  # pending
    await Templates(stack.db).save("Platoon", "# B\nMedic x2\n", 20, False)
    tools = ui.OrbatTools(asking(stack, guild, member), await stack.events.get(event.id))
    pick = asking(stack, guild, member)
    await tools._template(pick)
    select_view = pick.replies[0].kwargs["view"]
    (choose,) = select_view.children
    choose._values = [choose.options[0].value]
    chosen = asking(stack, guild, member)
    await choose.callback(chosen)
    assert chosen.replies[0].content.startswith("The event waits for approval")
    assert [s.name for s in (await stack.signups.board(event.id)).slots] == ["Rifleman"]


async def test_templates_are_renamed_and_deleted_by_their_author_only(stack, guild, clock):
    from app.orbat.templates import Templates

    member = person(10, MEMBER)
    event, _ = await create(stack, guild, clock, member)
    templates = Templates(stack.db)
    await templates.save("Platoon", "# B\nMedic\n", 10, False)
    await templates.save("Theirs", "# C\nMedic\n", 20, False)
    tools = ui.OrbatTools(asking(stack, guild, member), event)
    manage = asking(stack, guild, member)
    await tools._manage(manage)
    view = manage.replies[0].kwargs["view"]
    assert [o.label for o in view.children[0].options] == ["Platoon"]  # only their own
    await view._picked(asking(stack, guild, member), int(view.children[0].options[0].value))
    rename = asking(stack, guild, member)
    await view._rename(rename)
    modal = rename.response.modals[0]
    modal.text._value = "Theirs"
    taken = asking(stack, guild, member)
    await modal.on_submit(taken)
    assert taken.replies[0].content == "A template of that name exists already."
    modal.text._value = "Rifle platoon"
    await modal.on_submit(asking(stack, guild, member))
    deleted = asking(stack, guild, member)
    await view._delete(deleted)
    assert deleted.response.edited[0]["content"] == "Template deleted."
    assert [x.name for x in await templates.all()] == ["Theirs"]
    nothing = asking(stack, guild, person(11, MEMBER))
    await ui.OrbatTools(asking(stack, guild, member), event)._manage(nothing)
    assert nothing.replies[0].content == "You have no templates to change."


async def test_a_manager_corrects_a_group_and_its_guest_channel_is_renamed(stack, guild, clock):
    from conftest import ADMIN
    from app.events.groups import Groups

    maker = person(20, MEMBER, MISSION_MAKER)
    abc = await Groups(stack.db).add("ABX", "Alpha Bravo", 20)
    await Groups(stack.db).add("XYZ", "Xray", 20)
    start = asking(stack, guild, maker)
    await ui.CreateEvent().callback(start)
    choices = start.replies[0].kwargs["view"]
    await choices._chosen(asking(stack, guild, maker), "kind", ["guest"])
    assert not any(getattr(c, "custom_id", "").endswith(":fixgroup") for c in choices.children)  # managers only
    event, _ = await create(stack, guild, clock, maker, kind="internal")
    await stack.events.update(event.id, Member(20, frozenset({MEMBER, MISSION_MAKER})), kind="guest",
                              host_group_id=abc.id)
    await settle(stack.channels)

    manager = person(30, MEMBER, ADMIN)
    shown = asking(stack, guild, manager)
    await ui.PanelAction(event.id, "choices").callback(shown)
    panel_choices = shown.replies[0].kwargs["view"]
    fix = next(c for c in panel_choices.children if getattr(c, "custom_id", "").endswith(":fixgroup"))
    picker = asking(stack, guild, manager)
    await fix.callback(picker)
    (select,) = picker.replies[0].kwargs["view"].children
    select._values = [str(abc.id)]
    opened = asking(stack, guild, manager)
    await select.callback(opened)
    modal = opened.response.modals[0]
    modal.acronym._value, modal.name._value = "xyz", "Alpha Bravo"
    clash = asking(stack, guild, manager)
    await modal.on_submit(clash)
    assert clash.replies[0].content == "Another group has that acronym."
    modal.acronym._value = "abc"
    done = asking(stack, guild, manager)
    await modal.on_submit(done)
    assert done.replies[0].content == "Saved."
    await settle(stack.channels)
    assert guild.get_channel(event.channel_id).name.endswith("-abc")


async def test_reload_names_the_events_still_using_a_removed_item(stack, guild, clock, monkeypatch):
    from app.admin.cog import AdminCog
    from app.settings import ReloadResult

    event, _ = await create(stack, guild, clock, person(20, MEMBER, MISSION_MAKER))

    def reload():
        stack.store.current = stack.settings.model_copy(update={"events": stack.settings.events.model_copy(
            update={"mission_types": ["TvT"]})})
        return ReloadResult(ok=True)

    monkeypatch.setattr(stack.store, "reload", reload)
    from conftest import ADMIN
    from fakes import invoke
    cog = AdminCog(stack)
    interaction = asking(stack, guild, person(40, ADMIN))
    await invoke(cog.reload, cog, interaction)
    assert "no longer in the settings" in interaction.replies[0].content and event.title in interaction.replies[0].content


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
