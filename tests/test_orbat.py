"""The ORBAT format and the shared templates (docs/PLAN.md, *ORBAT format*, *Templates*)."""

import pytest

from app.orbat.parser import MAX_TEXT, OrbatError, ParsedSlot, parse, render
from app.orbat.templates import Problem, TemplateError, Templates

ROLES = [(1, "Officer"), (2, "NCO"), (3, "Medic"), (4, "Pilot")]
EXAMPLE = """# Command
Platoon Leader [Officer]
!Zeus

# Alpha
Squad Leader [NCO]
Medic [Medic]
Autorifleman
Rifleman x4

# Eagle 1-1
Pilot [Pilot]
Co-pilot [Pilot]
"""


def test_the_plans_example():
    squads = parse(EXAMPLE, ROLES)
    assert [s.name for s in squads] == ["Command", "Alpha", "Eagle 1-1"]
    assert squads[0].slots == [ParsedSlot("Platoon Leader", 1), ParsedSlot("Zeus", None, True)]
    assert [s.name for s in squads[1].slots] == ["Squad Leader", "Medic", "Autorifleman"] + ["Rifleman"] * 4


def test_repeat_and_role_in_either_order_and_role_by_id_or_any_case():
    (squad,) = parse("# A\nMedic x2 [medic]\nPilot [4] x2\n", ROLES)
    assert squad.slots == [ParsedSlot("Medic", 3)] * 2 + [ParsedSlot("Pilot", 4)] * 2


def test_every_problem_names_its_line():
    with pytest.raises(OrbatError) as caught:
        parse("Rifleman\n# A\nMedic [Doctor]\nRifleman x0\n#\n# Empty\n", ROLES)
    assert caught.value.problems == [
        "Line 1: a slot before the first squad (start one with #).",
        "Line 3: there is no role called Doctor.",
        "Line 4: repeat a slot 1 to 50 times.",
        "Line 5: a squad needs a name.",
        "The squad # has no slots.",
        "The squad Empty has no slots.",
    ]


def test_ambiguous_roles_and_size_limits():
    with pytest.raises(OrbatError) as caught:
        parse("# A\nMedic [Medic]\n", ROLES + [(9, "medic")], "en")
    assert caught.value.problems == ["Line 2: more than one role is called Medic; use its id."]
    with pytest.raises(OrbatError) as caught:
        parse("# A\n" + "Rifleman x50\n" * 5, ROLES)
    assert caught.value.problems == ["The ORBAT has 250 slots; at most 200 fit."]
    with pytest.raises(OrbatError):
        parse("# A\n" + "x" * MAX_TEXT, ROLES)
    with pytest.raises(OrbatError) as caught:
        parse("  \n", ROLES)
    assert caught.value.problems == ["The ORBAT is empty."]


def test_problems_in_polish():
    with pytest.raises(OrbatError) as caught:
        parse("Rifleman\n", ROLES, "pl")
    assert caught.value.problems[0].startswith("Linia 1:")


def test_render_reads_back_into_the_same_orbat():
    squads = parse(EXAMPLE, ROLES)
    text = render([(s.name, s.slots) for s in squads], dict(ROLES))
    assert "Rifleman x4" in text and "!Zeus" in text
    assert parse(text, ROLES) == squads
    assert "[77]" in render([("A", [ParsedSlot("Medic", 77)])], {})  # a deleted role keeps its id


async def test_templates_are_shared_but_changed_only_by_their_owner(db):
    templates = Templates(db)
    mine = await templates.save("Platoon", EXAMPLE, member_id=10, manage_any=False)
    with pytest.raises(TemplateError) as caught:
        await templates.save("Platoon", "other", member_id=11, manage_any=False)
    assert caught.value.problem == Problem.NAME_TAKEN
    await templates.save("Platoon", "# A\nX\n", member_id=10, manage_any=False)  # the owner overwrites
    with pytest.raises(TemplateError) as caught:
        await templates.delete(mine.id, member_id=11, manage_any=False)
    assert caught.value.problem == Problem.NOT_ALLOWED
    await templates.rename(mine.id, "  Platoon   v2 ", member_id=12, manage_any=True)
    assert [(t.name, t.text) for t in await templates.all()] == [("Platoon v2", "# A\nX\n")]
    await templates.save("Other", "x", member_id=11, manage_any=False)
    with pytest.raises(TemplateError) as caught:
        await templates.rename(mine.id, "Other", member_id=10, manage_any=False)
    assert caught.value.problem == Problem.NAME_TAKEN
    await templates.delete(mine.id, member_id=10, manage_any=False)
    assert [t.name for t in await templates.all()] == ["Other"]
