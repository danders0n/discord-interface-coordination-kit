import discord
from conftest import (ADMIN, ANNOUNCE, ANNOUNCEMENT, DRAFTS, EVENTS, GUILD, HEADER, KADET, MEMBER, MISSION_MAKER,
                      MODERATION, MODERATOR, STAFF)
from fakes import FakeChannel, FakeGuild, FakeMember, FakeRole

from app.startup_checks import check

CATEGORY = discord.ChannelType.category


def healthy_guild() -> FakeGuild:
    channels = {DRAFTS: FakeChannel(DRAFTS, "Szkice", CATEGORY), EVENTS: FakeChannel(EVENTS, "Eventy", CATEGORY)}
    for channel_id, name in ((HEADER, "eventy"), (ANNOUNCE, "ogloszenia"), (MODERATION, "moderacja"),
                             (STAFF, "staff-log")):
        channels[channel_id] = FakeChannel(channel_id, name)
    roles = {r: FakeRole(r, f"role{r}") for r in (MEMBER, MISSION_MAKER, KADET, ANNOUNCEMENT, ADMIN, MODERATOR)}
    return FakeGuild(GUILD, channels=channels, roles=roles)


def test_a_healthy_server_has_no_problems(settings):
    assert check(healthy_guild(), settings) == []


def test_missing_and_wrong_channels(settings):
    guild = healthy_guild()
    del guild.channels[STAFF]
    guild.channels[DRAFTS] = FakeChannel(DRAFTS, "szkice")
    problems = check(guild, settings.model_copy(update={"language": "en"}))
    assert any(p.startswith("BOT_CHANNELS__STAFF_LOG: no channel") for p in problems)
    assert any(p.startswith("BOT_CHANNELS__DRAFTS_CATEGORY: szkice is not a category") for p in problems)


def test_missing_permissions_are_named(settings):
    guild = healthy_guild()
    allowed = discord.Permissions.all()
    allowed.update(manage_roles=False, create_public_threads=False)
    guild.channels[EVENTS].permissions = allowed
    (problem,) = check(guild, settings.model_copy(update={"language": "en"}))
    assert "manage_roles" in problem and "create_public_threads" in problem and "Eventy" in problem


def test_missing_roles_and_an_unmentionable_announcement_role(settings):
    guild = healthy_guild()
    del guild.roles[MODERATOR]
    guild.roles[ANNOUNCEMENT].mentionable = False
    problems = check(guild, settings.model_copy(update={"language": "en"}))
    assert any(p.startswith("BOT_PERMISSIONS__APPROVE_EVENTS: no role") for p in problems)
    assert any("can't mention" in p for p in problems)
    guild.me = FakeMember(1, guild_permissions=discord.Permissions(mention_everyone=True))
    assert not any("can't mention" in p for p in check(guild, settings))


def test_problems_follow_the_public_language(settings):
    guild = healthy_guild()
    del guild.channels[STAFF]
    (problem,) = check(guild, settings)  # settings.language is pl
    assert problem.startswith("BOT_CHANNELS__STAFF_LOG:") and "no channel" not in problem
