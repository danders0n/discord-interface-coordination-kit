"""The `/privacy` text (docs/PLAN.md, *Privacy*): what the bot stores and publishes, and how to ask for erasure.

The defaults ship next to this file; staff override them with ``<data_dir>/privacy.<language>.md``, read on every
call, so an edit shows at once."""

from __future__ import annotations

from pathlib import Path

DEFAULTS = Path(__file__).parent


def text(language: str, data_dir: Path) -> str:
    for folder in (data_dir, DEFAULTS):
        path = folder / f"privacy.{language}.md"
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    return (DEFAULTS / "privacy.en.md").read_text(encoding="utf-8").strip()
