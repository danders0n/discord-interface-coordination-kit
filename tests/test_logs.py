import asyncio
import logging
import re

import pytest

from app import logs
from app.staff_log import StaffLog, chunks


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level, factory = list(root.handlers), root.level, logging.getLogRecordFactory()
    yield
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)
    logging.setLogRecordFactory(factory)


async def test_an_error_has_one_id_in_the_file_and_the_staff_log(tmp_path, restore_logging):
    staff = StaffLog()
    staff.bind(asyncio.get_running_loop())
    logs.setup(tmp_path, 14, staff, lambda: "en")
    logging.getLogger("app.events").error("Reservation failed: 409 overlap\nmore detail")
    logging.getLogger("app.events").warning("only a warning")
    logging.getLogger("app.staff_log").error("could not post")  # never sent back to the staff log
    await asyncio.sleep(0)
    posted = staff.drain()
    assert len(posted) == 1
    error_id = re.search(r"`([0-9a-f]{8})`", posted[0]).group(1)
    assert "app.events" in posted[0] and "more detail" not in posted[0]
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (tmp_path / "logs" / "bot.log").read_text(encoding="utf-8")
    assert f"[{error_id}] Reservation failed" in content
    assert "[-] only a warning" in content


async def test_the_staff_log_drops_the_oldest_when_full():
    staff = StaffLog()
    staff.bind(asyncio.get_running_loop())
    for n in range(1050):
        staff.post(str(n))
    await asyncio.sleep(0)
    notes = staff.drain()
    assert len(notes) == 1000 and notes[0] == "50" and notes[-1] == "1049"


async def test_sending_continues_after_a_failure():
    staff, sent = StaffLog(), []
    staff.bind(asyncio.get_running_loop())

    async def send(text):
        if text == "bad":
            raise RuntimeError("channel gone")
        sent.append(text)

    task = asyncio.create_task(staff.run(send))
    staff.post("bad")
    await asyncio.sleep(0.01)
    staff.post("good")
    await asyncio.sleep(0.01)
    task.cancel()
    assert sent == ["good"]


async def test_waiting_notes_go_out_together():
    staff, sent = StaffLog(), []
    staff.bind(asyncio.get_running_loop())

    async def send(text):
        sent.append(text)

    for n in range(3):
        staff.post(f"note {n}")
    task = asyncio.create_task(staff.run(send))
    await asyncio.sleep(0.01)
    staff.post("x" * 1500)
    staff.post("y" * 1500)
    await asyncio.sleep(0.01)
    task.cancel()
    assert sent == ["note 0\nnote 1\nnote 2", "x" * 1500, "y" * 1500]


def test_long_notes_split_at_line_ends():
    text = "\n".join(["x" * 900] * 5)
    parts = chunks(text, 2000)
    assert all(len(p) <= 2000 for p in parts) and "".join(parts).replace("\n", "") == text.replace("\n", "")
    assert chunks("y" * 4500, 2000) == ["y" * 2000, "y" * 2000, "y" * 500]
