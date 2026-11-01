"""Arma 3 Launcher presets (docs/PLAN.md, *Organiser panel*, *Modset*): a port of the supervisor's
``app/steam/presets.py`` with its Creator DLC table (``app/steam/depots.py``). Ported, not imported: the bot never
uses the supervisor's code. Keep the DLC table in step with the supervisor's when a Creator DLC is added."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Cdlc:
    key: str  # the supervisor's folder name, and the key of BOT_DLC_ROLES__<KEY>
    name: str
    client_app_id: int  # the store app id a launcher preset links to


CDLCS: dict[str, Cdlc] = {c.key: c for c in (
    Cdlc("gm", "Global Mobilization", 1042220),
    Cdlc("vn", "S.O.G. Prairie Fire", 1227700),
    Cdlc("csla", "CSLA Iron Curtain", 1294440),
    Cdlc("ws", "Western Sahara", 1681170),
    Cdlc("spe", "Spearhead 1944", 1175380),
    Cdlc("rf", "Reaction Forces", 2647760),
    Cdlc("ef", "Expeditionary Forces", 2647830),
)}
CDLC_BY_CLIENT_APP = {c.client_app_id: c for c in CDLCS.values()}

_ROW = re.compile(r"<tr[^>]*data-type=\"(ModContainer|DlcContainer)\"[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_DISPLAY = re.compile(r"data-type=\"DisplayName\"[^>]*>(.*?)<", re.DOTALL | re.IGNORECASE)
_WORKSHOP_LINK = re.compile(r"filedetails/\?(?:[^\"'<>]*&)?id=(\d+)", re.IGNORECASE)
_APP_LINK = re.compile(r"store\.steampowered\.com/app/(\d+)", re.IGNORECASE)
_PRESET_NAME = re.compile(r"<meta[^>]*name=\"arma:PresetName\"[^>]*content=\"([^\"]*)\"", re.IGNORECASE)


@dataclass
class PresetMod:
    workshop_id: int
    name: str


@dataclass
class Preset:
    name: str | None = None
    mods: list[PresetMod] = field(default_factory=list)
    cdlc: list[str] = field(default_factory=list)  # Creator DLC keys (gm, vn, ...)
    unknown_dlc_app_ids: list[int] = field(default_factory=list)


def parse_preset_html(content: str) -> Preset:
    """Raises ValueError when the file holds no Workshop mods and no DLC."""
    preset = Preset()
    if match := _PRESET_NAME.search(content):
        preset.name = html.unescape(match.group(1)).strip() or None
    seen: set[int] = set()
    rows = _ROW.findall(content)
    for kind, body in rows:
        name_match = _DISPLAY.search(body)
        name = html.unescape(name_match.group(1)).strip() if name_match else ""
        if kind.lower() == "modcontainer":
            link = _WORKSHOP_LINK.search(body)
            if link and int(link.group(1)) not in seen:
                seen.add(int(link.group(1)))
                preset.mods.append(PresetMod(int(link.group(1)), name or link.group(1)))
        else:
            link = _APP_LINK.search(body)
            if link:
                app_id = int(link.group(1))
                cdlc = CDLC_BY_CLIENT_APP.get(app_id)
                if cdlc is None:
                    preset.unknown_dlc_app_ids.append(app_id)
                elif cdlc.key not in preset.cdlc:
                    preset.cdlc.append(cdlc.key)
    if not rows:  # not a launcher export: take every Workshop link in document order
        for link in _WORKSHOP_LINK.finditer(content):
            workshop_id = int(link.group(1))
            if workshop_id not in seen:
                seen.add(workshop_id)
                preset.mods.append(PresetMod(workshop_id, str(workshop_id)))
    if not preset.mods and not preset.cdlc:
        raise ValueError("No Workshop mods or DLC found in the preset")
    return preset
