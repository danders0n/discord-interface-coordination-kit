"""Staff-log lines from the supervisor's ``activity`` notices (app/notices/activity.py): each event worded in the
config language, persons as mentions or names, the channel link, and the supervisor's English text as the fallback."""

from datetime import UTC, datetime

from app.notices.activity import WORDINGS, describe

ANNA = {"name": "anna", "discord_id": "11"}
MM = {"name": "mm", "discord_id": None}
OP = {"key": "OP-7", "title": "Zeus night"}
AT = datetime(2026, 11, 20, 18, 30, tzinfo=UTC)
NINETEEN, TWENTY, DAY_BEFORE = "2026-11-21T19:00:00+00:00", "2026-11-21T20:00:00+00:00", "2026-11-20T19:00:00+00:00"


def ts(iso: str, style: str = "f") -> str:
    return f"<t:{int(datetime.fromisoformat(iso).timestamp())}:{style}>"


def line(event: str, language: str = "en", channel_id: int | None = 55, **fields) -> str:
    return describe({"event": event, **OP, **fields}, language, channel_id=channel_id, kinds={"zeus": "Zeus"})


def test_the_operation_and_who():
    created = line("operation.created", by=MM, kind="zeus", assembly_at=NINETEEN, hold="off")
    assert created == f"@mm created **Zeus night** (<#55>) (Zeus, assembly {ts(NINETEEN)})."
    assert line("operation.created", "pl", by=ANNA, kind="zeus", assembly_at=NINETEEN) == \
        f"<@11> tworzy **Zeus night** (<#55>) (Zeus, zbiórka {ts(NINETEEN)})."
    assert line("operation.signup_opened", "pl", channel_id=None, by=None) == "Zapisy na **Zeus night** są otwarte."
    assert line("operation.ended", by=None, how="time") == "**Zeus night** (<#55>) ended: its time is over."
    assert line("operation.ended", "pl", by=MM, how="manual") == "@mm kończy **Zeus night** (<#55>)."
    stamped = describe({"event": "operation.published", **OP, "by": ANNA}, "en", at=AT)
    assert stamped == f"{ts(AT.isoformat(), 'T')} <@11> published **Zeus night**."


def test_an_edit_is_one_line_per_detail():
    text = line("operation.changed", "pl", by=MM, changes=[
        {"field": "title", "old": "Zeus", "new": "Zeus night"},
        {"field": "start_at", "old": NINETEEN, "new": TWENTY},
        {"field": "signup_opens_at", "old": None, "new": DAY_BEFORE},
        {"field": "duration_minutes", "old": 120, "new": 150},
        {"field": "public", "old": True, "new": False},
        {"field": "note", "old": None, "new": None, "quiet": True},
        {"field": "something_new", "old": "a", "new": "b"}])
    assert text.split("\n") == [
        "@mm zmienia „Tytuł” w **Zeus night** (<#55>): Zeus → Zeus night.",
        f"@mm zmienia „Start” w **Zeus night** (<#55>): {ts(NINETEEN)} → {ts(TWENTY)}.",
        f"@mm zmienia „Otwarcie zapisów” w **Zeus night** (<#55>): przy publikacji → {ts(DAY_BEFORE)}.",
        "@mm zmienia „Czas trwania” w **Zeus night** (<#55>): 2 h → 2 h 30 min.",
        "@mm zmienia „Publiczna” w **Zeus night** (<#55>): tak → nie.",
        "@mm zmienia „Notatka” w **Zeus night** (<#55>).",
        "@mm zmienia „something_new” w **Zeus night** (<#55>): a → b."]


def test_sign_ups_and_the_orbat():
    assert line("signup.answered", person=ANNA, status="cant", kind="zeus") == \
        "<@11> answered Not coming for **Zeus night** (<#55>)."
    assert line("signup.answered", "pl", person=ANNA, status="coming", kind="zeus") == \
        "<@11> odpowiada „Będę” na **Zeus night** (<#55>)."
    assert line("slot.took", person=ANNA, slot="Alpha / Lead", from_slot="Bravo / Medic", from_offer=True) == \
        "<@11> moved from Bravo / Medic to Alpha / Lead in **Zeus night** (<#55>). (from an offer)"
    assert line("orbat.set", "pl", by=MM, slots=5, displaced=[ANNA]) == \
        "@mm ustawia ORBAT **Zeus night** (<#55>): 5 slotów. Ich slot zniknął, są teraz na początku listy " \
        "rezerwowej: <@11>"
    assert line("offer.made", slots=["Alpha / Lead"], person=None, until=NINETEEN) == \
        f"Alpha / Lead in **Zeus night** (<#55>) offered to the whole reserve list until {ts(NINETEEN, 't')}."
    assert line("operation.organisers", by=MM, added=[ANNA], removed=[{"name": "bob", "discord_id": None}]).split("\n") == [
        "@mm made <@11> a co-organiser of **Zeus night** (<#55>).",
        "@mm removed @bob from the co-organisers of **Zeus night** (<#55>)."]


def test_what_isnt_known_is_the_supervisors_text():
    assert describe({"event": "operation.teleported", "text": "OP-7 Zeus night: *beamed* up"}, "pl") == \
        "OP-7 Zeus night: \\*beamed\\* up"
    assert describe({"event": "slot.took", "text": "OP-7: anna took a slot"}, "pl") == "OP-7: anna took a slot"  # (fields missing)
    assert describe({"text": "OP-7 Zeus night: @anna: Coming"}, "en") == "OP-7 Zeus night: @anna: Coming"  # an older supervisor
    assert describe({"event": "operation.file", **OP, "by": MM, "slot": "poster", "action": "set", "text": "x"}, "en") == "x"
    assert describe({}, "en") == ""


def test_every_event_of_the_supervisor_is_worded():
    assert set(WORDINGS) == {
        "operation.created", "operation.copied", "operation.changed", "operation.opord", "operation.organisers",
        "operation.file", "operation.mission", "operation.published", "operation.submitted", "operation.withdrawn",
        "operation.cancelled", "operation.deleted", "operation.ended", "operation.extended", "operation.profile_build",
        "operation.profile_built", "operation.signup_opened", "operation.signup_closed",
        "operation.requirements_missed", "orbat.set", "orbat.cleared",
        "slot.took", "slot.left", "signup.answered", "offer.made", "offer.declined", "offer.expired", "slot.locked",
        "slot.assigned", "slot.removed", "signup.placed", "signup.freed", "attendance.corrected",
        "attendance.confirmed", "attendance.rated", "link.removed", "account.renamed"}
