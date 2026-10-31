"""Application logging (docs/PLAN.md, *Code and tooling*): ``data/logs/bot.log`` rotated daily and kept
``logs.keep_days``, and stdout, which NSSM writes to ``data/service.log``. Every ERROR gets a short id, written
in the file and posted in short to the staff log, so staff can find the full trace."""

from __future__ import annotations

import logging
import secrets
import sys
from collections.abc import Callable
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from app.i18n import t
from app.staff_log import StaffLog

FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(error_id)s] %(message)s"
_base_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs) -> logging.LogRecord:
    record = _base_factory(*args, **kwargs)
    record.error_id = secrets.token_hex(4) if record.levelno >= logging.ERROR else "-"
    return record


class StaffLogHandler(logging.Handler):
    """ERRORs to the staff log, one line each: the id, where, and the message."""

    def __init__(self, staff: StaffLog, language: Callable[[], str]) -> None:
        super().__init__(logging.ERROR)
        self.staff = staff
        self.language = language

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("app.staff_log"):
            return
        try:
            message = record.getMessage().splitlines()[0][:300]
            self.staff.post(t("⚠ Error `{id}` in `{where}`: {message}", self.language(),
                              id=record.error_id, where=record.name, message=message))
        except Exception:  # noqa: BLE001
            self.handleError(record)


def setup(data_dir: Path, keep_days: int, staff: StaffLog | None = None, language: Callable[[], str] = lambda: "en",
          level: int = logging.INFO) -> None:
    logging.setLogRecordFactory(_record_factory)
    folder = data_dir / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(FORMAT)
    handlers: list[logging.Handler] = [
        TimedRotatingFileHandler(folder / "bot.log", when="midnight", backupCount=keep_days, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    for handler in handlers:
        handler.setFormatter(formatter)
    if staff is not None:
        handlers.append(StaffLogHandler(staff, language))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.INFO)
