"""`/server` against the fake supervisor (docs/PLAN.md, *Supervisor: server control*)."""

import asyncio
from datetime import timedelta

import discord
from conftest import ADMIN, MEMBER, MISSION_MAKER, MODERATOR
from fakes import FakeInteraction, FakeMember, FakeRole, invoke

from app.reservations.service import Window
from app.servers.cog import Confirm, Override, ServerCog

ENGLISH = discord.Locale.british_english


def member(*roles: int) -> FakeMember:
    return FakeMember(42, "anna", roles=[FakeRole(r) for r in roles])


async def notes(services) -> list[str]:
    await asyncio.sleep(0)
    return services.staff.drain()


async def test_autocomplete_lists_the_supervisors_servers(services, fake):
    cog = ServerCog(services)
    interaction = FakeInteraction(services, member(ADMIN), ENGLISH)
    choices = await cog.server_choices(interaction, "eve")
    assert [(c.name, c.value) for c in choices] == [("Event (event)", "event")]
    assert len(await cog.server_choices(interaction, "")) == 2


async def test_start_answers_and_goes_to_the_staff_log(services, fake):
    cog = ServerCog(services)
    interaction = FakeInteraction(services, member(MISSION_MAKER), ENGLISH)
    await invoke(cog.start, cog, interaction, server="server-1")
    assert interaction.replies[0].content == "Starting server-1."
    assert fake.server("server-1")["state"] == "running"
    (note,) = await notes(services)
    assert "/server start na server-1" in note and "mimo" not in note


async def test_members_without_the_role_are_refused(services, fake):
    cog = ServerCog(services)
    interaction = FakeInteraction(services, member(MEMBER), ENGLISH)
    await invoke(cog.start, cog, interaction, server="server-1")
    assert interaction.replies[0].content == "You don't have the permission for this."
    say = FakeInteraction(services, member(ADMIN), ENGLISH)  # server control, but not server message
    await invoke(cog.say, cog, say, server="server-1", message="hi")
    assert say.replies[0].content == "You don't have the permission for this."


async def test_stop_asks_first_and_shows_the_players(services, fake):
    fake.server("server-1").update(state="running", players_online=[{"name": "a"}, {"name": "b"}])
    cog = ServerCog(services)
    interaction = FakeInteraction(services, member(ADMIN), ENGLISH)
    await invoke(cog.stop, cog, interaction, server="server-1")
    question = interaction.replies[0]
    assert question.content == "Stop server-1? 2 players are on it."
    view = question.kwargs["view"]
    assert isinstance(view, Confirm) and fake.server("server-1")["state"] == "running"
    click = FakeInteraction(services, member(ADMIN), ENGLISH)
    await view.go.callback(click)
    assert fake.server("server-1")["state"] == "stopped"
    assert click.response.edited == [{"view": None}] and click.replies[0].content == "Stopping server-1."


async def test_cancelling_the_question_does_nothing(services, fake):
    fake.server("server-1")["state"] = "running"
    cog = ServerCog(services)
    interaction = FakeInteraction(services, member(ADMIN), ENGLISH)
    await invoke(cog.restart, cog, interaction, server="server-1")
    assert interaction.replies[0].content == "Restart server-1? 0 players are on it."
    click = FakeInteraction(services, member(ADMIN), ENGLISH)
    await interaction.replies[0].kwargs["view"].no.callback(click)
    assert click.response.edited[0]["content"] == "Nothing was done." and await notes(services) == []


async def test_a_reservation_refuses_and_an_override_starts_anyway(services, fake, clock):
    start = clock.now + timedelta(minutes=10)  # in the quiet period of the event's reservation
    await services.reservations.book(7, Window("18-10-ibc Op", start, start + timedelta(hours=3)))
    cog = ServerCog(services)
    plain = FakeInteraction(services, member(MISSION_MAKER), ENGLISH)
    await invoke(cog.start, cog, plain, server="server-1")
    refused = plain.replies[0]
    assert refused.content.startswith("Refused: the reservation 18-10-ibc Op holds the host until <t:")
    assert "view" not in refused.kwargs  # a mission maker has no override
    admin = FakeInteraction(services, member(ADMIN), ENGLISH)
    await invoke(cog.start, cog, admin, server="server-1")
    view = admin.replies[0].kwargs["view"]
    assert isinstance(view, Override)
    click = FakeInteraction(services, member(ADMIN), ENGLISH)
    await view.anyway.callback(click)
    assert fake.server("server-1")["state"] == "running"
    assert any("mimo rezerwacji" in n for n in await notes(services))


async def test_say_reaches_the_server_and_quotes_the_message(services, fake):
    fake.server("event")["state"] = "running"
    cog = ServerCog(services)
    interaction = FakeInteraction(services, member(MODERATOR), ENGLISH)
    await invoke(cog.say, cog, interaction, server="event", message="Briefing in 5 minutes")
    assert interaction.replies[0].content == "Message sent to event."
    (note,) = await notes(services)
    assert note.endswith("> Briefing in 5 minutes")


async def test_the_supervisors_refusal_is_shown(services, fake):
    cog = ServerCog(services)
    interaction = FakeInteraction(services, member(MODERATOR), discord.Locale.polish)
    await invoke(cog.say, cog, interaction, server="event", message="hi")
    assert interaction.replies[0].content.endswith("event is not running")
