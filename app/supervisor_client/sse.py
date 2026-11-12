"""Server-Sent Events as the supervisor's ``GET /api/events/stream`` writes them: ``id:``, ``event:`` and one
``data:`` line of JSON per message, a blank line after it, and ``: keep-alive`` comments between messages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class StreamEvent:
    event: str  # hello, notice, member, operation ...
    data: dict[str, Any] = field(default_factory=dict)
    id: str | None = None  # '<boot>:<seq>'; hello has none


class SseParser:
    """Fed one line at a time; returns a :class:`StreamEvent` at the blank line that ends a message."""

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._event, self._id, self._data = "message", None, []

    def feed(self, line: str) -> StreamEvent | None:
        line = line.rstrip("\r\n")
        if not line:
            if not self._data:
                self._reset()
                return None
            text = "\n".join(self._data)
            try:
                data = json.loads(text)
            except ValueError:
                data = {"text": text}
            event = StreamEvent(self._event, data if isinstance(data, dict) else {"value": data}, self._id)
            self._reset()
            return event
        if line.startswith(":"):
            return None  # a comment (keep-alive)
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            self._event = value
        elif name == "id":
            self._id = value
        elif name == "data":
            self._data.append(value)
        return None
