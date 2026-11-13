"""The supervisor's refusal codes for operations in the member's words (docs/PLAN-front-end.md, *Acting for members*,
*Refusals*). A code the bot doesn't know is shown as the supervisor's own message (``discord_util.supervisor_words``)."""

from __future__ import annotations

from typing import Any

from app.i18n import t
from app.operations import times


def time_problem(problem: times.Problem, locale: Any) -> str:
    """What an organiser typed in the wizard or the panel, refused before anything is sent."""
    return {
        times.Problem.BAD_DATE: t("Type the date as dd.mm or dd.mm.yyyy.", locale),
        times.Problem.PAST: t("That date has passed.", locale),
        times.Problem.TOO_FAR: t("That date is too far ahead.", locale),
        times.Problem.BAD_TIME: t("Type times as HH:MM.", locale),
        times.Problem.BAD_DURATION: t("Type the duration as minutes or like 3h or 2h30 (15 min to 24 h).", locale),
    }[problem]


def missing(items: list[Any], locale: Any) -> str:
    """``incomplete``'s list (``opord.situation``, ``how_to_join``) in words."""
    names = {"opord.situation": t("the OPORD's Situation", locale), "opord.mission": t("the OPORD's Mission", locale),
             "how_to_join": t("How to join", locale)}
    return t("Still missing: {items}.", locale, items=", ".join(names.get(str(i), str(i)) for i in items))


def refusal(code: str | None, body: Any, locale: Any) -> str | None:
    """Words for ``code`` (a refusal's ``body`` gives the details some codes carry), or None for a code not listed."""
    body = body if isinstance(body, dict) else {}
    if code == "incomplete":
        return missing(list(body.get("missing") or []), locale)
    if code == "overlap":
        other = body.get("other") if isinstance(body.get("other"), dict) else {}
        return t("You already have a place in {title} ({key}) at that time.", locale,
                 title=other.get("title") or "?", key=other.get("key") or "?")
    if code == "bad_orbat":
        problems = [str(p) for p in body.get("problems") or []][:10]
        return t("The ORBAT has problems:", locale) + "\n" + "\n".join(f"- {p}" for p in problems)
    words = {
        "not_published": t("The operation isn't published yet.", locale),
        "not_open": t("Sign-up isn't open yet.", locale),
        "closed": t("Sign-up has closed.", locale),
        "not_allowed": t("You don't have the permission for this.", locale),
        "no_person": t("Your app account isn't tied to a Discord member.", locale),
        "wrong_kind": t("That doesn't work for this operation.", locale),
        "no_slot": t("That slot no longer exists.", locale),
        "taken": t("Someone was faster: that slot is taken.", locale),
        "offered": t("That slot is being offered to the reserve list.", locale),
        "locked": t("That slot is locked by the organisers.", locale),
        "role": t("That slot needs a role you don't have.", locale),
        "full": t("The operation is full.", locale),
        "not_full": t("There is a free slot for you: take it instead of the reserve list.", locale),
        "already": t("You are there already.", locale),
        "not_signed_up": t("You aren't signed up.", locale),
        "offer_gone": t("That offer is no longer open.", locale),
        "not_pending": t("This request was decided meanwhile.", locale),
        "own_request": t("Nobody decides their own request.", locale),
        "not_found": t("That no longer exists.", locale),
        # organising
        "stale": t("The operation changed meanwhile; open it again to see the change.", locale),
        "pending": t("The operation waits for approval; withdraw the request to change it.", locale),
        "needs_approval": t("Publishing needs approval: press Send for approval.", locale),
        "publish_instead": t("This one needs no approval: press Publish.", locale),
        "kind_not_allowed": t("Only mission makers and above make guest operations.", locale),
        "needs_host": t("A guest operation needs its host group.", locale),
        "unknown_climate": t("That climate is not in the list.", locale),
        "not_draft": t("That works only for a draft.", locale),
        "started": t("The operation has started.", locale),
        "past": t("That time is over: the assembly must be in the future.", locale),
        "start_before_assembly": t("The start is before the assembly.", locale),
        "signup_after_start": t("Sign-up must open before the operation starts.", locale),
        "too_far": t("That date is too far ahead.", locale),
        "frozen_times": t("The operation is about to begin: its times can't move now.", locale),
        "group_exists": t("Another group has that acronym.", locale),
        "exists": t("Another group has that acronym.", locale),
        "name_taken": t("A template of that name exists already.", locale),
        "not_pbo": t("Send the mission's .pbo file.", locale),
        "not_an_image": t("Send a PNG, JPG or WEBP image.", locale),
        "bad_preset": t("That file is not an Arma 3 Launcher preset.", locale),
        "too_large": t("That file is too large.", locale),
        "unknown_mission": t("There is no such mission on the server.", locale),
        # attendance, ratings, erasure
        "not_started": t("The operation hasn't started yet: cancel it instead.", locale),
        "no_report": t("There is no attendance report yet.", locale),
        "rating_closed": t("Ratings for this operation have closed.", locale),
        "not_attended": t("Only those who attended rate the mission.", locale),
        "bad_stars": t("Give 1 to 5 stars.", locale),
        "self": t("Nobody erases their own account here: ask another admin.", locale),
        "erased": t("That member was erased already.", locale),
        "forbidden": t("You don't have the permission for this.", locale),
    }
    return words.get(code or "")
