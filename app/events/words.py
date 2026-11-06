"""The services' refusal codes in words, in the member's language (the services themselves speak no language)."""

from __future__ import annotations

from typing import Any

from app.events import times
from app.events.service import Problem
from app.i18n import t
from app.orbat.templates import Problem as TemplateProblem
from app.signup.service import Refusal


def event_problem(problem: Problem, locale: Any, missing: list[str] | None = None) -> str:
    if problem == Problem.INCOMPLETE:
        names = {"briefing": t("the briefing", locale), "orbat": t("the ORBAT", locale),
                 "how_to_join": t("How to join", locale)}
        return t("Still missing: {items}.", locale, items=", ".join(names[m] for m in missing or ()))
    return {
        Problem.NOT_FOUND: t("This event no longer exists.", locale),
        Problem.NOT_ALLOWED: t("You don't have the permission for this.", locale),
        Problem.KIND_NOT_ALLOWED: t("Only mission makers and above make guest events.", locale),
        Problem.NEEDS_HOST: t("A guest event needs its host group.", locale),
        Problem.UNKNOWN_CLIMATE: t("That climate is not in the list.", locale),
        Problem.NOT_DRAFT: t("That works only for a draft.", locale),
        Problem.PENDING: t("The event waits for approval; withdraw the request to change it.", locale),
        Problem.TIMES_LOCKED: t("The server's reservation has begun: the times can't change any more.", locale),
        Problem.STARTED: t("The event has started (or the time is in the past).", locale),
        Problem.NOT_STARTED: t("The event hasn't started yet.", locale),
        Problem.NOT_PENDING: t("Nothing waits for approval.", locale),
        Problem.NO_RESERVATION: t("The server isn't reserved for this time; resolve the clash and retry the reservation first.", locale),
        Problem.TOO_LONG: t("That is too long.", locale),
        Problem.NOT_ZEUS: t("That works only for a Zeus or training event.", locale),
        Problem.NOT_A_NUMBER: t("Type a whole number, or leave it empty.", locale),
        Problem.NOT_AN_IMAGE: t("Send a PNG, JPG, WEBP or GIF image.", locale),
    }[problem]


def time_problem(problem: times.Problem, locale: Any) -> str:
    return {
        times.Problem.BAD_DATE: t("Type the date as dd.mm or dd.mm.yyyy.", locale),
        times.Problem.PAST: t("That date has passed.", locale),
        times.Problem.TOO_FAR: t("That date is too far ahead.", locale),
        times.Problem.BAD_TIME: t("Type times as HH:MM.", locale),
        times.Problem.BAD_DURATION: t("Type the duration as minutes or like 3h or 2h30 (15 min to 24 h).", locale),
    }[problem]


def template_problem(problem: TemplateProblem, locale: Any) -> str:
    return {
        TemplateProblem.NOT_FOUND: t("That template no longer exists.", locale),
        TemplateProblem.NOT_ALLOWED: t("Only its author and event managers change that template.", locale),
        TemplateProblem.NAME_TAKEN: t("A template of that name exists already.", locale),
        TemplateProblem.BAD_NAME: t("Give the template a name of 1 to 100 characters.", locale),
    }[problem]


def refusal(code: Refusal, locale: Any, other_channel: int | None = None) -> str:
    if code == Refusal.OVERLAP:
        where = f" <#{other_channel}>" if other_channel else ""
        return t("You are signed up for another event at that time.", locale) + where
    return {
        Refusal.NOT_PUBLISHED: t("The event isn't published yet.", locale),
        Refusal.NOT_OPEN: t("Sign-up isn't open yet.", locale),
        Refusal.CLOSED: t("Sign-up has closed.", locale),
        Refusal.NOT_ALLOWED: t("Sign-up is for members.", locale),
        Refusal.WRONG_KIND: t("That doesn't work for this event.", locale),
        Refusal.NO_SLOT: t("That slot no longer exists.", locale),
        Refusal.TAKEN: t("Someone was faster: that slot is taken.", locale),
        Refusal.OFFERED: t("That slot is being offered to the reserve list.", locale),
        Refusal.LOCKED: t("That slot is locked by the organisers.", locale),
        Refusal.ROLE: t("That slot needs a role you don't have.", locale),
        Refusal.FULL: t("The event is full.", locale),
        Refusal.NOT_FULL: t("There is a free slot for you: take it instead of the reserve list.", locale),
        Refusal.ALREADY: t("You are there already.", locale),
        Refusal.NOT_SIGNED_UP: t("You aren't signed up.", locale),
        Refusal.OFFER_GONE: t("That offer is no longer open.", locale),
    }[code]
