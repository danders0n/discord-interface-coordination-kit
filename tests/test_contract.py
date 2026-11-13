"""The fake supervisor against the supervisor's contract (docs/PLAN-front-end.md, *Tests and the fake supervisor*).

The fake serves every route of ``tests/contract/openapi-bot.json`` and nothing else; requests on them are checked by
the fake itself (``contract_check``)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from contract_check import CONTRACT, problems, request_problems, routes
from fake_supervisor import make_app

SUPERVISOR = Path(__file__).resolve().parents[2] / "arma-server-supervisior" / "docs" / "api" / "openapi-bot.json"


def fake_routes() -> set[tuple[str, str]]:
    found = set()
    for route in make_app().router.routes():
        if route.method == "HEAD":
            continue
        info = route.resource.get_info()
        path = re.sub(r"\{(\w+)(:[^}]*)?\}", r"{\1}", info.get("formatter") or info.get("path"))
        found.add((route.method, path))
    return found


def test_the_fake_serves_every_contract_route():
    assert routes() - fake_routes() == set()


def test_the_fake_serves_nothing_outside_the_contract():
    assert fake_routes() - routes() == set()


def test_the_copy_is_the_supervisors_current_contract():
    if not SUPERVISOR.is_file():
        pytest.skip("the supervisor's checkout is not next to this one")
    assert CONTRACT.read_bytes() == SUPERVISOR.read_bytes(), "run python scripts/sync_contract.py"


def test_the_check_catches_what_the_supervisor_would_refuse():
    member = "/api/members/{discord_id}"
    assert request_problems("PUT", member, {}, {"username": "anna", "roles": ["301"]}) == []
    assert request_problems("PUT", member, {}, {"roles": [301]})  # no username, a number for an id
    assert request_problems("POST", "/api/notices/{notice_id}/done", {}, {"state": "sent"})  # not in the enum
    assert request_problems("GET", "/api/notices", {"since": "3"}, None)  # an unknown query name
    assert request_problems("DELETE", member, {}, None)  # not a route
    assert problems({"members": [{"discord_id": "x", "username": "a"}]}, {"$ref": "#/components/schemas/RosterBody"})


async def test_the_fake_answers_a_bad_request_like_the_supervisor(services, fake):
    from app.supervisor_client import SupervisorError

    with pytest.raises(SupervisorError) as caught:
        await services.supervisor.request("PUT", "/api/members/11", json={"display_name": "no username"})
    assert caught.value.status == 422 and "contract" in caught.value.message
