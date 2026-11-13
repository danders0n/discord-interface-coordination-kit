"""The supervisor's refusal codes for operations in the member's words (docs/PLAN-front-end.md, *Acting for members*,
*Refusals*). A code the bot doesn't know is shown as the supervisor's own message (``discord_util.supervisor_words``)."""

from __future__ import annotations

from typing import Any

from app.i18n import t


def refusal(code: str | None, body: Any, locale: Any) -> str | None:
    """Words for ``code`` (a refusal's ``body`` gives the details some codes carry), or None for a code not listed."""
    body = body if isinstance(body, dict) else {}
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
    }
    return words.get(code or "")
