"""Keeps Google Calendar in step with the task calendar (Görev Takvimi).

Igris writes only to three calendars it creates itself ("Igris · Okul", "Igris · Kişisel",
"Igris · ESN"); the owner's own calendars and events are never touched. Every event carries
``extendedProperties.private.igris_ref`` = the agenda item id, so a sync is a reconcile:

- an item with status "added" gets one event in its branch's calendar, created or updated
  until title, time, place and reminders match;
- an event whose item is gone, done, dismissed, no longer "added" or moved to another
  branch is deleted (events without an igris_ref are left alone).

Running it twice changes nothing the second time. ``request()`` asks for a sync soon (the
agenda calls it after every change); ``run()`` also syncs every ``interval`` seconds as a
safety net. A large change (a timetable import of hundreds of items) is written in the
background thread: after the first ``burst`` writes of one sync every write waits ``pause``
seconds, so Google's per-user rate limit is not hit; a sync cut short by an API error
resumes on the next one without duplicates (events are found by their igris_ref). Problems end up in ``status()``, which the Görev Takvimi panel shows; nothing
here raises into a web request.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Callable

from igris import gcal

CALENDAR_NAMES = {"okul": "Igris · Okul", "kisisel": "Igris · Kişisel", "esn": "Igris · ESN"}
REF_KEY = "igris_ref"
KIND_TEXT = {"exam": "Sınav", "assignment": "Ödev", "lab": "Lab", "presentation": "Sunum",
             "attendance": "Yoklama", "payment": "Ödeme", "meeting": "Toplantı", "lesson": "Ders",
             "study": "Study", "other": "Diğer"}
DAY, HOUR = 1440, 60
# minutes before the event; "payment" also gets 09:00 on the day (see reminders())
REMINDERS = {
    "exam": (7 * DAY, DAY, 2 * HOUR),
    "assignment": (3 * DAY, DAY),
    "lab": (3 * DAY, DAY),
    "presentation": (3 * DAY, DAY),
    "attendance": (HOUR,),
    "payment": (DAY,),
    "meeting": (DAY, HOUR),
    "lesson": (30,),  # an imported class: half an hour before, not a day
    "study": (HOUR,),
    "other": (DAY,),
}
MAX_REMINDER = 40320  # Google's limit: four weeks
MORNING = 9 * HOUR    # 09:00
POINT = timedelta(minutes=30)  # length of an event that has a single moment (a deadline)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_date(value) -> bool:
    return isinstance(value, str) and bool(_DATE.fullmatch(value))


def is_all_day(item: dict) -> bool:
    values = [v for v in (item.get("start"), item.get("due")) if v]
    return bool(item.get("all_day")) or all(_is_date(v) for v in values)


def reminders(item: dict) -> list[int]:
    """Popup reminder minutes for an item, newest-first order irrelevant, at most five.

    All-day events count from 00:00 of their day, so "N days before" becomes 09:00 N days
    earlier and anything shorter than a day becomes 09:00 the day before.
    """
    base = REMINDERS.get(item.get("kind"), REMINDERS["other"])
    if is_all_day(item):
        minutes = {m - MORNING if m >= DAY else DAY - MORNING for m in base}
    else:
        minutes = set(base)
        if item.get("kind") == "payment":
            moment = _timed(item)[0]
            since_morning = moment.hour * HOUR + moment.minute - MORNING
            if since_morning > 0:
                minutes.add(since_morning)
    return sorted((m for m in minutes if 0 <= m <= MAX_REMINDER), reverse=True)[:5]


def _timed(item: dict) -> tuple[datetime, datetime]:
    moments = [datetime.fromisoformat(v) for v in (item.get("start"), item.get("due"))
               if v and not _is_date(v)]
    first = moments[0]
    last = moments[-1] if len(moments) == 2 and moments[-1] > first else first + POINT
    return first, last


def event_body(item: dict) -> dict:
    """The Google Calendar event an agenda item should have."""
    if is_all_day(item):
        days = [v[:10] for v in (item.get("start"), item.get("due")) if v]
        first, last = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
        if last < first:
            last = first
        start = {"date": first.isoformat()}
        end = {"date": (last + timedelta(days=1)).isoformat()}
    else:
        first, last = _timed(item)
        start, end = {"dateTime": first.isoformat()}, {"dateTime": last.isoformat()}
    kind = KIND_TEXT.get(item.get("kind"), KIND_TEXT["other"])
    return {
        "summary": f"{kind}: {item.get('title', '')}",
        "description": "Igris Görev Takvimi",
        "location": item.get("location") or "",
        "start": start,
        "end": end,
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": m} for m in reminders(item)]},
        "extendedProperties": {"private": {REF_KEY: item["id"]}},
    }


def _ref(event: dict):
    private = (event.get("extendedProperties") or {}).get("private") or {}
    ref = private.get(REF_KEY) if isinstance(private, dict) else None
    return ref if isinstance(ref, str) and ref else None


def _when(value: dict):
    if not isinstance(value, dict):
        return None
    if value.get("date"):
        return ("date", value["date"])
    try:
        return ("time", datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00")))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def same_event(existing: dict, wanted: dict) -> bool:
    """True when Google's copy already shows what the agenda says (instants, not strings)."""
    for key in ("summary", "description", "location"):
        if (existing.get(key) or "") != (wanted.get(key) or ""):
            return False
    if _when(existing.get("start")) != _when(wanted["start"]) \
            or _when(existing.get("end")) != _when(wanted["end"]):
        return False
    have = existing.get("reminders") or {}
    if have.get("useDefault") is not False:
        return False
    minutes = sorted(o.get("minutes") for o in have.get("overrides") or [] if isinstance(o, dict))
    return minutes == sorted(o["minutes"] for o in wanted["reminders"]["overrides"])


class CalendarSync:
    def __init__(self, agenda, client_factory: Callable[[], object | None],
                 owner: str | Callable[[], str | None], state_path: Path, *,
                 interval: float = 300, debounce: float = 2.0,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 burst: int = 20, pause: float = 0.25, sleep: Callable[[float], None] = time.sleep):
        self.agenda, self._factory, self._owner = agenda, client_factory, owner
        self.state_path, self.interval, self.debounce, self._now = Path(state_path), interval, debounce, now
        self.burst, self.pause, self._sleep = burst, pause, sleep
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._status = {"state": "starting", "last_sync": None, "error": None,
                        "created": 0, "updated": 0, "deleted": 0}

    # ---- public ----

    def status(self) -> dict:
        return dict(self._status)

    def owner(self) -> str | None:
        return self._owner() if callable(self._owner) else self._owner

    def request(self, owner: str | None = None) -> None:
        """Ask for a sync soon; an owner other than the calendar's owner changes nothing."""
        if owner is None or owner == self.owner():
            self._wake.set()

    def run(self, stop: threading.Event) -> None:
        """Sync now, after every request and every ``interval`` seconds until ``stop``."""
        while not stop.is_set():
            self._wake.clear()  # a change made during the sync wakes the next one
            self.sync()
            deadline = time.monotonic() + self.interval
            while not stop.is_set() and time.monotonic() < deadline:
                if self._wake.wait(min(1.0, max(deadline - time.monotonic(), 0))):
                    stop.wait(self.debounce)  # one sync for a burst of changes
                    break

    def sync(self) -> dict:
        with self._lock:
            counts = {"created": 0, "updated": 0, "deleted": 0}
            try:
                state, error = self._sync(counts), None
            except gcal.CalendarScopeError:
                state, error = "needs_consent", None
            except gcal.CalendarAuthError:
                state, error = "auth_down", None
            except gcal.CalendarError as exc:
                state, error = "error", str(exc)[:200]
            except Exception as exc:  # a locked database or a bad row must not end the thread
                state, error = "error", type(exc).__name__
            self._status = {"state": state, "error": error,
                            "last_sync": self._now().isoformat(timespec="seconds"), **counts}
            return self.status()

    # ---- the reconcile ----

    def _pace(self, counts: dict) -> None:
        """Wait before a write once this sync has made ``burst`` writes."""
        if counts["created"] + counts["updated"] + counts["deleted"] >= self.burst:
            self._sleep(self.pause)

    def _sync(self, counts: dict) -> str:
        owner = self.owner()
        if not owner:
            return "no_owner"
        client = self._factory()
        if client is None:
            return "not_configured"
        client.require_write()
        items = self.agenda.list(owner, include_done=True)
        by_id = {it["id"]: it for it in items}
        wanted = {it["id"]: it for it in items if it["status"] == "added"}
        calendars = self._load()
        found = {}  # ref -> (calendar id, event)
        for branch, calendar_id in list(calendars.items()):
            if not client.calendar_exists(calendar_id):  # the owner deleted it: start over
                del calendars[branch]
                self._save(calendars)
                continue
            for event in client.list_events(calendar_id):
                ref = _ref(event)
                if ref is None:
                    continue
                item = wanted.get(ref)
                if item is None or item["branch"] != branch or ref in found:
                    self._pace(counts)
                    client.delete_event(calendar_id, event["id"])
                    counts["deleted"] += 1
                    old = by_id.get(ref)
                    if old is not None and old.get("calendar_event_id") == event.get("id"):
                        self.agenda.set_event_id(owner, ref, None)
                    continue
                found[ref] = (calendar_id, event)
        zone = []  # the owner's time zone, read once and only when a calendar is created
        for ref, item in wanted.items():
            body = event_body(item)
            if ref in found:
                calendar_id, event = found[ref]
                if not same_event(event, body):
                    self._pace(counts)
                    client.update_event(calendar_id, event["id"], body)
                    counts["updated"] += 1
                event_id = event["id"]
            else:
                calendar_id = calendars.get(item["branch"])
                if calendar_id is None:
                    if not zone:
                        zone.append(client.primary_time_zone())
                    calendar_id = client.create_calendar(CALENDAR_NAMES[item["branch"]], zone[0])
                    calendars[item["branch"]] = calendar_id
                    self._save(calendars)
                self._pace(counts)
                event_id = client.insert_event(calendar_id, body).get("id")
                counts["created"] += 1
            if isinstance(event_id, str) and item.get("calendar_event_id") != event_id:
                self.agenda.set_event_id(owner, ref, event_id)
        return "ok"

    # ---- the remembered calendar ids ----

    def _load(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        calendars = data.get("calendars") if isinstance(data, dict) else None
        if not isinstance(calendars, dict):
            return {}
        return {b: c for b, c in calendars.items()
                if b in CALENDAR_NAMES and isinstance(c, str) and c}

    def _save(self, calendars: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.state_path.parent, prefix=".gcal-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"calendars": calendars}, out)
            os.replace(temp, self.state_path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
