"""The profile service's codes in words (docs/PLAN.md, *Supervisor: event profile*), for the panel and the staff
log alike."""

from __future__ import annotations

from typing import Any

from app.i18n import t
from app.profiles.service import Problem


def problem(code: Problem, locale: Any, message: str = "") -> str:
    if code == Problem.SUPERVISOR:
        return t("The supervisor refused: {message}", locale, message=message or "—")
    return {
        Problem.NOT_ALLOWED: t("The server profile is for organisers with the mission maker role.", locale),
        Problem.NOT_RESERVED: t("A guest event runs on the host group's server: no profile here.", locale),
        Problem.NO_MISSION: t("Set the mission first: /event mission.", locale),
        Problem.NOT_PBO: t("Send the mission as a .pbo file.", locale),
        Problem.NAME_IN_USE: t("Another profile uses a mission of that file name: rename your file (e.g. _v2) and "
                               "send it again.", locale),
        Problem.UNKNOWN_MISSION: t("The server has no mission of that name.", locale),
        Problem.NO_BASE: t("The base profile (BOT_SUPERVISOR__BASE_PROFILE) is not set or not found; tell staff.",
                           locale),
        Problem.BAD_NUMBER: t("Max players: a whole number from 1 to 256, or empty for the default.", locale),
    }[code]


def warning(code: str, detail: str, locale: Any) -> str:
    return {
        "missing_dlc": t("⚠ The mission needs Creator DLC the preset lacks: {names}", locale, names=detail),
        "missing_mods": t("⚠ The mission needs mods the preset seems to lack: {names}", locale, names=detail),
        "no_preset": t("⚠ The mission needs mods, but there is no preset: {names}", locale, names=detail),
        "few_slots": t("⚠ Fewer playable slots than ORBAT slots: {counts}", locale, counts=detail),
        "mission_error": t("⚠ The supervisor found errors in the mission: {errors}", locale, errors=detail),
        "index_incomplete": t("The supervisor is still indexing the addons: the mod warnings may be incomplete.",
                              locale),
    }[code]


def state(event: Any, locale: Any) -> str:
    if event.profile_state == "built":
        return t("Built.", locale)
    if event.profile_state == "failed":
        return t("Failed: {reason}", locale, reason=event.profile_error or "—")
    if event.profile_state == "building":
        progress = event.profile_progress or {}
        if progress.get("status") == "held":
            return t("Waiting: the supervisor holds jobs during a reservation.", locale)
        total, pending = progress.get("items_total"), progress.get("items_pending")
        if total:
            return t("Downloading mods: {done} of {total}.", locale, done=total - (pending or 0), total=total)
        return t("Being built.", locale)
    return t("Not built yet.", locale)
