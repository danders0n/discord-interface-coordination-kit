"""An archived event as one HTML file (docs/PLAN.md, *Long-term archive*, *Reading it*): the info, the briefing,
the final ORBAT or sign-up list, the attendance report and the transcript, in the reader's language.

Attachments come three ways (:data:`MODES`): ``inline`` (images as data URIs, other files by name), ``zip``
(links to ``attachments/<file>`` next to the HTML in a ZIP), ``listed`` (names only: the files stay on the host).
The Discord side picks the first that fits the server's upload limit."""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Mapping
from datetime import tzinfo
from html import escape
from pathlib import Path
from typing import Any

from app.eventchannel.render import kind_label
from app.i18n import t

MODES = ("inline", "zip", "listed")
IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".webp")
STYLE = """body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;line-height:1.45;
color:#1d1d1f;background:#fff}h1{margin-bottom:.2rem}h2{margin-top:2rem;border-bottom:1px solid #ddd}
.meta{color:#666}.msg{margin:.6rem 0}.who{font-weight:600}.when{color:#888;font-size:.85em;margin-left:.4rem}
.body{white-space:pre-wrap}img{max-width:100%;margin:.3rem 0}ul{padding-left:1.2rem}"""


def _name(member_id: int | None, names: Mapping[int, str]) -> str:
    if member_id is None:
        return "—"
    if member_id < 0:
        return f"former:{-member_id}"
    return names.get(member_id) or str(member_id)


def _text(value: str) -> str:
    return escape(value or "")


def _attachment(item: dict[str, Any], mode: str, language: str) -> str:
    name, path = item["name"], Path(item["path"])
    if mode == "zip":
        return f'<div><a href="attachments/{escape(path.name)}">{escape(name)}</a></div>'
    if mode == "inline" and path.suffix.lower() in IMAGE and path.is_file():
        kind = mimetypes.guess_type(path.name)[0] or "image/png"
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return f'<div><img alt="{escape(name)}" src="data:{kind};base64,{data}"></div>'
    note = t("kept on the host", language) if mode == "listed" else ""
    return f"<div>📎 {escape(name)}{f' ({note})' if note else ''}</div>"


def render(event: Any, board: Any, entries: list, messages: list, names: Mapping[int, str], mode: str,
           language: str, tz: tzinfo, kinds: Any = None) -> str:
    """``kinds``: settings.events.kind_names."""
    def when(value) -> str:
        return value.astimezone(tz).strftime("%d.%m.%Y %H:%M")

    parts = [f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{_text(event.title)}</title>"
             f"<style>{STYLE}</style></head><body>",
             f"<h1>{_text(event.title)}</h1>",
             f"<p class=\"meta\">{_text(kind_label(event.kind, language, kinds))} · {t('Assembly', language)} {when(event.assembly_at)} · "
             f"{t('Start', language)} {when(event.start_at)} · {event.duration_minutes} min · "
             f"{t('Author', language)}: {_text(_name(event.author_id, names))}</p>"]
    if event.status == "cancelled":
        parts.append(f"<p><strong>{t('CANCELLED', language)}</strong>: {_text(event.cancel_reason or '')}</p>")
    briefing = [(t("Situation", language), event.briefing_situation), (t("Mission", language), event.briefing_mission),
                (t("Notes", language), event.briefing_notes)]
    if any(body for _, body in briefing):
        parts.append(f"<h2>{t('Briefing', language)}</h2>")
        parts += [f"<h3>{name}</h3><div class=\"body\">{_text(body)}</div>" for name, body in briefing if body]
    if board.squads:
        parts.append(f"<h2>{t('ORBAT', language)}</h2>")
        for squad, slots in board.squads:
            parts.append(f"<h3>{_text(squad.name)}</h3><ul>")
            parts += [f"<li>{_text(slot.name)}: {_text(_name(slot.holder_id, names))}</li>" for slot in slots]
            parts.append("</ul>")
    lists = [("coming", t("Coming", language)), ("maybe", t("Maybe", language)), ("cant", t("Can't come", language)),
             ("reserve", t("Reserve list", language))]
    for status, title in lists:
        members = board.status(status)
        if members:
            parts.append(f"<h3>{title} ({len(members)})</h3><p>"
                         + ", ".join(_text(_name(m, names)) for m in members) + "</p>")
    if entries:
        parts.append(f"<h2>{t('Attendance', language)}</h2><ul>")
        labels = {"attended": t("Attended", language), "no_show": t("No-show", language),
                  "excused": t("Excused", language), "to_confirm": t("To confirm", language),
                  "not_linked": t("Not linked", language), "walk_in": t("Played without a slot", language)}
        for entry in entries:
            who = _name(entry.member_id, names) if entry.member_id is not None else (entry.name or entry.steam_id or "?")
            parts.append(f"<li>{_text(who)}: {labels.get(entry.status, entry.status)}"
                         f"{f' · {entry.seconds // 60} min' if entry.seconds else ''}</li>")
        parts.append("</ul>")
    parts.append(f"<h2>{t('Discussion', language)}</h2>")
    if not messages:
        parts.append(f"<p class=\"meta\">{t('Nobody wrote anything.', language)}</p>")
    for message in messages:
        author = t("deleted", language) if message.author_id < 0 else message.author_name
        edited = f" ({t('edited', language)})" if message.edited_at else ""
        parts.append(f"<div class=\"msg\"><span class=\"who\">{_text(author)}</span>"
                     f"<span class=\"when\">{when(message.created_at)}{edited}</span>"
                     f"<div class=\"body\">{_text(message.content)}</div>"
                     + "".join(_attachment(a, mode, language) for a in message.attachments or ()) + "</div>")
    parts.append("</body></html>")
    return "".join(parts)
