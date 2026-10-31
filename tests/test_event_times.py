"""Dates and times as organisers type them (docs/PLAN.md, *Creating an event*, *Date*, *Times*)."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.events.times import Problem, TimeError, combine, parse, parse_date, parse_duration, parse_time

WARSAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)


def test_dd_mm_is_the_next_such_date():
    assert parse_date("18.11", NOW, WARSAW, 120) == date(2026, 11, 18)
    assert parse_date("31.10", NOW, WARSAW, 120) == date(2026, 10, 31)  # today still counts
    assert parse_date("5.1", NOW, WARSAW, 120) == date(2027, 1, 5)  # passed this year: next year
    assert parse_date("05-01-2027", NOW, WARSAW, 120) == date(2027, 1, 5)


@pytest.mark.parametrize(("text", "problem"), [("18.10.2026", Problem.PAST), ("1.6", Problem.TOO_FAR),
                                               ("31.02", Problem.BAD_DATE), ("jutro", Problem.BAD_DATE),
                                               ("1.1.2028", Problem.TOO_FAR)])
def test_bad_dates(text, problem):
    with pytest.raises(TimeError) as caught:
        parse_date(text, NOW, WARSAW, 120)
    assert caught.value.problem == problem


def test_times_and_durations():
    assert parse_time("19:00") == time(19) and parse_time(" 7.30 ") == time(7, 30)
    for bad in ("24:00", "19", "19:60", "ab:cd"):
        with pytest.raises(TimeError):
            parse_time(bad)
    assert [parse_duration(v) for v in ("180", "3h", "3:30", "2h30", "90m", 120)] == [180, 180, 210, 150, 90, 120]
    for bad in ("10", "25h", "abc"):
        with pytest.raises(TimeError):
            parse_duration(bad)


def test_a_start_before_the_assembly_is_the_next_day():
    times = combine(date(2026, 11, 18), time(23, 30), time(0, 15), 120, WARSAW)
    assert times.assembly_at == datetime(2026, 11, 18, 22, 30, tzinfo=UTC)
    assert times.start_at == datetime(2026, 11, 18, 23, 15, tzinfo=UTC)
    assert times.ends_at == datetime(2026, 11, 19, 1, 15, tzinfo=UTC)


def test_local_time_follows_summer_and_winter():
    winter = parse("18.11", "19:00", "19:30", "3h", NOW, WARSAW, 300)
    summer = parse("18.03.2027", "19:00", "19:30", "3h", NOW, WARSAW, 300)
    assert winter.assembly_at.hour == 18 and summer.assembly_at.hour == 18  # CET
    july = parse("1.07.2027", "19:00", "19:30", "3h", NOW, WARSAW, 300)
    assert july.assembly_at.hour == 17  # CEST
