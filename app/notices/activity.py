"""Staff-log lines from the supervisor's ``activity`` notices (docs/PLAN-front-end.md, *Notices*).

The supervisor sends what happened as data (its ``app/operations/activity.py``): ``event``, ``by`` (a person
``{name, discord_id}``, None for the supervisor itself), the operation's ``key`` and ``title``, and the event's own
fields. :func:`describe` words it in the config language, one line per thing that changed, each starting with the
time it happened: persons as mentions (the staff log silences them) or ``@name``, the operation by its title and
channel, times as Discord timestamps. An event this module doesn't know, or one missing a field, is posted as the
supervisor's English ``text``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.channel import render
from app.i18n import t

COMING = ("zeus", "training")
TIMES = ("assembly_at", "start_at", "signup_opens_at")
BOOLEANS = ("dont_end_automatically", "public", "verify_signatures", "profile_keep")


class _Line:
    """What every wording needs: the language, the operation as shown, who did it."""

    def __init__(self, data: dict[str, Any], language: str, channel_id: int | None, kinds: dict[str, str]) -> None:
        self.data, self.language, self.kinds = data, language, kinds
        title = render.plain(data.get("title") or data.get("key") or "?")
        self.operation = f"**{title}** (<#{channel_id}>)" if channel_id else f"**{title}**"

    def __getitem__(self, name: str) -> Any:
        return self.data[name]

    def get(self, name: str, default: Any = None) -> Any:
        return self.data.get(name, default)

    @property
    def by(self) -> str:
        return self.who(self.data.get("by"), t("the supervisor", self.language))

    def who(self, person: dict[str, Any] | None, nobody: str = "—") -> str:
        if not person:
            return nobody
        discord_id = str(person.get("discord_id") or "")
        return f"<@{discord_id}>" if discord_id.isdigit() else f"@{render.plain(person.get('name') or '?')}"

    def persons(self, people: list[dict[str, Any]] | None) -> str:
        return ", ".join(self.who(p) for p in people or []) or "—"

    def slots(self, names: list[str] | None) -> str:
        return ", ".join(render.plain(n) for n in names or []) or "—"

    def when(self, value: str | None, style: str = "f") -> str:
        return render.ts(datetime.fromisoformat(value), style) if value else "—"

    def text(self, value: str | None) -> str:
        return render.plain(value) if value else "—"

    def kind(self, kind: str) -> str:
        return render.plain(self.kinds.get(kind) or render.kind_name(kind, None, self.language))


# ---------------------------------------------------------------- the operation


def _created(x: _Line) -> list[str]:
    return [t("{by} created {operation} ({kind}, assembly {when}).", x.language, by=x.by, operation=x.operation,
              kind=x.kind(x["kind"]), when=x.when(x["assembly_at"]))]


def _copied(x: _Line) -> list[str]:
    return [t("{by} created {operation} as a copy of {source} ({kind}, assembly {when}).", x.language, by=x.by,
              operation=x.operation, source=x.text(x["copied_from"]), kind=x.kind(x["kind"]),
              when=x.when(x["assembly_at"]))]


def _label(field: str, language: str) -> str:
    return {"kind": t("Kind", language), "title": t("Title", language), "climate": t("Played preset", language),
            "host_group_id": t("Host group", language), "group_ids": t("Invited groups", language),
            "author_person_id": t("Author", language), "author_name": t("Author", language),
            "specialist_roles": t("Specialist roles", language), "assembly_at": t("Assembly", language),
            "start_at": t("Start", language), "duration_minutes": t("Duration", language),
            "signup_opens_at": t("Sign-up opens", language), "signup_limit": t("Sign-up limit", language),
            "how_to_join": t("How to join", language), "dont_end_automatically": t("Don't end automatically", language),
            "public": t("Public", language), "hold": t("Hold the host", language),
            "server_id": t("Event server", language), "base_profile_id": t("Base profile", language),
            "note": t("Note", language), "hostname": t("Hostname", language),
            "max_players": t("Max players", language), "verify_signatures": t("Verify signatures", language),
            "profile_keep": t("Keep the server profile", language)}.get(field, field)


def _value(x: _Line, field: str, value: Any) -> str:
    language = x.language
    if field in TIMES:
        return x.when(value) if value else (t("at publishing", language) if field == "signup_opens_at" else "—")
    if field == "duration_minutes" and value is not None:
        return render.duration(int(value), language)
    if field == "kind" and value:
        return x.kind(value)
    if field == "author_person_id":
        return x.who(value)
    if field == "specialist_roles":
        return render.roles_text(value or [], ", ") or "—"
    if field in BOOLEANS or isinstance(value, bool):
        return t("yes", language) if value else t("no", language)
    if field == "hold":
        return {"on": t("yes", language), "off": t("no", language),
                "requested": t("requested", language)}.get(value, x.text(value))
    if field in ("signup_limit", "max_players") and not value:
        return t("none", language)
    if isinstance(value, list):
        return ", ".join(render.plain(str(v)) for v in value) or "—"
    return x.text(str(value)) if value is not None else "—"


def _changed(x: _Line) -> list[str]:
    out = []
    for change in x["changes"]:
        field, what = change["field"], _label(change["field"], x.language)
        if change.get("quiet"):
            out.append(t("{by} changed {what} in {operation}.", x.language, by=x.by, what=what, operation=x.operation))
        else:
            out.append(t("{by} changed {what} in {operation}: {before} → {after}.", x.language, by=x.by, what=what,
                         operation=x.operation, before=_value(x, field, change.get("old")),
                         after=_value(x, field, change.get("new"))))
    return out


def _opord(x: _Line) -> list[str]:
    names = ", ".join(render.heading(p, x.language) for p in x["paragraphs"])
    return [t("{by} changed the OPORD of {operation}: {paragraphs}.", x.language, by=x.by, operation=x.operation,
              paragraphs=names)]


def _organisers(x: _Line) -> list[str]:
    added = [t("{by} made {member} a co-organiser of {operation}.", x.language, by=x.by, member=x.who(p),
               operation=x.operation) for p in x.get("added") or []]
    removed = [t("{by} removed {member} from the co-organisers of {operation}.", x.language, by=x.by, member=x.who(p),
                 operation=x.operation) for p in x.get("removed") or []]
    return added + removed


def _file(x: _Line) -> list[str]:
    language, slot, action = x.language, x["slot"], x["action"]
    fields = {"by": x.by, "operation": x.operation}
    if slot == "preset" and action == "set":
        return [t("{by} uploaded the preset {name} for {operation}.", language, name=x.text(x.get("name")), **fields)]
    wordings = {("preset", "removed"): t("{by} removed the preset of {operation}.", language, **fields),
                ("map", "set"): t("{by} uploaded the map of {operation}.", language, **fields),
                ("map", "removed"): t("{by} removed the map of {operation}.", language, **fields),
                ("banner", "set"): t("{by} uploaded the banner of {operation}.", language, **fields),
                ("banner", "removed"): t("{by} removed the banner of {operation}.", language, **fields)}
    return [wordings[(slot, action)]]


def _mission(x: _Line) -> list[str]:
    language, fields = x.language, {"by": x.by, "operation": x.operation, "name": x.text(x.get("name"))}
    return [{"upload": t("{by} uploaded the mission {name} for {operation}.", language, **fields),
             "pick": t("{by} set the mission of {operation}: {name}.", language, **fields),
             "cleared": t("{by} removed the mission of {operation}.", language, **fields)}[x["action"]]]


def _published(x: _Line) -> list[str]:
    if x.get("signup_open"):
        return [t("{by} published {operation}; sign-up is open.", x.language, by=x.by, operation=x.operation)]
    return [t("{by} published {operation}.", x.language, by=x.by, operation=x.operation)]


def _simple(key: str) -> Callable[[_Line], list[str]]:
    """A line of only who and which operation (``key``: one of the literal texts below)."""
    def word(x: _Line) -> list[str]:
        texts = {
            "submitted": t("{by} sent {operation} for approval.", x.language, by=x.by, operation=x.operation),
            "withdrawn": t("{by} withdrew {operation} from approval; it is a draft again.", x.language, by=x.by,
                           operation=x.operation),
            "profile_build": t("{by} started building the server profile of {operation}.", x.language, by=x.by,
                               operation=x.operation),
            "profile_built": t("The server profile of {operation} is built.", x.language, operation=x.operation),
            "signup_opened": t("Sign-up for {operation} is open.", x.language, operation=x.operation),
            "signup_closed": t("Sign-up for {operation} is closed.", x.language, operation=x.operation),
        }
        return [texts[key]]
    return word


def _cancelled(x: _Line) -> list[str]:
    return [t("{by} cancelled {operation}. Reason: {reason}", x.language, by=x.by, operation=x.operation,
              reason=x.text(x.get("reason")))]


def _deleted(x: _Line) -> list[str]:
    return [t("{by} deleted the draft {operation}. Reason: {reason}", x.language, by=x.by, operation=x.operation,
              reason=x.text(x.get("reason")))]


def _ended(x: _Line) -> list[str]:
    if x.get("by"):
        return [t("{by} ended {operation}.", x.language, by=x.by, operation=x.operation)]
    if x.get("how") == "finished":
        return [t("{operation} ended: the mission finished.", x.language, operation=x.operation)]
    return [t("{operation} ended: its time is over.", x.language, operation=x.operation)]


def _extended(x: _Line) -> list[str]:
    return [t("{by} extended {operation} until {until}.", x.language, by=x.by, operation=x.operation,
              until=x.when(x["until"], "t"))]


# ---------------------------------------------------------------- the ORBAT and sign-ups


def _displaced(x: _Line, line: str) -> list[str]:
    if x.get("displaced"):
        line += " " + t("Their slot is gone, now at the top of the reserve list: {members}", x.language,
                        members=x.persons(x["displaced"]))
    return [line]


def _orbat_set(x: _Line) -> list[str]:
    return _displaced(x, t(("{by} set the ORBAT of {operation}: {count} slot.",
                            "{by} set the ORBAT of {operation}: {count} slots."), x.language, by=x.by,
                           operation=x.operation, count=int(x["slots"])))


def _orbat_cleared(x: _Line) -> list[str]:
    return _displaced(x, t("{by} removed the ORBAT of {operation}.", x.language, by=x.by, operation=x.operation))


def _took(x: _Line) -> list[str]:
    fields = {"member": x.who(x["person"]), "slot": x.text(x["slot"]), "operation": x.operation}
    if x.get("from_slot"):
        line = t("{member} moved from {before} to {slot} in {operation}.", x.language, before=x.text(x["from_slot"]),
                 **fields)
    else:
        line = t("{member} took {slot} in {operation}.", x.language, **fields)
    if x.get("from_offer"):
        line += " " + t("(from an offer)", x.language)
    return [line]


def _left(x: _Line) -> list[str]:
    if x.get("slot"):
        return [t("{member} left {slot} in {operation}.", x.language, member=x.who(x["person"]),
                  slot=x.text(x["slot"]), operation=x.operation)]
    return [t("{member} left the sign-up of {operation}.", x.language, member=x.who(x["person"]), operation=x.operation)]


def _list_name(x: _Line, status: str) -> str:
    language = x.language
    return {"maybe": t("Maybe", language), "reserve": t("Reserve list", language), "coming": t("Coming", language),
            "cant": t("Not coming", language) if x.get("kind") in COMING else t("Can't come", language)}[status]


def _answered(x: _Line) -> list[str]:
    member, status = x.who(x["person"]), x["status"]
    if status == "reserve":
        line = t("{member} joined the reserve list of {operation}.", x.language, member=member, operation=x.operation)
    else:
        line = t("{member} answered {answer} for {operation}.", x.language, member=member,
                 answer=_list_name(x, status), operation=x.operation)
    if status == "coming" and x.get("role"):
        line += " " + t("(as {role})", x.language, role=x.text(x["role"]))
    if x.get("left_slot"):
        line += " " + t("(gave up {slot})", x.language, slot=x.text(x["left_slot"]))
    return [line]


def _placed(x: _Line) -> list[str]:
    member = x.who(x["person"])
    if not x.get("status"):
        return [t("{by} took {member} off the lists of {operation}.", x.language, by=x.by, member=member,
                  operation=x.operation)]
    return [t("{by} put {member} on the list {list} in {operation}.", x.language, by=x.by, member=member,
              list=_list_name(x, x["status"]), operation=x.operation)]


def _offer_made(x: _Line) -> list[str]:
    slot = x.slots(x["slots"])
    if not x.get("person"):
        return [t("{slot} in {operation} offered to the whole reserve list until {until}.", x.language, slot=slot,
                  operation=x.operation, until=x.when(x.get("until"), "t"))]
    return [t("{slot} in {operation} offered to {member}.", x.language, slot=slot, operation=x.operation,
              member=x.who(x["person"]))]


def _offer_declined(x: _Line) -> list[str]:
    return [t("{member} declined {slot} in {operation}.", x.language, member=x.who(x["person"]),
              slot=x.slots(x["slots"]), operation=x.operation)]


def _offer_expired(x: _Line) -> list[str]:
    return [t("The offer of {slot} in {operation} expired.", x.language, slot=x.slots(x["slots"]),
              operation=x.operation)]


def _locked(x: _Line) -> list[str]:
    fields = {"by": x.by, "slot": x.text(x["slot"]), "operation": x.operation}
    if not x["locked"]:
        return [t("{by} unlocked {slot} in {operation}.", x.language, **fields)]
    if x.get("person"):
        return [t("{by} locked {member} in {slot} in {operation}.", x.language, member=x.who(x["person"]), **fields)]
    return [t("{by} locked {slot} in {operation} closed.", x.language, **fields)]


def _assigned(x: _Line) -> list[str]:
    return [t("{by} put {member} in {slot} in {operation}.", x.language, by=x.by, member=x.who(x["person"]),
              slot=x.text(x["slot"]), operation=x.operation)]


def _removed(x: _Line) -> list[str]:
    return [t("{by} took {member} out of {slot} in {operation}.", x.language, by=x.by, member=x.who(x["person"]),
              slot=x.text(x["slot"]), operation=x.operation)]


def _freed(x: _Line) -> list[str]:
    return [t("Places freed in {operation} (left the server or may no longer sign up): {members}", x.language,
              operation=x.operation, members=x.persons(x["persons"]))]


# ---------------------------------------------------------------- attendance, links, accounts


def _corrected(x: _Line) -> list[str]:
    return [t("{by} corrected the attendance of {operation}: {members} → {status}.", x.language, by=x.by,
              operation=x.operation, members=x.persons(x["persons"]),
              status=render.status_name(x["status"], x.language))]


def _confirmed(x: _Line) -> list[str]:
    return [t(("{operation}: {count} player still to confirm after {days} days now counts as attended.",
               "{operation}: {count} players still to confirm after {days} days now count as attended."), x.language,
              operation=x.operation, count=int(x["count"]), days=x["days"])]


def _rated(x: _Line) -> list[str]:
    if x.get("again"):
        return [t("{member} changed their rating of {operation}.", x.language, member=x.who(x["person"]),
                  operation=x.operation)]
    return [t("{member} rated {operation}.", x.language, member=x.who(x["person"]), operation=x.operation)]


def _link_removed(x: _Line) -> list[str]:
    fields = {"by": x.by, "member": x.who(x["person"]), "steam": x.text(x.get("steam_uid"))}
    why = x.get("why")
    if why == "removed by the member":
        return [t("{member} removed their Steam link ({steam}).", x.language, **fields)]
    if why == "rejected":
        return [t("{by} rejected the Steam link of {member} ({steam}).", x.language, **fields)]
    return [t("{by} removed the Steam link of {member} ({steam}).", x.language, **fields)]


def _renamed(x: _Line) -> list[str]:
    return [t("The app account of {member} was made as {account}: the name {wanted} belongs to another account.",
              x.language, member=x.who(x["person"]), account=x.text(x["account"]), wanted=x.text(x["wanted"]))]


WORDINGS: dict[str, Callable[[_Line], list[str]]] = {
    "operation.created": _created, "operation.copied": _copied, "operation.changed": _changed,
    "operation.opord": _opord, "operation.organisers": _organisers, "operation.file": _file,
    "operation.mission": _mission, "operation.published": _published, "operation.submitted": _simple("submitted"),
    "operation.withdrawn": _simple("withdrawn"), "operation.cancelled": _cancelled, "operation.deleted": _deleted,
    "operation.ended": _ended, "operation.extended": _extended, "operation.profile_build": _simple("profile_build"),
    "operation.profile_built": _simple("profile_built"), "operation.signup_opened": _simple("signup_opened"),
    "operation.signup_closed": _simple("signup_closed"),
    "orbat.set": _orbat_set, "orbat.cleared": _orbat_cleared, "slot.took": _took, "slot.left": _left,
    "signup.answered": _answered, "signup.placed": _placed, "signup.freed": _freed, "offer.made": _offer_made,
    "offer.declined": _offer_declined, "offer.expired": _offer_expired, "slot.locked": _locked,
    "slot.assigned": _assigned, "slot.removed": _removed,
    "attendance.corrected": _corrected, "attendance.confirmed": _confirmed, "attendance.rated": _rated,
    "link.removed": _link_removed, "account.renamed": _renamed,
}


def describe(data: dict[str, Any], language: str, *, channel_id: int | None = None, at: datetime | None = None,
             kinds: dict[str, str] | None = None) -> str:
    """The staff-log text of an ``activity`` notice's ``data`` (empty: nothing to say). ``at``: when it happened;
    ``kinds``: the supervisor's kind names (``meta``)."""
    wording = WORDINGS.get(str(data.get("event") or ""))
    lines: list[str] = []
    if wording is not None:
        try:
            lines = wording(_Line(data, language, channel_id, kinds or {}))
        except (KeyError, TypeError, ValueError):  # a field missing or of another shape: the supervisor's own words
            lines = []
    if not lines:
        text = str(data.get("text") or "").strip()
        lines = [render.plain(text)] if text else []
    stamp = f"{render.ts(at, 'T')} " if at is not None else ""
    return "\n".join(stamp + line for line in lines)
