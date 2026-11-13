"""An archived operation as one HTML file (docs/PLAN.md, *Long-term archive*, *Reading it*): the info and the OPORD
from the operation as last drawn, and the transcript, in the reader's language (the final ORBAT and the attendance
join once the supervisor's operation carries them: docs/PLAN-front-end.md, phases 4 and 5).

Attachments come three ways (:data:`MODES`): ``inline`` (images as data URIs, other files by name), ``zip``
(links to ``attachments/<file>`` next to the HTML in a ZIP), ``listed`` (names only: the files stay on the host).
The Discord side picks the first that fits the server's upload limit."""

from __future__ import annotations

import base64
import mimetypes
from datetime import tzinfo
from html import escape
from pathlib import Path
from typing import Any

from app.channel.render import PARAGRAPHS, heading, when
from app.i18n import t

MODES = ("inline", "zip", "listed")
IMAGE = (".png", ".jpg", ".jpeg", ".gif", ".webp")
STYLE = """body{font-family:system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;line-height:1.45;
color:#1d1d1f;background:#fff}h1{margin-bottom:.2rem}h2{margin-top:2rem;border-bottom:1px solid #ddd}
.meta{color:#666}.msg{margin:.6rem 0}.who{font-weight:600}.when{color:#888;font-size:.85em;margin-left:.4rem}
.body{white-space:pre-wrap}img{max-width:100%;margin:.3rem 0}ul{padding-left:1.2rem}"""


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


def render(op: dict[str, Any], messages: list, mode: str, language: str, tz: tzinfo) -> str:
    """``op``: the operation as last drawn (the supervisor's JSON)."""
    def local(value) -> str:
        return value.astimezone(tz).strftime("%d.%m.%Y %H:%M") if value is not None else "—"

    author = op.get("author") or {}
    parts = [f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{_text(op['title'])}</title>"
             f"<style>{STYLE}</style></head><body>",
             f"<h1>{_text(op['title'])}</h1>",
             f"<p class=\"meta\">{_text(op.get('kind_name') or op['kind'])} · {t('Assembly', language)} "
             f"{local(when(op, 'assembly_at'))} · {t('Start', language)} {local(when(op, 'start_at'))} · "
             f"{op['duration_minutes']} min · {t('Author', language)}: "
             f"{_text(author.get('display_name') or author.get('username') or '—')}</p>"]
    if op["status"] == "cancelled":
        parts.append(f"<p><strong>{t('CANCELLED', language)}</strong>: {_text(op.get('cancel_reason') or '')}</p>")
    written = [(p, (op.get("opord") or {}).get(p)) for p in PARAGRAPHS]
    written = [(p, body) for p, body in written if isinstance(body, str) and body.strip()]
    if written:
        parts.append(f"<h2>{t('OPORD', language)}</h2>")
        parts += [f"<h3>{heading(p, language)}</h3><div class=\"body\">{_text(body)}</div>" for p, body in written]
    parts.append(f"<h2>{t('Discussion', language)}</h2>")
    if not messages:
        parts.append(f"<p class=\"meta\">{t('Nobody wrote anything.', language)}</p>")
    for message in messages:
        author = t("deleted", language) if message.author_id < 0 else message.author_name
        edited = f" ({t('edited', language)})" if message.edited_at else ""
        parts.append(f"<div class=\"msg\"><span class=\"who\">{_text(author)}</span>"
                     f"<span class=\"when\">{local(message.created_at)}{edited}</span>"
                     f"<div class=\"body\">{_text(message.content)}</div>"
                     + "".join(_attachment(a, mode, language) for a in message.attachments or ()) + "</div>")
    parts.append("</body></html>")
    return "".join(parts)
