"""The supervisor's contract for the bot (docs/PLAN-front-end.md, *Tests and the fake supervisor*):
``tests/contract/openapi-bot.json``, a copy of the supervisor's ``docs/api/openapi-bot.json`` made by
``python scripts/sync_contract.py``. The fake supervisor checks every request it gets on a contract route against it
(query names, JSON body), so a test that passes sends only what the real supervisor accepts.

The schema check covers what pydantic writes into that file: ``type``, ``properties``, ``required``, ``items``,
``enum``, ``pattern``, lengths, ``minimum`` / ``maximum``, ``anyOf`` and ``$ref``."""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

CONTRACT = Path(__file__).resolve().parent / "contract" / "openapi-bot.json"
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


@cache
def contract() -> dict[str, Any]:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def operation(method: str, path: str) -> dict[str, Any] | None:
    """The contract's operation for a route, ``path`` as the contract writes it (``/api/members/{discord_id}``)."""
    return contract()["paths"].get(path, {}).get(method.lower())


def routes() -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, ops in contract()["paths"].items() for method in ops}


def _resolve(schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = contract()["components"]["schemas"][name]
    return schema


def problems(value: Any, schema: dict[str, Any], where: str = "body") -> list[str]:
    """What is wrong with ``value`` against ``schema`` (empty: it fits)."""
    schema = _resolve(schema)
    if "anyOf" in schema:
        options = [problems(value, option, where) for option in schema["anyOf"]]
        return [] if any(not found for found in options) else [f"{where}: fits none of anyOf ({options})"]
    kind = schema.get("type")
    if kind == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{where}: {value!r} is not an integer"]
    elif kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{where}: {value!r} is not a number"]
    elif kind in _TYPES and not isinstance(value, _TYPES[kind]):
        return [f"{where}: {value!r} is not {kind}"]
    found: list[str] = []
    if "enum" in schema and value not in schema["enum"]:
        found.append(f"{where}: {value!r} not in {schema['enum']}")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            found.append(f"{where}: {value!r} doesn't match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            found.append(f"{where}: length {len(value)} out of range")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            found.append(f"{where}: {value} out of range")
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", len(value)):
            found.append(f"{where}: more than {schema['maxItems']} items")
        for i, item in enumerate(value):
            found += problems(item, schema.get("items", {}), f"{where}[{i}]")
    if isinstance(value, dict) and kind == "object":
        found += [f"{where}: '{name}' missing" for name in schema.get("required", []) if name not in value]
        for name, item in value.items():
            if name in schema.get("properties", {}):
                found += problems(item, schema["properties"][name], f"{where}.{name}")
            elif schema.get("properties") and schema.get("additionalProperties") is not True:
                found.append(f"{where}: '{name}' is not in the contract")
    return found


def request_problems(method: str, path: str, query: dict[str, str], body: Any) -> list[str]:
    """What the real supervisor would reject (or silently ignore) in this request."""
    op = operation(method, path)
    if op is None:
        return [f"{method} {path} is not in the contract"]
    params = {p["name"]: p for p in op.get("parameters", []) if p["in"] == "query"}
    found = [f"query '{name}' is not in the contract" for name in query if name not in params]
    content = (op.get("requestBody") or {}).get("content", {}).get("application/json")
    if content is not None:
        found += problems(body, content["schema"])
    elif body is not None:
        found.append("a body the route doesn't take")
    return found
