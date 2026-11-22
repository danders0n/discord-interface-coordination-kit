"""Members' actions queued while the supervisor is away (app/queued/, docs/PLAN-remote-update.md, step 5): queued
while it updates or when the call never reached it, collapsed, sent in order once it is back with the answer in place
of the *Queued* reply or by DM, refusals worded, expiry; never a write that may have gone through."""

from __future__ import annotations

from datetime import timedelta

import discord
import pytest
from fakes import FakeInteraction
from sqlalchemy import select
from test_channel import run, stack  # noqa: F401  (stack: the fixture)
from test_signup import acting, front, press, published  # noqa: F401  (front: the fixture)

from app.db.models import QueuedAction
from app.health import SupervisorHealth
from app.queued.service import Queue
from app.queued.ui import QUEUED, Teller, act_or_queue
from app.signup.ui import OfferAction, SignupAction
from app.supervisor_client import SupervisorError, SupervisorUnreachable


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def queued(front, guild, clock):
    s = front
    s.health = SupervisorHealth(s.supervisor, s.staff.post, lambda: "en", clock)
    s.queue = Queue(s.db, s.supervisor, s.roster, s.store, clock, s.health)
    s.teller_clock = Clock()

    async def fetch(user_id):
        return guild.members[user_id]

    s.queue.tell = Teller(fetch, s.teller_clock)
    yield s


def updating(s, eta_s: int = 300) -> None:
    s.health.system({"state": "restarting", "kind": "update", "to": "f" * 40, "eta_s": eta_s})


def back(s) -> None:
    s.health._set("up")


async def rows(s) -> list[QueuedAction]:
    async with s.db.session() as session:
        return list((await session.scalars(select(QueuedAction).order_by(QueuedAction.id))).all())


def signup_of(fake, op, discord_id: int):
    person = fake.persons[str(discord_id)]["id"]
    return next((e for e in fake.operations[op["id"]]["signups"] if e["person_id"] == person), None)


def last_edit(interaction) -> str:
    return interaction.original_edits[-1]["content"]


async def test_a_press_while_the_supervisor_updates_is_queued_and_sent_when_it_is_back(queued, guild, fake):
    op = published(fake)
    await run(queued)
    updating(queued, eta_s=240)
    maybe = acting(queued, guild, 1)
    await press(SignupAction(op["id"], "maybe"), maybe)
    assert maybe.replies[-1].content == \
        "Queued: the app is updating and should be back in about 4 min. You'll get the answer here."
    assert maybe.replies[-1].kwargs.get("ephemeral") is True
    assert signup_of(fake, op, 1) is None  # nothing reached the supervisor
    [row] = await rows(queued)
    assert (row.method, row.args, row.state, row.locale) == ("answer", {"args": [op["id"], "maybe"]}, "queued", "en")

    back(queued)
    assert await queued.queue.flush() == 1
    assert signup_of(fake, op, 1)["status"] == "maybe"
    assert last_edit(maybe) == "Sent now that the app is back: Noted."  # (in place of the Queued reply)
    assert (await rows(queued))[0].state == "sent"


async def test_only_the_last_sign_up_choice_counts(queued, guild, fake):
    op = published(fake)
    fake.sign(op["id"], 1, status="maybe")  # (signed up in the app before)
    await run(queued)
    updating(queued)
    first, second, third = (acting(queued, guild, 1) for _ in range(3))
    await press(SignupAction(op["id"], "maybe"), first)
    await press(SignupAction(op["id"], "reserve"), second)
    await press(SignupAction(op["id"], "leave"), third)
    assert last_edit(first) == "Replaced by your later choice." and last_edit(second) == "Replaced by your later choice."
    assert [r.state for r in await rows(queued)] == ["replaced", "replaced", "queued"]
    back(queued)
    assert await queued.queue.flush() == 1
    assert signup_of(fake, op, 1) is None
    assert last_edit(third) == "Sent now that the app is back: You are off this operation."


async def test_a_refused_connection_is_queued_but_a_timeout_never(queued, guild, fake, monkeypatch):
    op = published(fake)
    await run(queued)
    url = queued.supervisor.url
    queued.supervisor.url = "http://127.0.0.1:1"  # nothing listens: the request never left
    refused = acting(queued, guild, 1)
    await press(SignupAction(op["id"], "maybe"), refused)
    assert refused.replies[-1].content.startswith("Queued: the app can't be reached right now.")
    assert queued.health.state == "down"
    queued.supervisor.url = url

    async def timed_out(discord_id, call):
        raise SupervisorUnreachable("did not answer in time", sent=None)

    await queued.queue.flush()  # (still down: nothing goes)
    assert [r.state for r in await rows(queued)] == ["queued"]
    back(queued)
    await queued.queue.flush()
    assert signup_of(fake, op, 1)["status"] == "maybe"

    monkeypatch.setattr(queued.roster, "as_member", timed_out)
    maybe = acting(queued, guild, 2)
    await press(SignupAction(op["id"], "maybe"), maybe)
    assert maybe.replies[-1].content == "The app can't be reached right now. Try again in a few minutes."
    assert len(await rows(queued)) == 1  # (it may have gone through: never queued, never sent twice)


async def test_a_member_with_queued_actions_waits_behind_them(queued, guild, fake):
    op = published(fake)
    await run(queued)
    updating(queued)
    await press(SignupAction(op["id"], "maybe"), acting(queued, guild, 1))
    back(queued)
    other = acting(queued, guild, 2)
    await press(SignupAction(op["id"], "maybe"), other)  # someone else: straight through
    assert other.replies[-1].content == "Noted."
    late = acting(queued, guild, 1)
    await act_or_queue(late, "accept_offer", op["id"], 99, done="Taken.")  # Anna's next write: behind her first
    assert late.replies[-1].content.startswith("Queued")
    await queued.queue.flush()
    assert [r.method for r in await rows(queued) if r.state != "queued"] == ["answer", "accept_offer"]


async def test_a_refusal_at_sending_is_worded(queued, guild, fake):
    op = published(fake)
    await run(queued)
    updating(queued)
    leave = acting(queued, guild, 3)
    await press(SignupAction(op["id"], "leave"), leave)
    fake.persons["3"]["permissions"] = []  # Cid may no longer sign up, by the time it is back
    back(queued)
    await queued.queue.flush()
    assert last_edit(leave) == "Not done now that the app is back: You don't have the permission for this."
    assert (await rows(queued))[0].state == "failed"


async def test_a_stale_organiser_edit_repeats_what_was_typed(queued):
    row = QueuedAction(locale="en", method="set_opord")
    error = SupervisorError(409, "stale", {"code": "stale", "detail": "stale"})
    text = queued.queue.refusal(row, error, [7, {"situation": "Rain all night", "mission": "Hold the bridge"}, 4])
    assert text.startswith("Not saved: the operation changed while the app was away.")
    assert "What you sent:\nRain all night\nHold the bridge" in text


async def test_an_old_action_is_not_sent(queued, guild, fake, clock):
    op = published(fake)
    await run(queued)
    updating(queued)
    maybe = acting(queued, guild, 1)
    await press(SignupAction(op["id"], "maybe"), maybe)
    clock.now += timedelta(minutes=61)
    assert await queued.queue.expire() == 1
    assert last_edit(maybe) == "Not sent: the app was away too long. Press again."
    back(queued)
    assert await queued.queue.flush() == 0 and signup_of(fake, op, 1) is None


async def test_after_fifteen_minutes_the_answer_is_a_dm(queued, guild, fake):
    op = published(fake)
    await run(queued)
    updating(queued)
    maybe = acting(queued, guild, 1)
    await press(SignupAction(op["id"], "maybe"), maybe)
    queued.teller_clock.now += 15 * 60  # Discord no longer lets the bot edit that reply
    back(queued)
    await queued.queue.flush()
    assert maybe.original_edits == []
    assert guild.members[1].dms[-1].content == "Sent now that the app is back: Noted."


async def test_a_reply_that_cant_be_edited_becomes_a_dm(queued, guild):
    class Gone(FakeInteraction):
        async def edit_original_response(self, **kwargs):
            raise discord.NotFound(type("R", (), {"status": 404, "reason": "gone"})(), "Unknown Webhook")

    gone = Gone(queued, guild.members[2], guild=guild)
    queued.queue.tell.remember(5, gone)
    await queued.queue.tell(5, 2, "Sent now that the app is back: Noted.")
    assert guild.members[2].dms[-1].content == "Sent now that the app is back: Noted."


async def test_too_many_waiting_is_refused(queued, guild, fake):
    queued.store.current = queued.settings.model_copy(update={"queue": queued.settings.queue.model_copy(
        update={"per_member": 1})})
    updating(queued)
    first, second = acting(queued, guild, 1), acting(queued, guild, 1)
    assert await act_or_queue(first, "rate", 1, 5, "", done="Saved.") is QUEUED
    assert await act_or_queue(second, "rate", 2, 5, "", done="Saved.") is QUEUED
    assert second.replies[-1].content == \
        "Too many of your actions are waiting for the app. Try again when it is back."
    assert len(await rows(queued)) == 1


async def test_an_offer_taken_while_the_supervisor_updates_waits(queued, guild, fake):
    updating(queued)
    take = acting(queued, guild, 1)
    await press(OfferAction(5, 9, "take"), take)
    [row] = await rows(queued)
    assert (row.method, row.args["args"], row.collapse) == ("accept_offer", [5, 9], "offer:9")


async def test_only_listed_methods_can_be_queued(queued):
    with pytest.raises(ValueError):
        await queued.queue.add(1, "erase_member", [1], "", "en")


async def test_the_queue_wakes_when_the_supervisor_is_back(queued):
    updating(queued)
    queued.queue._wake.clear()
    back(queued)
    assert queued.queue._wake.is_set()
