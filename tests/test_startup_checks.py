import discord
from conftest import ANNOUNCEMENT, DRAFTS, EVENTS, MEMBER, MODERATOR, STAFF, make_settings
from fakes import FakeChannel, FakeMember, FakeRole

from app.resolve import resolve
from app.startup_checks import check


def english(settings, **overrides):
    return settings.model_copy(update={"language": "en", **overrides})


def test_a_healthy_server_has_no_problems(guild, settings):
    ids, problems = check(guild, settings)
    assert problems == []
    assert ids.staff == STAFF and ids.member == {MEMBER} and ids.permissions["approve_events"] == {MODERATOR}


def test_names_work_like_ids_and_ignore_case(guild, tmp_path):
    settings = make_settings(tmp_path, channels={"drafts_category": "szkice", "events_category": "Eventy",
                                                 "header": "#eventy", "announcements": "ogloszenia",
                                                 "staff": "STAFF"},
                             roles={"member": ["klanowicz"], "mission_maker": "@Mission Maker",
                                    "signup": ["Klanowicz", "Kadet"]})
    ids, problems = resolve(guild, settings)
    assert problems == []
    assert ids.drafts_category == DRAFTS and ids.staff == STAFF and ids.member == {MEMBER}


def test_the_top_channels_put_the_header_first_unless_listed(guild, tmp_path):
    from conftest import ANNOUNCE, HEADER

    guild.add(FakeChannel(997, "instrukcja-strefy"))
    base = {"drafts_category": DRAFTS, "events_category": EVENTS, "header": HEADER, "announcements": ANNOUNCE,
            "staff": STAFF}
    ids, problems = resolve(guild, make_settings(tmp_path, channels=base | {"top": ["Instrukcja-strefy", ANNOUNCE]}))
    assert problems == [] and ids.top == (HEADER, 997, ANNOUNCE)
    ids, _ = resolve(guild, make_settings(tmp_path, channels=base | {"top": [997, ANNOUNCE, HEADER]}))
    assert ids.top == (997, ANNOUNCE, HEADER)
    ids, problems = resolve(guild, make_settings(tmp_path, language="en", channels=base | {"top": ["gone"]}))
    assert ids.top == (HEADER,) and problems == ["BOT_CHANNELS__TOP: gone not found on this server"]


def test_missing_ambiguous_and_wrong_kind(guild, tmp_path):
    guild.add(FakeRole(999, "Klanowicz"))
    guild.add(FakeChannel(998, "Eventy"))  # a text channel named like the category: not a clash for a category
    settings = make_settings(tmp_path, language="en",
                             channels={"drafts_category": "eventy", "events_category": EVENTS, "header": DRAFTS,
                                       "announcements": "nowhere", "staff": STAFF},
                             roles={"member": ["Klanowicz"], "mission_maker": 12345})
    ids, problems = resolve(guild, settings)
    assert ids.drafts_category == EVENTS  # matched the category, not the text channel
    assert "BOT_CHANNELS__HEADER: Szkice is not a text channel" in problems
    assert "BOT_CHANNELS__ANNOUNCEMENTS: nowhere not found on this server" in problems
    assert any(p.startswith("BOT_ROLES__MEMBER: more than one is called Klanowicz") and "999" in p for p in problems)
    assert "BOT_ROLES__MISSION_MAKER: 12345 not found on this server" in problems
    assert ids.member == frozenset() and ids.header is None


def test_missing_permissions_are_named(guild, settings):
    allowed = discord.Permissions.all()
    allowed.update(manage_roles=False, create_public_threads=False)
    guild.get_channel(EVENTS).permissions = allowed
    no_threads = discord.Permissions.all()
    no_threads.update(create_public_threads=False)
    guild.get_channel(STAFF).permissions = no_threads
    _, problems = check(guild, english(settings))
    assert len(problems) == 2
    assert "manage_roles" in problems[0] and "create_public_threads" in problems[0] and "Eventy" in problems[0]
    assert problems[1].startswith("BOT_CHANNELS__STAFF:") and "create_public_threads" in problems[1]


def test_an_unmentionable_announcement_role(guild, settings):
    guild.get_role(ANNOUNCEMENT).mentionable = False
    _, problems = check(guild, english(settings))
    assert any("can't mention" in p for p in problems)
    guild.me = FakeMember(1, guild_permissions=discord.Permissions(mention_everyone=True))
    assert check(guild, settings)[1] == []


def test_an_unmentionable_climate_role(guild, settings):
    guild.add(FakeRole(401, "Cold War"))
    guild.get_role(401).mentionable = False
    settings = english(settings, events=settings.events.model_copy(update={"climate_roles": [401]}))
    (problem,) = check(guild, settings)[1]
    assert "can't mention Cold War" in problem


def test_no_sign_up_role_is_a_problem(guild, tmp_path):
    settings = make_settings(tmp_path, language="en", roles={"member": [MEMBER], "mission_maker": MEMBER, "signup": []})
    (problem,) = check(guild, settings)[1]
    assert problem.startswith("BOT_ROLES__SIGNUP names no role")


def test_problems_follow_the_public_language(guild, tmp_path):
    settings = make_settings(tmp_path, channels={"drafts_category": DRAFTS, "events_category": EVENTS,
                                                 "header": "brak", "announcements": "ogloszenia", "staff": STAFF})
    (problem,) = check(guild, settings)[1]  # language is pl
    assert problem == "BOT_CHANNELS__HEADER: nie znaleziono „brak” na tym serwerze"
