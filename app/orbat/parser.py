"""The ORBAT text format (docs/PLAN.md, *ORBAT format*).

A line starting with ``#`` is a squad; every other non-blank line is a slot. On a slot line, ``[Role]`` requires
that Discord role (by name, or by id), ``xN`` repeats the slot N times (before or after the role), and a leading
``!`` locks it closed::

    # Alpha
    Squad Leader [NCO]
    Rifleman x4
    !Zeus

:func:`parse` checks everything and names the line of each problem; :func:`render` writes an ORBAT back as text,
for the edit modal, collapsing repeated slots into ``xN``."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.i18n import t

MAX_TEXT = 4000  # what a Discord modal takes
MAX_REPEAT = 50
MAX_SLOTS = 200
MAX_NAME = 100
SLOT = re.compile(r"^(?P<lock>!)?\s*(?P<name>.*?)(?:\s+x(?P<count>\d+))?(?:\s*\[(?P<role>[^\[\]]*)\])?"
                  r"(?:\s+x(?P<count2>\d+))?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedSlot:
    name: str
    role_id: int | None = None
    locked: bool = False


@dataclass
class ParsedSquad:
    name: str
    slots: list[ParsedSlot] = field(default_factory=list)


class OrbatError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


def parse(text: str, roles: Iterable[tuple[int, str]], language: str = "en") -> list[ParsedSquad]:
    """The squads of ``text``; ``roles`` are the server's (id, name). Raises :class:`OrbatError` with every problem."""
    by_name: dict[str, list[int]] = {}
    ids = set()
    for role_id, name in roles:
        by_name.setdefault(name.casefold(), []).append(role_id)
        ids.add(role_id)
    problems: list[str] = []
    if len(text) > MAX_TEXT:
        problems.append(t("The ORBAT is {length} characters long; at most {limit} fit.", language,
                          length=len(text), limit=MAX_TEXT))
    squads: list[ParsedSquad] = []
    written: list[int] = []  # slot lines under each squad, wrong ones included
    total = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            name = line.lstrip("#").strip()
            if not name:
                problems.append(t("Line {line}: a squad needs a name.", language, line=number))
            squads.append(ParsedSquad(name[:MAX_NAME]))
            written.append(0)
            continue
        if not squads:
            problems.append(t("Line {line}: a slot before the first squad (start one with #).", language, line=number))
            continue
        written[-1] += 1
        match = SLOT.match(line)
        name = (match.group("name") if match else line).strip()
        if not match or not name:
            problems.append(t("Line {line}: a slot needs a name.", language, line=number))
            continue
        count = int(match.group("count") or match.group("count2") or 1)
        if not 1 <= count <= MAX_REPEAT:
            problems.append(t("Line {line}: repeat a slot 1 to {limit} times.", language, line=number,
                              limit=MAX_REPEAT))
            continue
        role_id = None
        if match.group("role") is not None:
            role = match.group("role").strip()
            found = [int(role)] if role.isdigit() and int(role) in ids else by_name.get(role.casefold(), [])
            if len(found) != 1:
                problems.append(t("Line {line}: there is no role called {role}.", language, line=number, role=role)
                                if not found else
                                t("Line {line}: more than one role is called {role}; use its id.", language,
                                  line=number, role=role))
                continue
            role_id = found[0]
        squads[-1].slots.extend([ParsedSlot(name[:MAX_NAME], role_id, bool(match.group("lock")))] * count)
        total += count
    for squad, lines in zip(squads, written):
        if not lines:
            problems.append(t("The squad {squad} has no slots.", language, squad=squad.name or "#"))
    if not squads and not problems:
        problems.append(t("The ORBAT is empty.", language))
    if total > MAX_SLOTS:
        problems.append(t("The ORBAT has {count} slots; at most {limit} fit.", language, count=total,
                          limit=MAX_SLOTS))
    if problems:
        raise OrbatError(problems)
    return squads


def render(squads: Iterable[tuple[str, Iterable[ParsedSlot]]], role_names: Mapping[int, str]) -> str:
    """The text that :func:`parse` reads back into the same ORBAT (roles by their current name, or id if gone)."""
    lines: list[str] = []
    for name, slots in squads:
        if lines:
            lines.append("")
        lines.append(f"# {name}")
        run: list[ParsedSlot] = []
        for slot in [*slots, None]:
            if run and slot != run[0]:
                first = run[0]
                text = ("!" if first.locked else "") + first.name
                if len(run) > 1:
                    text += f" x{len(run)}"
                if first.role_id is not None:
                    text += f" [{role_names.get(first.role_id, first.role_id)}]"
                lines.append(text)
                run = []
            if slot is not None:
                run.append(slot)
    return "\n".join(lines)
