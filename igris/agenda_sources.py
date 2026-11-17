"""Read-only calendar sources for the task calendar: Moodle and the shared ESN calendar.

``AgendaSync.run()`` asks every source for its current items, upserts them into the owner's
agenda (idempotent through source_ref) and dismisses items that left a feed, but only open
items (proposed or added) whose date still lies in the window the feed covers; an item the
owner marked done, or one that dropped out of the window because it is in the past, stays.
Each source reports a state the panel shows: ok, not_configured or error, with a reason code.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import threading
import time
from typing import Callable

from igris import gcal, moodle_ical
from igris.agenda import ACTIVE, AgendaError, kind_from_text

ESN_DAYS = 60


class SourceProblem(Exception):
    """A source could not deliver items; ``state`` and ``reason`` go to the panel."""

    def __init__(self, state: str, reason: str, detail: str = ""):
        super().__init__(detail or reason)
        self.state, self.reason, self.detail = state, reason, detail


def _moment(value: str, tz):
    if len(value) == 10:
        return datetime.fromisoformat(value).replace(tzinfo=tz)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(tz)


def sync_items(store, owner: str, source: str, items: list[dict], *, now: datetime,
               until: datetime | None = None) -> dict:
    """Upsert ``items`` of one source and dismiss open items that are no longer in the feed.

    Only items dated from ``now`` (to ``until`` when the feed has an end) are dismissed.
    Returns {"added": n, "updated": n, "dismissed": n, "skipped": n}.
    """
    counts = {"added": 0, "updated": 0, "dismissed": 0, "skipped": 0}
    seen = set()
    for item in items:
        try:
            saved, created = store.upsert(owner, item)
        except AgendaError:
            counts["skipped"] += 1  # one bad event must not stop the rest
            continue
        seen.add(saved["source_ref"])
        counts["added" if created else "updated"] += 1
    tz = now.tzinfo
    for item in store.list(owner):
        if item["source"] != source or item["source_ref"] in seen or item["status"] not in ACTIVE:
            continue
        moment = _moment(item["due"] or item["start"], tz)
        if moment >= now and (until is None or moment <= until):
            store.set_status(owner, item["id"], "dismissed")
            counts["dismissed"] += 1
    return counts


def moodle_source(get_url: Callable[[], str | None], *, opener=None):
    """Items from the Moodle export link; not_configured until the owner stores it."""
    def read(now):
        url = get_url()
        if not url:
            raise SourceProblem("not_configured", "no_link")
        kwargs = {} if opener is None else {"opener": opener}
        try:
            text = moodle_ical.fetch(url, **kwargs)
        except moodle_ical.MoodleError as exc:
            raise SourceProblem("error", "error", str(exc)) from None
        return moodle_ical.items_from_ical(text, default_tz=now.tzinfo), None
    return read


def esn_item(event: dict) -> dict:
    """One shared ESN calendar event as an agenda item for the "esn" branch."""
    def iso(value):
        return value.replace("Z", "+00:00") if isinstance(value, str) else value

    return {"branch": "esn", "kind": kind_from_text(event.get("title"), default="meeting"),
            "title": event.get("title") or "(başlıksız)", "start": iso(event.get("start")),
            "all_day": bool(event.get("all_day")), "location": event.get("location", ""),
            "source": "esn_calendar", "source_ref": event["id"], "mandatory": False,
            "confidence": 1.0, "status": "proposed"}


def esn_source(calendar_ids: tuple[str, ...], client_factory: Callable[[], object | None]):
    """Events of the shared ESN calendar(s) for the next ESN_DAYS days."""
    def read(now):
        if not calendar_ids:
            raise SourceProblem("not_configured", "no_calendar_id")
        client = client_factory()
        if client is None:
            raise SourceProblem("not_configured", "no_google")
        items = []
        for calendar_id in calendar_ids:
            try:
                events = client.upcoming(days=ESN_DAYS, max_results=250, now=now.timestamp(),
                                         calendar_id=calendar_id)
            except gcal.CalendarNotFoundError:
                raise SourceProblem("error", "not_shared") from None
            except gcal.CalendarScopeError:
                raise SourceProblem("error", "needs_consent") from None
            except gcal.CalendarAuthError:
                raise SourceProblem("error", "auth_down") from None
            except gcal.CalendarError as exc:
                raise SourceProblem("error", "error", str(exc)) from None
            items += [esn_item(e) for e in events]
        return items, now + timedelta(days=ESN_DAYS)
    return read


class AgendaSync:
    """Runs every source for one owner and keeps a status per source for the panel."""

    def __init__(self, store, owner: Callable[[], str | None], sources: dict, *,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 clock: Callable[[], float] = time.time):
        self.store, self.owner, self.sources = store, owner, dict(sources)
        self._now, self._clock = now, clock
        self._lock = threading.Lock()
        self._status = {name: {"state": "starting", "reason": "", "last_sync": None, "count": 0}
                        for name in self.sources}

    def status(self) -> dict:
        with self._lock:
            return {name: dict(value) for name, value in self._status.items()}

    def _set(self, name, **value):
        with self._lock:
            self._status[name] = {**self._status[name], **value}

    def run(self) -> dict:
        """One pass over every source; a failing source never stops the others."""
        owner = self.owner()
        now = self._now()
        for name, read in self.sources.items():
            if not owner:
                self._set(name, state="not_configured", reason="no_owner", error="")
                continue
            try:
                items, until = read(now)
                counts = sync_items(self.store, owner, _SOURCE[name], items, now=now, until=until)
            except SourceProblem as exc:
                self._set(name, state=exc.state, reason=exc.reason, error=exc.detail[:200])
                continue
            except Exception as exc:  # a locked file or a parser bug: report, keep running
                self._set(name, state="error", reason="error", error=type(exc).__name__)
                continue
            self._set(name, state="ok", reason="", error="", last_sync=self._clock(),
                      count=len(items), **{"changes": counts})
        return self.status()


_SOURCE = {"moodle": "moodle", "esn": "esn_calendar"}
