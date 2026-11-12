"""Steam links through the supervisor and their reminders (docs/PLAN-front-end.md, *Commands and buttons*), on the
Discord fakes and the fake supervisor."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import discord
import pytest
from conftest import MEMBER, make_settings
from fakes import FakeChannel, FakeInteraction, FakeMember, FakeRole, invoke

from app.links import ui
from app.links.cog import LinkCog
from app.links.service import LinkService
from app.links.watch import KIND, LinkWatch
from app.resolve import resolve
from app.roster.service import Member as RosterMember
from app.roster.service import Roster

ENGLISH = discord.Locale.british_english
ANNA, BOB = "76561198000000001", "76561198000000002"
RANK, LINKS_ROLE = 310, 313
REMINDERS = 207


@pytest.fixture
async def stack(services, guild, clock, fake):
    """The services with a rank role (the supervisor's squad.xml rank), a reminder channel, and links through a roster
    that pushes members the fake supervisor doesn't know yet."""
    for role_id, name in ((RANK, "Szeregowy"), (LINKS_ROLE, "Linki")):
        guild.add(FakeRole(role_id, name))
    guild.add(FakeChannel(REMINDERS, "steam"))
    fake.rank_roles = {str(RANK)}
    base = services.settings
    settings = make_settings(base.data_dir, channels=base.channels.model_dump() | {"link_reminder": REMINDERS},
                             permissions=base.permissions.model_dump() | {"manage_links": [LINKS_ROLE]})
    services.store.current = settings
    services.ids, _ = resolve(guild, settings)

    def one(member_id: int) -> RosterMember | None:
        m = guild.members.get(member_id)
        return RosterMember(m.id, m.name, m.name, tuple(r.id for r in m.roles)) if m else None

    services.roster = Roster(services.supervisor, services.scheduler, lambda: None, lambda: None, one)
    services.links = LinkService(services.db, services.supervisor, services.roster.as_member, clock)
    services.link_watch = LinkWatch(services, lambda: guild)
    return services


def person(guild, fake, member_id: int, name: str, *roles: int, known: bool = True) -> FakeMember:
    """A member on Discord and, unless ``known`` is False, on the supervisor's roster."""
    member = FakeMember(member_id, name, roles=[FakeRole(r) for r in roles])
    guild.members[member_id] = member
    if known:
        fake.person(member_id, name, roles=[str(r) for r in roles])
    return member


def asking(services, guild, who: FakeMember) -> FakeInteraction:
    return FakeInteraction(services, who, ENGLISH, guild=guild)


async def notes(services) -> list[str]:
    await asyncio.sleep(0)
    return services.staff.drain()


# ---------------------------------------------------------------------------- /link, /unlink, Link now

async def test_link_goes_to_the_supervisor_unconfirmed_with_the_squad_xml_address(stack, guild, fake):
    anna = person(guild, fake, 1, "anna_dc", RANK)
    cog = LinkCog(stack)
    asked = asking(stack, guild, anna)
    await invoke(cog.link, cog, asked, steam=f"https://steamcommunity.com/profiles/{ANNA}/")
    text = asked.replies[0].content
    assert text.startswith(f"Linked: Steam ID {ANNA}.") and "Staff confirm the link in the app" in text
    assert "https://app.example/squad/squad.xml" in text and "squad.xml is public" in text
    assert fake.persons["1"]["steam_uid"] == ANNA and fake.persons["1"]["steam_state"] == "unconfirmed"
    assert fake.acted == [("PUT", "/api/members/me/steam", "1")]
    assert [n["kind"] for n in fake.notices] == ["link_request"]  # the supervisor tells staff


async def test_link_by_a_member_the_supervisor_doesnt_know_yet_pushes_them_first(stack, guild, fake):
    anna = person(guild, fake, 1, "anna_dc", RANK, known=False)
    cog = LinkCog(stack)
    asked = asking(stack, guild, anna)
    await invoke(cog.link, cog, asked, steam=ANNA)
    assert asked.replies[0].content.startswith("Linked:")
    assert ("PUT", "/api/members/1") in fake.calls and fake.persons["1"]["steam_uid"] == ANNA


async def test_link_refuses_a_bad_id_a_custom_address_and_a_duplicate(stack, guild, fake):
    cog = LinkCog(stack)
    person(guild, fake, 2, "bob_dc", RANK)
    fake.persons["2"].update(steam_uid=ANNA, steam_state="confirmed")
    anna = person(guild, fake, 1, "anna_dc", RANK)
    for typed, expected in (("12345", "Give your Steam64 ID"),
                            ("https://steamcommunity.com/id/anna", "The bot can't read a custom address"),
                            (ANNA, "That Steam ID is linked to another member")):
        asked = asking(stack, guild, anna)
        await invoke(cog.link, cog, asked, steam=typed)
        assert asked.replies[0].content.startswith(expected), typed
    assert fake.persons["1"].get("steam_uid") is None
    assert [n["kind"] for n in fake.notices] == ["link_duplicate"]


async def test_a_disabled_account_hears_it_in_words(stack, guild, fake):
    anna = person(guild, fake, 1, "anna_dc", RANK)
    fake.persons["1"]["account"]["disabled"] = True
    cog = LinkCog(stack)
    asked = asking(stack, guild, anna)
    await invoke(cog.link, cog, asked, steam=ANNA)
    assert asked.replies[0].content == "Your account in the app is disabled. Ask staff if that is a mistake."


async def test_link_without_steam_opens_the_modal_and_unlink_removes(stack, guild, fake):
    cog = LinkCog(stack)
    anna = person(guild, fake, 1, "anna_dc", RANK)
    opened = asking(stack, guild, anna)
    await invoke(cog.link, cog, opened, steam=None)
    modal = opened.response.modals[0]
    modal.steam._value = ANNA
    await modal.on_submit(asking(stack, guild, anna))
    assert fake.persons["1"]["steam_uid"] == ANNA
    gone = asking(stack, guild, anna)
    await invoke(cog.unlink, cog, gone)
    assert gone.replies[0].content.startswith("The Steam link is removed")
    assert fake.persons["1"]["steam_uid"] is None
    nothing = asking(stack, guild, anna)
    await invoke(cog.unlink, cog, nothing)
    assert nothing.replies[0].content == "There is no Steam link to remove."


async def test_link_now_opens_the_modal(stack, guild, fake):
    pressed = asking(stack, guild, person(guild, fake, 1, "anna_dc", RANK))
    await ui.LinkNow().callback(pressed)
    assert isinstance(pressed.response.modals[0], ui.LinkModal)


async def test_the_supervisor_down_is_said_in_words(stack, guild, fake):
    anna = person(guild, fake, 1, "anna_dc", RANK)
    stack.supervisor.url = "http://127.0.0.1:1"
    cog = LinkCog(stack)
    asked = asking(stack, guild, anna)
    await invoke(cog.link, cog, asked, steam=ANNA)
    assert asked.replies[0].content == "The app can't be reached right now. Try again in a few minutes."


# ---------------------------------------------------------------------------- reminders

async def test_the_first_run_announces_once_and_dms_start_n_days_later(stack, guild, fake, clock):
    person(guild, fake, 1, "anna_dc", RANK)
    person(guild, fake, 2, "bob_dc", RANK)
    fake.persons["2"].update(steam_uid=BOB, steam_state="confirmed")
    await stack.link_watch.start()
    (announcement,) = guild.get_channel(REMINDERS).messages
    assert announcement.kwargs["view"].children[0].custom_id == "link:now"
    assert await notes(stack) == ["Start linków Steam: 1 członek bez linku dostanie DM za 14 dni, jeśli wcześniej "
                                  "się nie połączy."]
    await stack.link_watch.start()  # a restart: no second announcement
    assert len(guild.get_channel(REMINDERS).messages) == 1
    await stack.scheduler.run_due()
    assert guild.members[1].dms == []  # reminded by the announcement just now
    clock.now += timedelta(days=15)
    await stack.scheduler.run_due()
    assert guild.members[1].dms and guild.members[1].dms[0].kwargs["view"].children[0].custom_id == "link:now"
    assert guild.members[2].dms == []  # linked
    assert await stack.scheduler.exists(KIND)  # hourly, for good


async def test_the_rollout_waits_while_the_supervisor_is_away(stack, guild, fake, clock):
    person(guild, fake, 1, "anna_dc", RANK)
    url = stack.supervisor.url
    stack.supervisor.url = "http://127.0.0.1:1"
    await stack.link_watch.start()
    assert guild.get_channel(REMINDERS).messages == [] and not await stack.links.rollout_done()
    stack.supervisor.url = url
    clock.now += timedelta(hours=1)
    await stack.scheduler.run_due()  # the hourly run catches up
    assert len(guild.get_channel(REMINDERS).messages) == 1 and await stack.links.rollout_done()


async def test_gaining_a_rank_reminds_at_once_and_closed_dms_ping_the_channel(stack, guild, fake):
    await stack.links.mark_rollout([])
    anna = person(guild, fake, 1, "anna_dc", RANK)
    await stack.link_watch.roles_gained(anna)
    assert len(anna.dms) == 1
    await stack.link_watch.roles_gained(anna)  # only the first time
    assert len(anna.dms) == 1
    bob = person(guild, fake, 2, "bob_dc", RANK)
    bob.dms_closed = True
    await stack.link_watch.roles_gained(bob)
    (ping,) = guild.get_channel(REMINDERS).messages
    assert ping.content.startswith("<@2> ") and ping.kwargs["view"].children[0].custom_id == "link:now"
    cid = person(guild, fake, 3, "cid_dc", MEMBER)  # a role that is no squad.xml rank
    await stack.link_watch.roles_gained(cid)
    assert cid.dms == []


async def test_a_first_slot_without_a_link_asks_for_it(stack, guild, fake):
    from app.signup.service import Outcome
    from app.signup.ui import answer

    anna = person(guild, fake, 1, "anna_dc", RANK)
    first = asking(stack, guild, anna)
    await answer(first, Outcome(), "You have the slot.", ask_link=True)
    assert "Link your Steam account" in first.replies[0].content
    assert first.replies[0].kwargs["view"].children[0].custom_id == "link:now"
    second = asking(stack, guild, anna)
    await answer(second, Outcome(), "You have the slot.", ask_link=True)
    assert second.replies[0].content == "You have the slot." and "view" not in second.replies[0].kwargs
    bob = person(guild, fake, 2, "bob_dc", RANK)
    fake.persons["2"].update(steam_uid=BOB, steam_state="unconfirmed")
    linked = asking(stack, guild, bob)
    await answer(linked, Outcome(), "You have the slot.", ask_link=True)
    assert linked.replies[0].content == "You have the slot."  # linked already, even if not confirmed yet


def staff_asking(stack, guild, fake) -> FakeInteraction:
    return asking(stack, guild, guild.members.get(999) or person(guild, fake, 999, "staff_dc", LINKS_ROLE))


async def test_remind_now_and_reminders_off(stack, guild, fake):
    await stack.links.mark_rollout([])
    cog = LinkCog(stack)
    refused = asking(stack, guild, person(guild, fake, 5, "eve_dc", RANK))
    await invoke(cog.remind, cog, refused, member=guild.members[5])
    assert refused.replies[0].content == "You don't have the permission for this."
    cid = person(guild, fake, 3, "cid_dc", RANK)
    sent = staff_asking(stack, guild, fake)
    await invoke(cog.remind, cog, sent, member=cid)
    assert sent.replies[-1].content == "Reminder sent to <@3> by DM." and len(cid.dms) == 1
    assert (await stack.links.reminders())[3].count == 1
    anna = person(guild, fake, 1, "anna_dc", RANK)
    fake.persons["1"].update(steam_uid=ANNA, steam_state="confirmed")
    already = staff_asking(stack, guild, fake)
    await invoke(cog.remind, cog, already, member=anna)
    assert already.replies[-1].content == "<@1> is linked already."
    no_rank = staff_asking(stack, guild, fake)
    await invoke(cog.remind, cog, no_rank, member=person(guild, fake, 2, "bob_dc", MEMBER))
    assert no_rank.replies[-1].content == "<@2> has no rank role, so squad.xml doesn't need them."

    dan = person(guild, fake, 4, "dan_dc", RANK)
    off = staff_asking(stack, guild, fake)
    await invoke(cog.reminders, cog, off, member=dan, on=False)
    assert off.replies[0].content.startswith("Reminders for <@4> are off")
    await stack.link_watch.roles_gained(dan)  # gaining the rank: no DM
    assert dan.dms == []
    assert await stack.links.due_reminders({3, 4}, timedelta(0), 10) == [3]
    assert await stack.links.ask_at_signup(4) is False
    on = staff_asking(stack, guild, fake)
    await invoke(cog.reminders, cog, on, member=dan, on=True)
    assert await stack.links.due_reminders({4}, timedelta(0), 10) == [4]
    await stack.links.mute(4, True)
    await stack.links.link(4, BOB)  # linking ends it
    assert not await stack.links.muted(4)


async def test_reminders_go_to_the_longest_waiting(stack, clock):
    every = timedelta(days=14)
    assert await stack.links.due_reminders({1, 2}, every, 10) == [1, 2]
    await stack.links.reminded([1])
    assert await stack.links.due_reminders({1, 2}, every, 10) == [2]
    clock.now += timedelta(days=15)
    await stack.links.reminded([2])
    assert await stack.links.due_reminders({1, 2}, every, 1) == [1]


async def test_one_failed_reminder_doesnt_stop_the_others_and_is_tried_again(stack, guild, fake, clock, monkeypatch):
    await stack.links.mark_rollout([])
    clock.now += timedelta(days=15)
    closed = person(guild, fake, 1, "anna_dc", RANK)
    closed.dms_closed = True  # (so the reminder channel is pinged, and it refuses)
    open_ = person(guild, fake, 2, "bob_dc", RANK)

    async def refused(*_args, **_kwargs):
        raise discord.HTTPException(SimpleNamespace(status=429, reason="Too Many Requests"), "rate limited")

    monkeypatch.setattr(guild.get_channel(REMINDERS), "send", refused)
    await stack.link_watch._hourly(None, clock.now)
    assert len(open_.dms) == 1
    reminders = await stack.links.reminders()
    assert 2 in reminders and 1 not in reminders  # asked again next hour


async def test_the_attendance_counts_only_confirmed_links(stack, fake):
    fake.person(1, "anna", steam_uid=ANNA, steam_state="confirmed")
    fake.person(2, "bob", steam_uid=BOB, steam_state="unconfirmed")
    assert await stack.links.steam_ids() == {1: ANNA}
