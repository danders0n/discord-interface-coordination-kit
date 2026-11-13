"""Every text the bot shows has a Polish translation with the same fields (docs/PLAN.md, *Translations*)."""

import ast
import re
from pathlib import Path

import discord

from app.i18n import catalog, language_of, placeholders, polish_form, t

APP = Path(__file__).resolve().parents[1] / "app"
COMMAND_NAME = re.compile(r"^[-_\w]{1,32}$")


def _literal(node: ast.AST) -> str | tuple[str, str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Tuple) and len(node.elts) == 2 and all(isinstance(e, ast.Constant) for e in node.elts):
        return (node.elts[0].value, node.elts[1].value)
    return None


def texts() -> tuple[list[tuple[str, object, str]], list[str]]:
    """(where, key, kind) for every ``t()`` call and slash command text, and the calls that aren't literals."""
    found, dynamic = [], []
    for path in APP.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            where = f"{path.relative_to(APP.parent)}:{node.lineno}"
            if name in ("t", "locale_str") and node.args:
                key = _literal(node.args[0])
                (found.append((where, key, "text")) if key else dynamic.append(where))
            elif name in ("command", "Group", "describe", "choices"):
                for keyword in node.keywords:
                    value = _literal(keyword.value)
                    if isinstance(value, str):
                        found.append((where, value, "name" if keyword.arg == "name" else "text"))
    return found, dynamic


def test_t_is_only_called_with_literals():
    assert texts()[1] == []


def test_every_text_has_a_polish_translation_with_the_same_fields():
    polish = catalog("pl")
    problems = []
    for where, key, kind in texts()[0]:
        singular = key[0] if isinstance(key, tuple) else key
        found = polish.get(singular)
        if found is None:
            problems.append(f"{where}: missing {singular!r}")
            continue
        forms = found.values() if isinstance(found, dict) else [found]
        for form in forms:
            if placeholders(form) != placeholders(singular):
                problems.append(f"{where}: fields differ in {singular!r}")
        if kind == "name" and not (isinstance(found, str) and COMMAND_NAME.match(found) and found == found.lower()):
            problems.append(f"{where}: {found!r} is not a valid command name")
    assert problems == []


async def test_every_translated_command_name_is_one_discord_accepts():
    """A name shares its key with texts of the same words (the ``on`` parameter and the text "on" -> "wł."); one
    invalid name makes Discord refuse the whole sync, so the payloads are built here as ``tree.sync`` does."""
    from app.admin.cog import AdminCog
    from app.archive.cog import ArchiveCog
    from app.i18n import Translator
    from app.links.cog import LinkCog
    from app.privacy.cog import PrivacyCog

    tree = discord.app_commands.CommandTree(discord.Client(intents=discord.Intents.none()))
    problems = []

    def names(payload: dict, where: str) -> None:
        for locale, name in (payload.get("name_localizations") or {}).items():
            if not (COMMAND_NAME.match(name) and name == name.lower()):
                problems.append(f"{where} {payload['name']}: {locale} {name!r}")
        for option in payload.get("options", []):
            names(option, f"{where} {payload['name']}")

    for cog in (AdminCog, PrivacyCog, LinkCog, ArchiveCog):
        for command in cog.__cog_app_commands__:
            names(await command.get_translated_payload(tree, Translator()), cog.__name__)
    assert problems == []


def test_no_unused_translations():
    used = {key[0] if isinstance(key, tuple) else key for _, key, _ in texts()[0]}
    assert sorted(set(catalog("pl")) - used) == []


def test_polish_plural_forms():
    assert [polish_form(n) for n in (1, 2, 4, 5, 12, 22, 25, 101, 112)] == \
        ["one", "few", "few", "many", "many", "few", "many", "many", "many"]


def test_language_follows_the_discord_locale():
    assert language_of(discord.Locale.polish) == "pl"
    assert language_of(discord.Locale.german) == "en"
    assert t("Settings reloaded.", discord.Locale.polish) != "Settings reloaded."
    assert t("Settings reloaded.", discord.Locale.british_english) == "Settings reloaded."
