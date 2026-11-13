"""Dates and times as organisers type them (docs/PLAN.md, *Creating an event*, *Date*, *Times*).

``dd.mm`` is the **next** such date (this year if it hasn't passed, otherwise next year); ``dd.mm.yyyy`` is taken
as it is; a date more than ``max_days`` ahead is refused as a likely typo. Times are ``HH:MM`` in the configured
zone: the assembly is on the event date, and a start earlier in the day than the assembly is on the next day.
Everything comes back as aware UTC. Problems are :class:`TimeError` codes for the Discord layer to word."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from enum import StrEnum

DATE = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?\s*$")
TIME = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


class Problem(StrEnum):
    BAD_DATE = "bad_date"
    PAST = "past"
    TOO_FAR = "too_far"
    BAD_TIME = "bad_time"
    BAD_DURATION = "bad_duration"


class TimeError(ValueError):
    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.value)
        self.problem = problem


@dataclass(frozen=True)
class Times:
    assembly_at: datetime
    start_at: datetime
    duration_minutes: int

    @property
    def ends_at(self) -> datetime:
        return self.start_at + timedelta(minutes=self.duration_minutes)


def parse_date(text: str, now: datetime, tz: tzinfo, max_days: int) -> date:
    match = DATE.match(text)
    if not match:
        raise TimeError(Problem.BAD_DATE)
    day, month, year = int(match[1]), int(match[2]), match[3]
    today = now.astimezone(tz).date()
    try:
        found = date(int(year), month, day) if year else date(today.year, month, day)
        if not year and found < today:
            found = date(today.year + 1, month, day)
    except ValueError as exc:  # 31.02, or 29.02 in a year without it
        raise TimeError(Problem.BAD_DATE) from exc
    if found < today:
        raise TimeError(Problem.PAST)
    if found > today + timedelta(days=max_days):
        raise TimeError(Problem.TOO_FAR)
    return found


def parse_time(text: str) -> time:
    match = TIME.match(text)
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise TimeError(Problem.BAD_TIME)
    return time(int(match[1]), int(match[2]))


def parse_duration(text: str | int) -> int:
    """Minutes, from ``180``, ``3h``, ``3:30`` or ``2h30``."""
    if isinstance(text, int):
        minutes = text
    else:
        value = text.strip().lower().replace(" ", "")
        match = re.fullmatch(r"(\d+)(?:h|:)(\d{1,2})?m?|(\d+)m?", value)
        if not match:
            raise TimeError(Problem.BAD_DURATION)
        minutes = int(match[3]) if match[3] else int(match[1]) * 60 + int(match[2] or 0)
    if not 15 <= minutes <= 24 * 60:
        raise TimeError(Problem.BAD_DURATION)
    return minutes


def combine(day: date, assembly: time, start: time, duration_minutes: int, tz: tzinfo) -> Times:
    assembly_at = datetime.combine(day, assembly, tz)
    start_day = day + timedelta(days=1) if start < assembly else day
    start_at = datetime.combine(start_day, start, tz)
    return Times(assembly_at.astimezone(UTC), start_at.astimezone(UTC), duration_minutes)


def parse(date_text: str, assembly_text: str, start_text: str, duration: str | int, now: datetime, tz: tzinfo,
          max_days: int) -> Times:
    """The wizard's four fields at once."""
    return combine(parse_date(date_text, now, tz, max_days), parse_time(assembly_text), parse_time(start_text),
                   parse_duration(duration), tz)
