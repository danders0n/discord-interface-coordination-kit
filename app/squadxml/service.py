"""squad.xml (docs/PLAN.md, *Steam link and squad.xml*), without discord.py.

- One file for the group: members with a membership rank role and a Steam link, sorted by nick. ``<member id>``
  is the Steam64 ID, ``nick`` and ``<name>`` the nick typed at ``/link``, ``<icq>`` the Discord username, and
  ``<remark>`` the configured remark roles the member has, in config order (cut to units.arma3.com's 128
  characters). The layout is the old bot's, so the switch-over doesn't change the file.
- Only ``squad.xml`` is written, into ``squad_xml.output_dir``; IIS serves it with ``squad.dtd``, ``squad.xsl`` and
  the logo, kept by hand. The write is atomic (a temp file, then replace) and happens only when the text changed.
- Changes are debounced: :meth:`SquadXml.request` writes once ``delay`` seconds after the first of a burst (a bulk
  role edit), and again if more arrive meanwhile. Nothing is written while the server's members are unknown (not
  connected yet), so a start-up never writes an empty file. A full rebuild runs at start-up and on ``/reload``."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from app.links.service import LinkService
from app.resolve import Ids
from app.settings import SettingsStore

log = logging.getLogger(__name__)
REMARK_MAX = 128  # units.arma3.com's limit
INDENT = " " * 8
FILE_NAME = "squad.xml"


@dataclass(frozen=True)
class Header:
    nick: str
    name: str
    email: str
    web: str
    picture: str
    title: str


@dataclass(frozen=True)
class Entry:
    steam_id: str
    nick: str
    username: str
    remark: str


@dataclass(frozen=True)
class Person:
    """A member on the server now, as squad.xml needs them."""

    role_ids: frozenset[int]
    username: str


def remark(names: Iterable[str]) -> str:
    text = ", ".join(names)
    if len(text) > REMARK_MAX:
        text = text[:REMARK_MAX - 1].rstrip(", ") + "…"
    return text


def render(header: Header, entries: Iterable[Entry]) -> str:
    parts = [
        '<?xml version="1.0"?>\n',
        '<!DOCTYPE squad SYSTEM "squad.dtd">\n',
        '<?xml-stylesheet href="squad.xsl" type="text/xsl"?>\n',
        f"<squad nick={quoteattr(header.nick)}>\n",
        f"\t<name>{escape(header.name)}</name>\n",
        f"\t<email>{escape(header.email)}</email>\n",
        f"\t<web>{escape(header.web)}</web>\n",
        f"\t<picture>{escape(header.picture)}</picture>\n",
        f"\t<title>{escape(header.title)}</title>\n\n\n",
    ]
    for entry in sorted(entries, key=lambda e: (e.nick.casefold(), e.steam_id)):
        parts += [
            f"<member id={quoteattr(entry.steam_id)} nick={quoteattr(entry.nick)}>\n",
            f"{INDENT}<name>{escape(entry.nick)}</name>\n",
            f"{INDENT}<email>N/A</email>\n",
            f"{INDENT}<icq>{escape(entry.username)}</icq>\n",
            f"{INDENT}<remark>{escape(entry.remark)}</remark>\n",
            "</member>\n",
        ]
    parts.append("</squad>")
    return "".join(parts)


def _write(path: Path, text: str) -> bool:
    """Write ``text`` atomically unless the file already holds it. True when written."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except FileNotFoundError:
        pass
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8", newline="")
    os.replace(temp, path)
    return True


class SquadXml:
    def __init__(self, links: LinkService, store: SettingsStore, ids: Callable[[], Ids],
                 roster: Callable[[], Mapping[int, Person] | None], delay: float = 30.0) -> None:
        """``roster()``: the server's members now, or None while not connected."""
        self.links, self.store, self.ids, self.roster, self.delay = links, store, ids, roster, delay
        self._task: asyncio.Task | None = None
        self._again = False

    @property
    def path(self) -> Path | None:
        folder = self.store.current.squad_xml.output_dir
        return folder / FILE_NAME if folder is not None else None

    def header(self) -> Header:
        settings = self.store.current
        x = settings.squad_xml
        return Header(x.nick or settings.group.acronym, x.name or settings.group.name, x.email, x.web, x.picture,
                      x.title or settings.group.name)

    async def build(self) -> str | None:
        """The file's text now, or None when the members aren't known."""
        people = self.roster()
        if people is None:
            return None
        ids = self.ids()
        entries = []
        for link in await self.links.all():
            person = people.get(link.member_id)
            if person is None or person.role_ids.isdisjoint(ids.rank):
                continue
            entries.append(Entry(link.steam_id, link.nick, person.username,
                                 remark(name for role, name in ids.remark.items() if role in person.role_ids)))
        return render(self.header(), entries)

    async def write_now(self) -> bool:
        """Rebuild and write; True when the file changed."""
        path = self.path
        if path is None:
            return False
        text = await self.build()
        if text is None:
            return False
        written = await asyncio.to_thread(_write, path, text)
        if written:
            log.info("Wrote %s", path)
        return written

    def request(self) -> None:
        """Something squad.xml shows may have changed: write once the burst is over."""
        if self._task is not None and not self._task.done():
            self._again = True
            return
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.delay)
            self._again = False
            try:
                await self.write_now()
            except Exception:  # noqa: BLE001 - logged as an ERROR, so staff hear of it
                log.exception("Writing squad.xml failed")
            if not self._again:
                return

    async def settle(self) -> None:
        """Wait for a pending write (tests, shutdown)."""
        while self._task is not None and not self._task.done():
            await self._task
