"""Restarting the bot from inside: it closes the Discord client and exits with ``boot.RESTART_CODE``, and whatever runs
it starts it again (docs/PLAN-remote-update.md, *Exiting*). :func:`detect` says what that is, if anything;
``python -m app`` (``app/cli.py``) installs the handler that closes the bot. Standard library only."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from app.boot import RESTART_CODE, RESTARTER_ENV

__all__ = ["RESTART_CODE", "can_exit", "detect", "exit_requested", "request_exit", "set_exit_handler"]

RESTARTERS = ("console", "nssm")
_handler: Callable[[], None] | None = None
_requested = False


def set_exit_handler(handler: Callable[[], None] | None) -> None:
    """How to end the bot (``cli.run``: close the client); None when nothing runs through it."""
    global _handler, _requested
    _handler, _requested = handler, False


def can_exit() -> bool:
    return _handler is not None


def request_exit() -> bool:
    """End the bot; ``python -m app`` then exits with ``RESTART_CODE``. False when it was not started that way."""
    global _requested
    if _handler is None:
        return False
    _requested = True
    _handler()
    return True


def exit_requested() -> bool:
    return _requested


def detect(environ: Any = None) -> str | None:
    """``console`` (the loop of ``python -m app`` in a terminal), ``nssm`` (``BOT_RESTARTER=nssm`` in the service's
    environment: ``nssm set EventBot AppEnvironmentExtra BOT_RESTARTER=nssm``), ``systemd``, or None: nothing known
    would start the bot again after it exits."""
    environ = os.environ if environ is None else environ
    value = str(environ.get(RESTARTER_ENV) or "").strip().lower()
    if value in RESTARTERS:
        return value
    if environ.get("INVOCATION_ID"):
        return "systemd"
    return None
