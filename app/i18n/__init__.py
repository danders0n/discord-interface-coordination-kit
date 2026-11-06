"""Translations (docs/PLAN.md, *Code and tooling*): the English text is the key.

``t("Sign-up opens {when}", locale, when=...)`` looks the text up in ``pl.json`` for Polish and formats the
fields. A count takes a pair of English forms, ``t(("{count} slot", "{count} slots"), locale, count=n)``; its
Polish value is ``{"one": ..., "few": ..., "many": ...}``. ``tests/test_i18n.py`` checks that every literal passed
to ``t()`` has a translation with the same fields, so ``t()`` is only ever called with literals."""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from string import Formatter
from typing import Any

import discord
from discord import app_commands

CATALOG_DIR = Path(__file__).parent
Key = str | tuple[str, str]


@cache
def catalog(language: str) -> dict[str, Any]:
    path = CATALOG_DIR / f"{language}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def language_of(locale: discord.Locale | str | None) -> str:
    """``pl`` for Polish, ``en`` for everything else (members with another client language read English)."""
    value = locale.value if isinstance(locale, discord.Locale) else (locale or "en")
    return "pl" if value.lower().startswith("pl") else "en"


def polish_form(count: int) -> str:
    n = abs(count)
    if n == 1:
        return "one"
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return "few"
    return "many"


def t(key: Key, locale: discord.Locale | str | None = None, /, **fields: Any) -> str:
    language = language_of(locale)
    singular, plural = key if isinstance(key, tuple) else (key, key)
    count = fields.get("count")
    if language == "en":
        text = singular if count is None or abs(count) == 1 else plural
    else:
        found = catalog(language).get(singular)
        if isinstance(found, dict):
            text = found.get(polish_form(count if count is not None else 1)) or found.get("many") or singular
        else:
            text = found or singular
    return text.format(**fields) if fields else text


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(text) if name}


class Translator(app_commands.Translator):
    """Slash command names, descriptions and choices from the same catalog.

    A name (command, group, parameter) shares its key with any text of the same words, such as the ``on``
    parameter and the text "on" ("wł."), so a translation that isn't a valid name is left out: Discord refuses
    the whole sync over one."""

    async def translate(self, string: app_commands.locale_str, locale: discord.Locale,
                        context: app_commands.TranslationContextTypes) -> str | None:
        if language_of(locale) == "en":
            return None
        found = catalog(language_of(locale)).get(string.message)
        if not isinstance(found, str):
            return None
        if context.location in NAMES and not valid_name(found):
            return None
        return found


NAMES = {app_commands.TranslationContextLocation.command_name, app_commands.TranslationContextLocation.group_name,
         app_commands.TranslationContextLocation.parameter_name}
# Discord's rule for command and option names, lower case only.
_NAME = re.compile(r"^[-_\w]{1,32}$")


def valid_name(text: str) -> bool:
    return bool(_NAME.match(text)) and text == text.lower()
