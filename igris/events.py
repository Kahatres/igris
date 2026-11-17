"""Server-sent events that tell the web page which data changed.

A stream watches named data sources (zero-argument callables returning JSON-serialisable
data) and writes a ``change`` event listing the names whose data differs since the last
look, so the page refetches only those. Standard library only; clock and sleep are
injectable so tests never wait.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any, Callable, Mapping

_NAME = re.compile(r"[a-z0-9_]{1,32}")


def _single_line(value: str, label: str) -> None:
    if "\r" in value or "\n" in value:
        raise ValueError(f"{label} must not contain CR or LF")


def format_event(event: str, data: Any, *, event_id: int | None = None,
                 retry: int | None = None) -> bytes:
    """Build one SSE message: retry, id, event, then one ``data:`` line per text line."""
    if not isinstance(event, str) or not event:
        raise ValueError("event must be a non-empty string")
    _single_line(event, "event")
    lines = []
    if retry is not None:
        if not isinstance(retry, int) or isinstance(retry, bool) or retry <= 0:
            raise ValueError("retry must be a positive integer (milliseconds)")
        lines.append(f"retry: {retry}")
    if event_id is not None:
        if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 0:
            raise ValueError("event_id must be a non-negative integer")
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False,
                                                         separators=(",", ":"))
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        lines.append(f"data: {line}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def comment(text: str = "ping") -> bytes:
    """A keep-alive comment line that browsers ignore."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    _single_line(text, "text")
    return f": {text}\n\n".encode("utf-8")


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ChangeWatcher:
    """Remembers a digest per source and reports which sources changed."""

    def __init__(self, sources: Mapping[str, Callable[[], Any]]):
        if not isinstance(sources, Mapping) or not sources:
            raise ValueError("sources must be a non-empty mapping")
        for name, source in sources.items():
            if not isinstance(name, str) or _NAME.fullmatch(name) is None:
                raise ValueError("source names must match [a-z0-9_]{1,32}")
            if not callable(source):
                raise ValueError(f"source {name} must be callable")
        self._sources = dict(sources)
        self._digests: dict[str, str] = {}
        self.errors: dict[str, str] = {}

    def poll(self) -> list[str]:
        """Call every source; return the names whose data changed, in mapping order."""
        changed = []
        for name, source in self._sources.items():
            try:
                digest = _digest(source())
                self.errors.pop(name, None)
            except Exception as exc:  # a failing source is reported, never raised
                digest = "error:" + type(exc).__name__
                self.errors[name] = str(exc) or type(exc).__name__
            if self._digests.get(name) != digest:
                self._digests[name] = digest
                changed.append(name)
        return changed


def _positive(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be a positive finite number")


def stream(watcher: ChangeWatcher, write: Callable[[bytes], Any], *, interval: float = 2.0,
           heartbeat: float = 15.0, max_seconds: float = 300.0, retry_ms: int = 3000,
           sleep: Callable[[float], Any] = time.sleep,
           clock: Callable[[], float] = time.monotonic) -> int:
    """Write change events for one response; return how many were written.

    Stops after ``max_seconds`` (the browser reconnects by itself) or when the client
    leaves (``write`` raises OSError).
    """
    _positive(interval, "interval")
    _positive(heartbeat, "heartbeat")
    _positive(max_seconds, "max_seconds")
    start = clock()
    sent = 0
    try:
        write(format_event("hello", {"retry": retry_ms}, retry=retry_ms))
        last_write = clock()
        while clock() - start < max_seconds:
            changed = watcher.poll()
            if changed:
                sent += 1
                write(format_event("change", {"keys": changed, "errors": sorted(watcher.errors)},
                                   event_id=sent))
                last_write = clock()
            elif clock() - last_write >= heartbeat:
                write(comment())
                last_write = clock()
            sleep(interval)
    except OSError:
        pass
    return sent
