"""The Discord side of queued actions (docs/PLAN-remote-update.md, *Queued actions*): :func:`act_or_queue`, used by
the presses and modals whose write may wait for the supervisor, and :class:`Teller`, which gives the member the
answer later: in place of their *Queued* reply while Discord still lets the bot edit it (15 minutes), by DM after.

The interactions are kept in memory only (their tokens are credentials, and they expire in 15 minutes anyway): after
a restart of the bot, the answer is a DM."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import discord

from app.discord_util import NO_MENTIONS, reply, services
from app.i18n import language_of, t
from app.supervisor_client import SupervisorUnreachable

log = logging.getLogger(__name__)
QUEUED = object()  # what act_or_queue gives when the write waits (the member was told)
EDITABLE = 14 * 60  # seconds: Discord lets an interaction's reply be edited for 15 minutes


async def act_or_queue(interaction: discord.Interaction, method: str, *args: Any, done: str,
                       collapse: str | None = None) -> Any:
    """``client.<method>(member, *args)`` in the name of whoever pressed (``Roster.as_member``); while the supervisor
    is away, or when the call never reached it, the write is queued instead, the member is told, and this gives
    :data:`QUEUED`. ``done``: what to tell them once it went through (in their language)."""
    s = services(interaction)
    member = interaction.user.id
    call: Callable[[int], Awaitable[Any]] = lambda me: getattr(s.supervisor, method)(me, *args)  # noqa: E731
    queue = getattr(s, "queue", None)
    if queue is None:
        return await s.roster.as_member(member, call)
    if not await queue.must_wait(member):
        try:
            return await s.roster.as_member(member, call)
        except SupervisorUnreachable as exc:
            if exc.sent is not False:  # (it may have gone through: never sent twice)
                raise
    row_id, replaced = await queue.add(member, method, list(args), done, language_of(interaction.locale), collapse)
    locale = interaction.locale
    if row_id is None:
        await reply(interaction, t("Too many of your actions are waiting for the app. Try again when it is back.",
                                   locale))
        return QUEUED
    queue.wake()  # (the supervisor may be up, with only this member's earlier rows waiting)
    health = getattr(s, "health", None)
    left = health.minutes_left() if health is not None else None
    text = (t("Queued: the app is updating and should be back in about {minutes} min. You'll get the answer here.",
              locale, minutes=left) if left else
            t("Queued: the app can't be reached right now. It goes through once the app is back, and you'll get the "
              "answer here.", locale))
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    else:
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=NO_MENTIONS)
    teller = getattr(queue, "tell", None)
    if isinstance(teller, Teller):
        teller.remember(row_id, interaction)
        for old in replaced:
            await teller(old, member, t("Replaced by your later choice.", locale))
    return QUEUED


class Teller:
    """``tell`` for :class:`app.queued.service.Queue`: edits the member's *Queued* reply, or sends a DM."""

    def __init__(self, fetch_user: Callable[[int], Awaitable[Any]], clock: Callable[[], float] = time.monotonic) -> None:
        self.fetch_user, self.clock = fetch_user, clock
        self.live: dict[int, tuple[Any, float]] = {}

    def remember(self, row_id: int, interaction: Any) -> None:
        now = self.clock()
        self.live = {k: v for k, v in self.live.items() if now - v[1] < EDITABLE}  # (forget the expired ones)
        self.live[row_id] = (interaction, now)

    async def __call__(self, row_id: int, discord_id: int, text: str) -> None:
        found = self.live.pop(row_id, None)
        if found is not None and self.clock() - found[1] < EDITABLE:
            try:
                await found[0].edit_original_response(content=text[:2000], view=None)
                return
            except discord.HTTPException as exc:
                log.info("Could not edit the queued reply of row %s (%s): a DM instead", row_id, exc)
        try:
            user = await self.fetch_user(discord_id)
            await user.send(text[:2000], allowed_mentions=NO_MENTIONS)
        except discord.HTTPException as exc:  # (DMs closed, or the member left)
            log.info("Could not tell member %s about queued row %s: %s", discord_id, row_id, exc)
