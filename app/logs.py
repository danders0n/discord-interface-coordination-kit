"""Application logging (docs/PLAN.md, *Code and tooling*): ``data/logs/bot.log`` rotated daily and kept
``logs.keep_days``, and stdout, which NSSM writes to ``data/service.log``. Every ERROR gets a short id, written
in the file and posted in short to the staff log, so staff can find the full trace.

Records are written by a thread of their own: the event loop only queues them. A write can block for minutes (a
console window with text selected in it holds every write until the selection ends), and blocking the loop drops
the Discord connection."""

from __future__ import annotations

import atexit
import logging
import queue
import secrets
import sys
from collections.abc import Callable
from logging.handlers import QueueHandler, QueueListener, TimedRotatingFileHandler
from pathlib import Path

from app.i18n import t
from app.staff_log import StaffLog

FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(error_id)s] %(message)s"
MAX_WAITING = 10_000  # records queued for the writer; more are dropped while it is stuck
_base_factory = logging.getLogRecordFactory()
_listener: QueueListener | None = None


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


class _Enqueue(QueueHandler):
    """Hands a record to the writer thread without waiting; a full queue drops it (reporting that would write to
    stderr, which may be what is stuck)."""

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            pass


class _QuietReset(logging.Filter):
    """asyncio's Windows loop logs an ERROR with a trace when it closes a connection the other side has reset
    already (``ConnectionResetError`` in ``_call_connection_lost``). Nothing is lost there: a WARNING line instead,
    so it doesn't reach the staff log as an error."""

    def filter(self, record: logging.LogRecord) -> bool:
        error = record.exc_info[1] if record.exc_info else None
        if record.levelno >= logging.ERROR and isinstance(error, ConnectionResetError):
            record.levelno, record.levelname, record.error_id = logging.WARNING, "WARNING", "-"
            record.msg, record.args = "A connection was reset by the other side: %s", (error,)
            record.exc_info = record.exc_text = None
        return True


_quiet_reset = _QuietReset()


def stop() -> None:
    """Write what is queued and stop the writer thread (at exit, and before a new :func:`setup`)."""
    global _listener
    if _listener is None:
        return
    _listener.stop()
    for handler in _listener.handlers:
        handler.close()
    _listener = None


atexit.register(stop)


def setup(data_dir: Path, keep_days: int, staff: StaffLog | None = None, language: Callable[[], str] = lambda: "en",
          level: int = logging.INFO) -> None:
    global _listener
    stop()
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
    waiting: queue.Queue[logging.LogRecord] = queue.Queue(MAX_WAITING)
    _listener = QueueListener(waiting, *handlers, respect_handler_level=True)
    _listener.start()
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(_Enqueue(waiting))
    root.setLevel(level)
    logging.getLogger("asyncio").addFilter(_quiet_reset)
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.client").setLevel(logging.INFO)
