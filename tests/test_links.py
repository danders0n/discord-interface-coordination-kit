"""Steam links and squad.xml (docs/PLAN.md, *Steam link and squad.xml*, phase 5): the service, the import of the old
bot's links, the file, the commands and the reminders, on fakes."""

import asyncio
import json
from datetime import timedelta

import discord
import pytest
from conftest import ADMIN, GUILD, MEMBER, make_settings
from fakes import FakeChannel, FakeInteraction, FakeMember, FakeRole, invoke

from app.links import ui
from app.links.cog import LinkCog
from app.links.service import LinkError, LinkService, Problem, import_old, steam_id
from app.links.watch import KIND, LinkWatch
from app.resolve import resolve
from app.squadxml.service import Entry, Header, Person, SquadXml, remark, render

ENGLISH = discord.Locale.british_english
ANNA, BOB = "76561198000000001", "76561198000000002"
RANK, PILOT, MEDIC, LINKS_ROLE = 310, 311, 312, 313
REMINDERS = 207


@pytest.fixture
async def stack(services, guild, clock, tmp_path):
    """The services with a rank role, two remark roles, a reminder channel and squad.xml in a temp folder."""
    for role_id, name in ((RANK, "Szeregowy"), (PILOT, "Pilot"), (MEDIC, "Medic"), (LINKS_ROLE, "Linki")):
        guild.add(FakeRole(role_id, name))
    guild.add(FakeChannel(REMINDERS, "steam"))
    out = tmp_path / "www"
    out.mkdir()
    base = services.settings
    settings = make_settings(base.data_dir, roles=base.roles.model_dump() | {"rank": [RANK], "remark": [MEDIC, PILOT]},
                             channels=base.channels.model_dump() | {"link_reminder": REMINDERS},
                             permissions=base.permissions.model_dump() | {"manage_links": [LINKS_ROLE]},
                             squad_xml={"output_dir": out, "url": "https://example.org/squad.xml", "picture": "logo.paa"})
    services.store.current = settings
    services.ids, _ = resolve(guild, settings)
    roster = lambda: {m.id: Person(frozenset(r.id for r in m.roles), m.name) for m in guild.members.values()}
    services.roles_of = lambda member_id: (frozenset(r.id for r in guild.members[member_id].roles)
                                           if member_id in guild.members else None)
    services.links = LinkService(services.db, clock)
    services.squadxml = SquadXml(services.links, services.store, lambda: services.ids, roster, delay=0)
    services.link_watch = LinkWatch(services, lambda: guild, roster)
    yield services
    await services.squadxml.settle()


def person(guild, member_id: int, name: str, *roles: int) -> FakeMember:
    member = FakeMember(member_id, name, roles=[FakeRole(r) for r in roles])
    guild.members[member_id] = member
    return member


def asking(services, guild, who: FakeMember) -> FakeInteraction:
    return FakeInteraction(services, who, ENGLISH, guild=guild)


async def notes(services) -> list[str]:
    await asyncio.sleep(0)
    return services.staff.drain()


# ---------------------------------------------------------------------------- the service

def test_steam_ids_are_read_from_the_id_or_a_profile_address():
    assert steam_id(f" {ANNA} ") == ANNA
    assert steam_id(f"https://steamcommunity.com/profiles/{ANNA}/") == ANNA
    for wrong, code in (("https://steamcommunity.com/id/anna", Problem.CUSTOM_URL), ("1234", Problem.BAD_ID),
                        ("12345678901234567", Problem.BAD_ID)):
        with pytest.raises(LinkError) as caught:
            steam_id(wrong)
        assert caught.value.problem == code


async def test_one_steam_id_belongs_to_one_member(db, clock):
    links = LinkService(db, clock)
    first = await links.link(1, ANNA, " Anna  K ", 1, has_rank=True)
    assert (first.nick, first.before, first.changed) == ("Anna K", None, True)
    with pytest.raises(LinkError) as caught:
        await links.link(2, ANNA, "Bob", 2, has_rank=True)
    assert (caught.value.problem, caught.value.owner_id) == (Problem.TAKEN, 1)
    again = await links.link(1, ANNA, "Anna K", 1, has_rank=True)
    assert again.changed is False
    moved = await links.link(1, BOB, "Anna", 9, has_rank=True)
    assert moved.before == (ANNA, "Anna K") and (await links.get(1)).linked_by == 9
    assert (await links.remove(1)).steam_id == BOB and await links.get(1) is None


async def test_a_link_is_deleted_only_after_n_days_without_a_rank(db, clock):
    links = LinkService(db, clock)
    await links.link(1, ANNA, "Anna", 1, has_rank=True)
    await links.link(2, BOB, "Bob", 2, has_rank=False)  # linked without a rank: the clock runs from now
    await links.rank_changed(1, False)
    clock.now += timedelta(days=50)
    await links.rank_changed(1, True)  # back within the N days: nothing happens
    clock.now += timedelta(days=41)
    assert [gone.member_id for gone in await links.expire(90)] == [2]
    await links.reconcile(set())  # member 1 left while the bot was down
    clock.now += timedelta(days=90)
    assert [gone.member_id for gone in await links.expire(90)] == [1]


async def test_reminders_go_to_the_longest_waiting_and_a_first_signup_asks_once(db, clock):
    links = LinkService(db, clock)
    await links.link(3, ANNA, "Anna", 3, has_rank=True)
    every = timedelta(days=14)
    assert await links.due_reminders({1, 2, 3}, every, 10) == [1, 2]  # 3 is linked
    await links.reminded([1])
    assert await links.due_reminders({1, 2}, every, 10) == [2]
    clock.now += timedelta(days=15)
    await links.reminded([2])
    assert await links.due_reminders({1, 2}, every, 1) == [1]
    assert await links.ask_at_signup(1) is True and await links.ask_at_signup(1) is False
    assert await links.ask_at_signup(3) is False  # linked already


async def test_the_old_bots_links_are_imported_with_what_was_skipped(db, tmp_path):
    old = tmp_path / "steam_ids.json"
    old.write_text(json.dumps({
        "100": {"steam_uid": ANNA, "game_nick": "Anna"},
        "101": {"steam_uid": ANNA, "game_nick": "Copy"},
        "102": {"steam_uid": "123", "game_nick": "Short"},
        "103": {"steam_uid": BOB, "game_nick": ""},
        "x": {"steam_uid": BOB, "game_nick": "Bob"},
    }), encoding="utf-8")
    imported, skipped = await import_old(db, old)
    assert imported == 1
    assert skipped == [f"101: Steam ID {ANNA} belongs to 100", "102: bad Steam ID '123'", "103: bad nick ''",
                       "x: not a Discord id"]
    again, skipped = await import_old(db, old)
    assert again == 0 and skipped[0] == "100: already linked"


async def test_the_import_command(settings, tmp_path, capsys, monkeypatch):
    from app import __main__ as cli

    old = tmp_path / "steam_ids.json"
    old.write_text(json.dumps({"100": {"steam_uid": ANNA, "game_nick": "Anna"}}), encoding="utf-8")
    monkeypatch.setattr(cli, "load_or_exit", lambda _env: settings)
    monkeypatch.setattr(cli.logs, "setup", lambda *a, **k: None)
    await asyncio.to_thread(cli.main, ["import-old-links", str(old)])  # (it runs its own event loop)
    assert "Imported 1 Steam links" in capsys.readouterr().out


# ---------------------------------------------------------------------------- squad.xml

def test_squad_xml_keeps_the_old_bots_layout_and_escapes():
    header = Header("IBC", "Inglourious Basterds Clan", "N/A", "units.arma3.com/unit/ibc", "logo.paa", "IBC & co")
    text = render(header, [Entry(BOB, "zed", "zed_dc", ""), Entry(ANNA, 'Anna "<A>"', "anna", "Medic, Pilot")])
    assert text.startswith('<?xml version="1.0"?>\n<!DOCTYPE squad SYSTEM "squad.dtd">\n')
    assert "\t<title>IBC &amp; co</title>\n\n\n" in text and text.endswith("</member>\n</squad>")
    assert text.index(ANNA) < text.index(BOB)  # sorted by nick
    assert f"<member id=\"{ANNA}\" nick='Anna \"&lt;A&gt;\"'>\n        <name>Anna \"&lt;A&gt;\"</name>" in text
    assert "        <icq>anna</icq>\n        <remark>Medic, Pilot</remark>\n</member>" in text
    long = remark(["x" * 100, "y" * 100])
    assert len(long) == 128 and long.endswith("…")


async def test_squad_xml_lists_ranked_linked_members_and_writes_only_changes(stack, guild):
    person(guild, 1, "anna_dc", RANK, PILOT, MEDIC)
    person(guild, 2, "bob_dc", MEMBER)  # linked, but no rank
    person(guild, 3, "cid_dc", RANK)  # a rank, but no link
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    await stack.links.link(2, BOB, "Bob", 2, has_rank=False)
    assert await stack.squadxml.write_now() is True
    text = stack.squadxml.path.read_text(encoding="utf-8")
    assert ANNA in text and BOB not in text and "<remark>Medic, Pilot</remark>" in text  # config order
    assert "<squad nick=\"IBC\">" in text and "<picture>logo.paa</picture>" in text
    assert await stack.squadxml.write_now() is False  # the same text: untouched
    stack.squadxml.roster = lambda: None  # not connected: never an empty file
    assert await stack.squadxml.write_now() is False and ANNA in stack.squadxml.path.read_text(encoding="utf-8")


async def test_a_burst_of_changes_writes_once(stack, guild, monkeypatch):
    person(guild, 1, "anna_dc", RANK)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    writes = []
    real = stack.squadxml.write_now

    async def counted():
        writes.append(1)
        return await real()

    monkeypatch.setattr(stack.squadxml, "write_now", counted)
    for _ in range(5):
        stack.squadxml.request()
    await stack.squadxml.settle()
    assert len(writes) == 1 and stack.squadxml.path.is_file()


# ---------------------------------------------------------------------------- the supervisor

async def test_the_last_in_game_name_comes_from_the_supervisor(services, fake):
    fake.players = [{"uid": ANNA, "name": "Anna"}, {"uid": "76561198000000011", "name": ANNA}]
    assert (await services.supervisor.player_by_uid(ANNA))["name"] == "Anna"
    assert ("GET", "/api/stats/players") in fake.calls
    fake.capabilities.remove("player_uid_lookup")
    await services.supervisor.refresh_info()
    assert (await services.supervisor.player_by_uid(ANNA))["name"] == "Anna"  # q= also finds the name: exact uid
    assert await services.supervisor.player_by_uid(BOB) is None


# ---------------------------------------------------------------------------- the commands

async def test_link_answers_with_the_in_game_name_squad_xml_and_privacy(stack, guild, fake):
    fake.players = [{"uid": ANNA, "name": "AnnaInGame"}]
    anna = person(guild, 1, "anna_dc", RANK)
    cog = LinkCog(stack)
    asked = asking(stack, guild, anna)
    await invoke(cog.link, cog, asked, steam=f"steamcommunity.com/profiles/{ANNA}", nick="Anna")
    text = asked.replies[0].content
    assert text.startswith(f"Linked: Steam ID {ANNA}, nick Anna.")
    assert "last saw this account as AnnaInGame" in text and "https://example.org/squad.xml" in text
    assert "squad.xml is public" in text
    (note,) = await notes(stack)
    assert f"<@1>: — → {ANNA} (Anna)" in note
    await stack.squadxml.settle()
    assert ANNA in stack.squadxml.path.read_text(encoding="utf-8")


async def test_link_refuses_a_custom_address_and_a_duplicate(stack, guild):
    cog = LinkCog(stack)
    await stack.links.link(2, ANNA, "Bob", 2, has_rank=True)
    custom = asking(stack, guild, person(guild, 1, "anna_dc", RANK))
    await invoke(cog.link, cog, custom, steam="https://steamcommunity.com/id/anna", nick="Anna")
    assert "custom address" in custom.replies[0].content
    taken = asking(stack, guild, guild.members[1])
    await invoke(cog.link, cog, taken, steam=ANNA, nick="Anna")
    assert taken.replies[0].content.startswith("That Steam ID is linked to another member")
    (note,) = await notes(stack)
    assert note.startswith("Powtórzony Steam ID: <@1>") and "<@2>" in note  # the staff log speaks pl


async def test_link_without_both_opens_the_modal_and_unlink_removes(stack, guild):
    cog = LinkCog(stack)
    anna = person(guild, 1, "anna_dc", RANK)
    opened = asking(stack, guild, anna)
    await invoke(cog.link, cog, opened, steam=ANNA, nick=None)
    modal = opened.response.modals[0]
    assert modal.steam.default == ANNA
    modal.nick._value, modal.steam._value = "Anna", ANNA
    await modal.on_submit(asking(stack, guild, anna))
    assert (await stack.links.get(1)).nick == "Anna"
    gone = asking(stack, guild, anna)
    await invoke(cog.unlink, cog, gone)
    assert gone.replies[0].content.startswith("The Steam link is removed")
    nothing = asking(stack, guild, anna)
    await invoke(cog.unlink, cog, nothing)
    assert nothing.replies[0].content == "There is no Steam link to remove."


async def test_staff_set_and_remove_anyones_link(stack, guild):
    cog = LinkCog(stack)
    bob = person(guild, 2, "bob_dc", RANK)
    member = asking(stack, guild, person(guild, 1, "anna_dc", RANK))
    await invoke(cog.set_link, cog, member, member=bob, steam=BOB, nick="Bob")
    assert member.replies[0].content == "You don't have the permission for this."
    staff = person(guild, 9, "staff_dc", LINKS_ROLE)
    done = asking(stack, guild, staff)
    await invoke(cog.set_link, cog, done, member=bob, steam=BOB, nick="Bob")
    assert done.replies[0].content.startswith(f"Linked: Steam ID {BOB}") and "squad.xml is public" not in \
        done.replies[0].content  # the notice is for the member themselves
    assert (await stack.links.get(2)).linked_by == 9
    removed = asking(stack, guild, staff)
    await invoke(cog.remove_link, cog, removed, member=bob)
    assert await stack.links.get(2) is None
    assert any("<@9>" in n and BOB in n for n in await notes(stack))


async def test_link_now_opens_the_modal_with_the_current_link(stack, guild):
    anna = person(guild, 1, "anna_dc", RANK)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    pressed = asking(stack, guild, anna)
    await ui.LinkNow().callback(pressed)
    modal = pressed.response.modals[0]
    assert (modal.steam.default, modal.nick.default) == (ANNA, "Anna")


# ---------------------------------------------------------------------------- reminders

async def test_the_first_run_announces_once_and_dms_start_n_days_later(stack, guild, clock):
    person(guild, 1, "anna_dc", RANK)
    person(guild, 2, "bob_dc", RANK)
    await stack.links.link(2, BOB, "Bob", 2, has_rank=True)
    await stack.link_watch.start()
    (announcement,) = guild.get_channel(REMINDERS).messages
    assert announcement.kwargs["view"].children[0].custom_id == "link:now"
    assert await notes(stack) == ["Start linków Steam: 1 członek bez linku dostanie DM za 14 dni, jeśli wcześniej "
                                  "się nie połączy."]
    await stack.link_watch.start()  # a restart: no second announcement
    assert len(guild.get_channel(REMINDERS).messages) == 1
    assert stack.squadxml.path.is_file() and BOB in stack.squadxml.path.read_text(encoding="utf-8")
    await stack.scheduler.run_due()
    assert guild.members[1].dms == []  # reminded by the announcement just now
    clock.now += timedelta(days=15)
    await stack.scheduler.run_due()
    assert guild.members[1].dms and guild.members[1].dms[0].kwargs["view"].children[0].custom_id == "link:now"
    assert guild.members[2].dms == []  # linked


async def test_gaining_a_rank_reminds_at_once_and_closed_dms_ping_the_channel(stack, guild):
    await stack.links.mark_rollout([])
    anna = person(guild, 1, "anna_dc", RANK)
    await stack.link_watch.member_changed(anna, frozenset({MEMBER}))
    assert len(anna.dms) == 1
    bob = person(guild, 2, "bob_dc", RANK)
    bob.dms_closed = True
    await stack.link_watch.member_changed(bob, frozenset())
    (ping,) = guild.get_channel(REMINDERS).messages
    assert ping.content.startswith("<@2> ") and ping.kwargs["view"].children[0].custom_id == "link:now"


async def test_losing_the_rank_drops_out_of_squad_xml_and_the_link_goes_after_n_days(stack, guild, clock):
    anna = person(guild, 1, "anna_dc", RANK)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    await stack.link_watch.start()
    assert ANNA in stack.squadxml.path.read_text(encoding="utf-8")
    anna.roles = [FakeRole(MEMBER)]
    await stack.link_watch.member_changed(anna, frozenset({RANK}))
    await stack.squadxml.settle()
    assert ANNA not in stack.squadxml.path.read_text(encoding="utf-8")
    await notes(stack)
    clock.now += timedelta(days=91)
    await stack.scheduler.run_due()
    assert await stack.links.get(1) is None
    assert any("po 90 dniach bez rangi" in n for n in await notes(stack))
    assert await stack.scheduler.exists(KIND)  # hourly, for good


async def test_a_first_slot_without_a_link_asks_for_it(stack, guild):
    from app.signup.service import Outcome
    from app.signup.ui import answer

    anna = person(guild, 1, "anna_dc", RANK)
    first = asking(stack, guild, anna)
    await answer(first, Outcome(), "You have the slot.", ask_link=True)
    assert "Link your Steam account" in first.replies[0].content
    assert first.replies[0].kwargs["view"].children[0].custom_id == "link:now"
    second = asking(stack, guild, anna)
    await answer(second, Outcome(), "You have the slot.", ask_link=True)
    assert second.replies[0].content == "You have the slot." and "view" not in second.replies[0].kwargs


# ---------------------------------------------------------------------------- managing squad.xml

def staff_asking(stack, guild) -> FakeInteraction:
    return asking(stack, guild, guild.members.get(999) or person(guild, 999, "staff_dc", LINKS_ROLE))


async def test_an_own_remark_goes_before_the_roles_or_instead_of_them(stack, guild):
    anna = person(guild, 1, "anna_dc", RANK, PILOT)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    cog = LinkCog(stack)
    asked = staff_asking(stack, guild)
    await invoke(cog.remark, cog, asked, member=anna, text="  Dowódca  ")
    assert asked.replies[0].content == "The remark of <@1> in squad.xml: Dowódca, Pilot"
    await stack.squadxml.settle()
    assert "<remark>Dowódca, Pilot</remark>" in stack.squadxml.path.read_text(encoding="utf-8")
    assert any("Dowódca" in n and "<@999>" in n for n in await notes(stack))
    only = staff_asking(stack, guild)
    await invoke(cog.remark, cog, only, member=anna, text="Dowódca", only=True)
    assert only.replies[0].content.endswith(": Dowódca")
    await stack.links.link(1, ANNA, "Anna K", 1, has_rank=True)  # a relink keeps it
    cleared = staff_asking(stack, guild)
    await invoke(cog.remark, cog, cleared, member=anna)
    assert cleared.replies[0].content.endswith(": Pilot")
    link = await stack.links.get(1)
    assert (link.nick, link.remark, link.remark_only) == ("Anna K", "", False)
    long = staff_asking(stack, guild)
    await invoke(cog.remark, cog, long, member=anna, text="x" * 129)
    assert long.replies[0].content == "Give a remark of at most 128 characters."
    unlinked = staff_asking(stack, guild)
    await invoke(cog.remark, cog, unlinked, member=person(guild, 2, "bob_dc", RANK), text="x")
    assert unlinked.replies[0].content == "<@2> has no Steam link, so no squad.xml entry."


async def test_status_shows_the_file_what_is_missing_and_write_now_writes(stack, guild):
    person(guild, 1, "anna_dc", RANK)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    (stack.squadxml.path.parent / "squad.dtd").write_text("", encoding="utf-8")
    cog = LinkCog(stack)
    before = staff_asking(stack, guild)
    await invoke(cog.status, cog, before)
    (shown,) = before.replies
    fields = {f.name: f.value for f in shown.kwargs["embed"].fields}
    assert fields["Members in it"] == "1" and fields["Written"] == "Never" and fields["Up to date"] == "No: press Write now."
    assert fields["Missing next to it (kept by hand)"] == "squad.xsl, logo.paa"
    assert fields["Address"] == "https://example.org/squad.xml" and "file" not in shown.kwargs
    button = shown.kwargs["view"].children[0]
    assert button.custom_id == "squadxml:write"
    refused = asking(stack, guild, person(guild, 2, "bob_dc", RANK))
    await ui.WriteNow().callback(refused)
    assert refused.replies[0].content == "You don't have the permission for this." and not stack.squadxml.path.exists()
    pressed = staff_asking(stack, guild)
    await ui.WriteNow().callback(pressed)
    assert pressed.replies[0].content == "squad.xml written."
    again = staff_asking(stack, guild)
    await ui.WriteNow().callback(again)
    assert again.replies[0].content == "squad.xml was up to date: nothing to write."
    after = staff_asking(stack, guild)
    await invoke(cog.status, cog, after)
    fields = {f.name: f.value for f in after.replies[0].kwargs["embed"].fields}
    assert fields["Up to date"] == "Yes" and fields["Written"].startswith("<t:")
    assert after.replies[0].kwargs["file"].filename == "squad.xml"


async def test_a_failed_write_shows_in_the_status_until_one_succeeds(stack, guild, monkeypatch):
    from app.squadxml import service

    person(guild, 1, "anna_dc", RANK)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)

    def locked(path, text):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(service, "_write", locked)
    pressed = staff_asking(stack, guild)
    await ui.WriteNow().callback(pressed)
    assert pressed.replies[0].content == "squad.xml could not be written: [WinError 5] Access is denied"
    assert (await stack.squadxml.status()).error == "[WinError 5] Access is denied"
    monkeypatch.undo()
    assert await stack.squadxml.write_now() is True and (await stack.squadxml.status()).error is None


async def test_the_lists_for_staff(stack, guild, clock):
    person(guild, 1, "anna_dc", RANK)
    person(guild, 2, "bob_dc", MEMBER)
    person(guild, 3, "cid_dc", RANK)
    person(guild, 4, "dan_dc", RANK)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    await stack.links.link(2, BOB, "Bob", 2, has_rank=False)
    await stack.links.set_remark(1, "Dowódca", only=True)
    await stack.links.reminded([3])
    await stack.links.mute(4, True)
    cog = LinkCog(stack)

    async def shown(show: str) -> str:
        asked = staff_asking(stack, guild)
        await invoke(cog.list_links, cog, asked, show=show)
        embed = asked.replies[0].kwargs["embed"]
        return f"{embed.title}\n{embed.description}"

    assert await shown("in_file") == f"In squad.xml (1)\n<@1> · Anna · {ANNA} · Dowódca"
    unlinked = await shown("unlinked")
    assert unlinked.startswith("A rank, no link (2)\n<@3> · reminded 1 time, last <t:")
    assert unlinked.endswith("<@4> · reminders off")
    goes = clock.now + timedelta(days=90)
    assert await shown("unranked") == f"Linked, no rank (1)\n<@2> · Bob · {BOB} · the link goes <t:{int(goes.timestamp())}:R>"
    assert await shown("muted") == "Reminders off (1)\n<@4>"
    assert await shown("remarks") == "Own remark (1)\n<@1> · Anna · Dowódca · instead of the roles"


async def test_a_long_list_comes_as_a_file_too(stack, guild):
    for n in range(1, 200):
        person(guild, n, f"member_{n:03}", RANK)
    asked = staff_asking(stack, guild)
    cog = LinkCog(stack)
    await invoke(cog.list_links, cog, asked, show="unlinked")
    reply_ = asked.replies[0]
    assert len(reply_.kwargs["embed"].description) <= 4002 and reply_.kwargs["embed"].description.endswith("…")
    listed = reply_.kwargs["file"].fp.read().decode("utf-8-sig")
    assert "member_199 (199) · not reminded yet" in listed and len(listed.splitlines()) == 199


async def test_remind_now_and_reminders_off(stack, guild):
    await stack.links.mark_rollout([])
    cog = LinkCog(stack)
    cid = person(guild, 3, "cid_dc", RANK)
    sent = staff_asking(stack, guild)
    await invoke(cog.remind, cog, sent, member=cid)
    assert sent.replies[-1].content == "Reminder sent to <@3> by DM." and len(cid.dms) == 1
    assert (await stack.links.reminders())[3].count == 1
    linked = person(guild, 1, "anna_dc", RANK)
    await stack.links.link(1, ANNA, "Anna", 1, has_rank=True)
    already = staff_asking(stack, guild)
    await invoke(cog.remind, cog, already, member=linked)
    assert already.replies[0].content == "<@1> is linked already."
    no_rank = staff_asking(stack, guild)
    await invoke(cog.remind, cog, no_rank, member=person(guild, 2, "bob_dc", MEMBER))
    assert no_rank.replies[0].content == "<@2> has no rank role, so squad.xml doesn't need them."

    dan = person(guild, 4, "dan_dc", MEMBER)
    off = staff_asking(stack, guild)
    await invoke(cog.reminders, cog, off, member=dan, on=False)
    assert off.replies[0].content.startswith("Reminders for <@4> are off")
    dan.roles = [FakeRole(RANK)]
    await stack.link_watch.member_changed(dan, frozenset({MEMBER}))  # gaining the rank: no DM
    assert dan.dms == []
    assert await stack.links.due_reminders({3, 4}, timedelta(0), 10) == [3]
    assert await stack.links.ask_at_signup(4) is False
    on = staff_asking(stack, guild)
    await invoke(cog.reminders, cog, on, member=dan, on=True)
    assert await stack.links.due_reminders({4}, timedelta(0), 10) == [4]
    await stack.links.mute(4, True)
    await stack.links.link(4, BOB, "Dan", 4, has_rank=True)  # linking ends it
    assert not await stack.links.muted(4)


def test_the_startup_check_names_a_missing_squad_xml_folder(guild, tmp_path):
    from app.startup_checks import check

    settings = make_settings(tmp_path, squad_xml={"output_dir": tmp_path / "missing"})
    _, problems = check(guild, settings)
    assert any("BOT_SQUAD_XML__OUTPUT_DIR" in p for p in problems)
    assert any("BOT_ROLES__RANK" in p for p in problems)


async def test_one_failed_reminder_doesnt_stop_the_others_and_is_tried_again(stack, guild, clock, monkeypatch):
    from types import SimpleNamespace

    import discord

    await stack.links.mark_rollout([])
    clock.now += timedelta(days=15)
    closed = person(guild, 1, "anna_dc", RANK)
    closed.dms_closed = True  # (so the reminder channel is pinged, and it refuses)
    open_ = person(guild, 2, "bob_dc", RANK)

    async def refused(*_args, **_kwargs):
        raise discord.HTTPException(SimpleNamespace(status=429, reason="Too Many Requests"), "rate limited")

    monkeypatch.setattr(guild.get_channel(REMINDERS), "send", refused)
    await stack.link_watch._hourly(None, clock.now)
    assert len(open_.dms) == 1
    reminders = await stack.links.reminders()
    assert 2 in reminders and 1 not in reminders  # asked again next hour
