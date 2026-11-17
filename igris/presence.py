"""User presence state machine.

The tracker is fed by device reports.  A report only carries a coarse
activity *category* and the number of idle seconds; window titles and
keystrokes never reach this module (privacy rule).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

CATEGORIES = ("coding", "studying", "browsing", "media", "other")
STATES = ("deep_work", "study", "idle", "away", "sleeping")

# Category -> resulting state when the user is not idle.
_CATEGORY_STATES = {"coding": "deep_work", "studying": "study"}

ChangeCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class Report:
    """A single activity report sent by one device."""

    device: str
    category: str
    idle_seconds: float
    at: float

    def __post_init__(self) -> None:
        if not isinstance(self.device, str) or not self.device.strip():
            raise ValueError("device must be a non-empty string")
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category: {self.category!r}")
        for name in ("idle_seconds", "at"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
        if self.idle_seconds < 0:
            raise ValueError("idle_seconds must not be negative")


def _default_local_hour(timestamp: float) -> int:
    """Return the local hour of the given timestamp."""
    return time.localtime(timestamp).tm_hour


class PresenceTracker:
    """Derive the current presence state from fresh device reports."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        stale_after: float = 300.0,
        idle_after: float = 600.0,
        quiet_hours: tuple[float, float] = (1, 7),
        local_hour: Callable[[float], int] | None = None,
    ) -> None:
        if stale_after < 0:
            raise ValueError("stale_after must not be negative")
        if idle_after < 0:
            raise ValueError("idle_after must not be negative")
        self._clock = clock
        self._stale_after = stale_after
        self._idle_after = idle_after
        self._quiet_hours = quiet_hours
        self._local_hour = local_hour or _default_local_hour
        self._lock = threading.RLock()
        self._reports: dict[str, Report] = {}
        self._listeners: list[ChangeCallback] = []
        self._state: str | None = None
        self._device: str | None = None
        self._since: float | None = None

    # ---------------------------------------------------------------- public

    def update(self, report: Report) -> None:
        """Feed one device report into the tracker."""
        with self._lock:
            now = self._clock()
            if now - report.at > self._stale_after:
                # Report is already too old to say anything about now.
                return
            self._reports[report.device] = report
            self._prune(now)
            changes = self._evaluate(now)
        self._notify(changes)

    def state(self) -> dict:
        """Return the current state as ``{'state', 'device', 'since'}``.

        Reports going stale change the state with no new report, so a read is
        also where such a change is noticed and on_change callbacks fire.
        """
        with self._lock:
            now = self._clock()
            self._prune(now)
            changes = self._evaluate(now)
            snapshot = {
                "state": self._state,
                "device": self._device,
                "since": self._since,
            }
        self._notify(changes)
        return snapshot

    def on_change(self, callback: ChangeCallback) -> ChangeCallback:
        """Register a callback invoked with ``(old, new)`` on real changes."""
        with self._lock:
            self._listeners.append(callback)
        return callback

    # --------------------------------------------------------------- private

    def _prune(self, now: float) -> None:
        stale = [
            device
            for device, report in self._reports.items()
            if now - report.at > self._stale_after
        ]
        for device in stale:
            del self._reports[device]

    def _is_quiet(self, hour: float) -> bool:
        start, end = self._quiet_hours
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    def _compute(self, now: float) -> tuple[str, str | None]:
        fresh = self._reports.values()
        if not fresh:
            if self._is_quiet(self._local_hour(now)):
                return "sleeping", None
            return "away", None
        # The most active device wins; ties go to the freshest report.
        winner = min(fresh, key=lambda report: (report.idle_seconds, -report.at))
        if winner.idle_seconds >= self._idle_after:
            return "idle", winner.device
        return _CATEGORY_STATES.get(winner.category, "idle"), winner.device

    def _evaluate(self, now: float) -> list[tuple[str, str]]:
        """Refresh internal state; return the real transitions that happened."""
        state, device = self._compute(now)
        if state == self._state:
            self._device = device
            return []
        old = self._state
        self._state = state
        self._device = device
        self._since = now
        if old is None:
            # The very first evaluation is not a change.
            return []
        return [(old, state)]

    def _notify(self, changes: Iterable[tuple[str, str]]) -> None:
        changes = list(changes)
        if not changes:
            return
        with self._lock:
            listeners = list(self._listeners)
        for old, new in changes:
            for callback in listeners:
                callback(old, new)
