import discord
from conftest import ADMIN, ANNOUNCEMENT, EVENTS, OTHER_CATEGORY, STAFF, make_settings
from fakes import FakeChannel, FakeMember, FakeRole

from app.resolve import resolve
from app.startup_checks import check


def english(settings, **overrides):
    return settings.model_copy(update={"language": "en", **overrides})


def test_a_healthy_server_has_no_problems(guild, settings):
    ids, problems = check(guild, settings)
    assert problems == []
    assert ids.staff == STAFF and ids.announcement == ANNOUNCEMENT and ids.permissions["bot_admin"] == {ADMIN}


def test_names_work_like_ids_and_ignore_case(guild, tmp_path):
    settings = make_settings(tmp_path, channels={"events_category": "Eventy",
                                                 "header": "#eventy", "announcements": "ogloszenia",
                                                 "staff": "STAFF"},
                             roles={"announcement": "@ogloszenia"}, permissions={"bot_admin": ["admin"]})
    ids, problems = resolve(guild, settings)
    assert problems == []
    assert ids.events_category == EVENTS and ids.staff == STAFF and ids.announcement == ANNOUNCEMENT
    assert ids.permissions["bot_admin"] == {ADMIN}


def test_the_top_channels_put_the_header_first_unless_listed(guild, tmp_path):
    from conftest import ANNOUNCE, HEADER

    guild.add(FakeChannel(997, "instrukcja-strefy"))
    base = {"events_category": EVENTS, "header": HEADER, "announcements": ANNOUNCE,
            "staff": STAFF}
    ids, problems = resolve(guild, make_settings(tmp_path, channels=base | {"top": ["Instrukcja-strefy", ANNOUNCE]}))
    assert problems == [] and ids.top == (HEADER, 997, ANNOUNCE)
    ids, _ = resolve(guild, make_settings(tmp_path, channels=base | {"top": [997, ANNOUNCE, HEADER]}))
    assert ids.top == (997, ANNOUNCE, HEADER)
    ids, problems = resolve(guild, make_settings(tmp_path, language="en", channels=base | {"top": ["gone"]}))
    assert ids.top == (HEADER,) and problems == ["BOT_CHANNELS__TOP: gone not found on this server"]


def test_missing_ambiguous_and_wrong_kind(guild, tmp_path):
    guild.add(FakeRole(999, "Admin"))
    guild.add(FakeChannel(998, "Eventy"))  # a text channel named like the category: not a clash for a category
    settings = make_settings(tmp_path, language="en",
                             channels={"events_category": "eventy", "header": OTHER_CATEGORY,
                                       "announcements": "nowhere", "staff": STAFF},
                             roles={"announcement": 12345}, permissions={"bot_admin": ["Admin"]})
    ids, problems = resolve(guild, settings)
    assert ids.events_category == EVENTS  # matched the category, not the text channel
    assert "BOT_CHANNELS__HEADER: Szkice is not a text channel" in problems
    assert "BOT_CHANNELS__ANNOUNCEMENTS: nowhere not found on this server" in problems
    assert any(p.startswith("BOT_PERMISSIONS__BOT_ADMIN: more than one is called Admin") and "999" in p
               for p in problems)
    assert "BOT_ROLES__ANNOUNCEMENT: 12345 not found on this server" in problems
    assert ids.permissions["bot_admin"] == frozenset() and ids.header is None


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


def test_problems_follow_the_public_language(guild, tmp_path):
    settings = make_settings(tmp_path, channels={"events_category": EVENTS,
                                                 "header": "brak", "announcements": "ogloszenia", "staff": STAFF})
    (problem,) = check(guild, settings)[1]  # language is pl
    assert problem == "BOT_CHANNELS__HEADER: nie znaleziono „brak” na tym serwerze"
