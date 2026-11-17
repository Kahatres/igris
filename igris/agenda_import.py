"""Timetable import for the task calendar (Görev Takvimi): a semester of lessons in one call.

``agenda_add`` takes one call per handful of items, and a turn has at most 8 tool calls, so a
weekly class timetable needs its own tool. ``agenda_import`` takes a list of entries (one per
weekly slot, with an optional weekly repeat) or iCalendar text, expands every repeat into one
agenda item per occurrence (the agenda and the Google Calendar sync know only single items, so
the sync needs no second path) and writes them in one transaction through
``AgendaStore.replace_refs``.

Identity: every item is a manual entry with source_ref
``import:<batch>:<YYYY-MM-DD>:<digest>``, the digest of title, branch and start time. Importing
the same batch again updates matching items in place (status and calendar event stay), adds
new ones and removes future ones that are no longer in the timetable; past items and items
the owner marked done are kept, and when an iCalendar event could not be read nothing is
removed (it may be one of the stored lessons). ``agenda_import_remove`` deletes a batch but
keeps its done items unless asked. Lessons get the kind "lesson" (a short reminder) unless
their title names an exam, lab and the like.

Safety: ``preview`` is the default and writes nothing; it shows the count, first and last
date, a per-weekday summary and overlaps with the owner's timed items (Görev Takvimi, and the
owner's Google Calendar when it is connected) and within the import. Validation errors name the
entry; when one entry is invalid nothing is written. Occurrences that are over are skipped.

Time zones: lessons keep their local clock time across daylight saving changes. A time without
an offset is the owner's wall clock (``zone``; None means this machine's local zone, which
Python resolves with its DST rules even without a tz database). iCalendar TZID names are read
with ``zoneinfo`` when the machine has the tz database, else as the owner's local time (and
the result says so).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Callable

from igris.agenda import BRANCHES, IMPORT_PREFIX, KINDS, LESSON, AgendaError, kind_from_text
from igris.moodle_ical import split_property, unfold, unescape
from igris.tools import Tool, ToolError, ToolRegistry

MAX_OCCURRENCES = 400      # items one call may create
MAX_ENTRIES = 100          # entries (or iCalendar events) per call
HORIZON_DAYS = 366         # nothing later than 12 months ahead
MAX_TIMED = timedelta(hours=24)
MAX_ALL_DAY = 14           # days one all-day entry may cover
MAX_INTERVAL = 4           # every 4th week at most
MAX_TEXT = 1_000_000       # characters of iCalendar text
SHOWN = 25                 # conflict lines shown
POINT = timedelta(minutes=30)
SOURCE = "manual"
DEFAULT_BRANCH = "okul"
_BATCH = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_DAY_NAMES = {name: i for i, names in enumerate((
    ("mon", "monday", "mo"), ("tue", "tuesday", "tu"), ("wed", "wednesday", "we"),
    ("thu", "thursday", "th"), ("fri", "friday", "fr"), ("sat", "saturday", "sa"),
    ("sun", "sunday", "su"))) for name in names}
ENTRY_FIELDS = ("title", "start", "end", "branch", "kind", "location", "weekly")
WEEKLY_FIELDS = ("until", "count", "interval", "days", "except")


@dataclass
class Entry:
    """One timetable slot: a first occurrence on a wall clock, maybe repeated weekly."""

    label: str              # "entry 3" or "event 3 (Matematik)", for messages
    title: str
    branch: str
    kind: str
    location: str
    start: date             # a naive datetime (wall clock in ``zone``) or a date (all-day)
    end: date               # naive datetime, or the last day of an all-day entry
    zone: object = None     # tzinfo of the wall clock; None is the machine's local zone
    weekly: dict | None = None  # {"interval", "days", "until", "count", "except"}
    uid: str = ""           # iCalendar UID, to find the master of a moved occurrence

    @property
    def all_day(self) -> bool:
        return not isinstance(self.start, datetime)


@dataclass
class Occurrence:
    entry: Entry
    begin: date             # an aware local datetime, or a date
    end: date

    @property
    def timed(self) -> bool:
        return isinstance(self.begin, datetime)


# ---- time zones ----

def _aware(naive: datetime, tz) -> datetime:
    """A wall clock time in tz; tz None is this machine's local zone with its DST rules."""
    return naive.replace(tzinfo=tz) if tz is not None else naive.astimezone()


def _local(moment: datetime, tz) -> datetime:
    return moment.astimezone(tz) if tz is not None else moment.astimezone()


def _tz_lookup(name: str):
    """A tzinfo for an IANA name, or None when this machine has no tz database entry."""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


def _wall(moment: datetime, tz) -> datetime:
    """The naive wall clock of an aware moment in tz."""
    return _local(moment, tz).replace(tzinfo=None)


# ---- entries given as JSON ----

def _when(value, field: str, zone):
    """'YYYY-MM-DD' as a date, 'YYYY-MM-DDTHH:MM' as a naive wall clock in zone."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be 'YYYY-MM-DDTHH:MM' or 'YYYY-MM-DD'")
    value = value.strip()
    try:
        if _DATE.fullmatch(value):
            return date.fromisoformat(value)
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field} must be 'YYYY-MM-DDTHH:MM' or 'YYYY-MM-DD'") from None
    moment = _wall(moment, zone) if moment.tzinfo is not None else moment
    return moment.replace(second=0, microsecond=0)


def _day(value, field: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value.strip()):
        raise ValueError(f"{field} must be a date like 2026-12-20")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(f"{field} must be a date like 2026-12-20") from None


def _int(value, field: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{field} must be a whole number from {low} to {high}")
    return value


def _weekly(raw, start: date) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("weekly must be an object such as {\"until\": \"2026-12-20\"}")
    unknown = sorted(set(raw) - set(WEEKLY_FIELDS))
    if unknown:
        raise ValueError(f"unknown weekly field {unknown[0]!r} (fields: {', '.join(WEEKLY_FIELDS)})")
    rule = {"interval": _int(raw.get("interval", 1), "weekly.interval", 1, MAX_INTERVAL),
            "until": _day(raw["until"], "weekly.until") if raw.get("until") is not None else None,
            "count": (_int(raw["count"], "weekly.count", 1, MAX_OCCURRENCES)
                      if raw.get("count") is not None else None),
            "except": set()}
    days = raw.get("days")
    if days is None:
        rule["days"] = {start.weekday()}
    else:
        if not isinstance(days, list) or not days:
            raise ValueError("weekly.days must be a list such as [\"Mon\", \"Wed\"]")
        rule["days"] = set()
        for name in days:
            number = _DAY_NAMES.get(str(name).strip().lower())
            if number is None:
                raise ValueError(f"weekly.days has an unknown day {name!r} (use Mon ... Sun)")
            rule["days"].add(number)
    excluded = raw.get("except") or []
    if not isinstance(excluded, list):
        raise ValueError("weekly.except must be a list of dates")
    rule["except"] = {_day(v, "weekly.except") for v in excluded}
    return rule


def parse_entries(raw_entries, *, branch: str, zone) -> list[Entry]:
    """Entries from the model's JSON; raises ToolError listing every invalid entry."""
    if isinstance(raw_entries, dict):
        raw_entries = [raw_entries]
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ToolError("entries must be a non-empty list")
    if len(raw_entries) > MAX_ENTRIES:
        raise ToolError(f"at most {MAX_ENTRIES} entries per call")
    entries, errors = [], []
    for number, raw in enumerate(raw_entries, start=1):
        label = f"entry {number}"
        try:
            if not isinstance(raw, dict):
                raise ValueError("must be an object")
            unknown = sorted(set(raw) - set(ENTRY_FIELDS) - {"due"})
            if unknown:
                raise ValueError(f"unknown field {unknown[0]!r} (fields: {', '.join(ENTRY_FIELDS)})")
            if "end" in raw and "due" in raw:
                raise ValueError("give end or due, not both")
            title = raw.get("title")
            if not isinstance(title, str) or not title.strip():
                raise ValueError("title must not be empty")
            chosen = raw.get("branch") or branch
            if chosen not in BRANCHES:
                raise ValueError(f"branch must be one of: {', '.join(BRANCHES)}")
            kind = raw.get("kind") or kind_from_text(title, default=LESSON)
            if kind not in KINDS:
                raise ValueError(f"kind must be one of: {', '.join(KINDS)}")
            location = raw.get("location") or ""
            if not isinstance(location, str):
                raise ValueError("location must be text")
            start = _when(raw.get("start"), "start", zone)
            end_value = raw.get("end", raw.get("due"))
            if isinstance(start, datetime):
                if end_value is None:
                    raise ValueError("end is missing ('YYYY-MM-DDTHH:MM' of the first lesson)")
                end = _when(end_value, "end", zone)
                if not isinstance(end, datetime):
                    raise ValueError("end needs a time like 2026-11-16T11:30")
            else:
                end = start if end_value is None else _when(end_value, "end", zone)
                if isinstance(end, datetime):
                    raise ValueError("an all-day entry has dates only in start and end")
            weekly = _weekly(raw["weekly"], start if not isinstance(start, datetime)
                             else start.date()) if raw.get("weekly") is not None else None
            entries.append(Entry(label, title.strip(), chosen, kind, location.strip(),
                                 start, end, zone, weekly))
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
    if errors:
        raise ToolError("nothing was imported; fix these entries:\n" + "\n".join(errors[:20]))
    return entries


# ---- iCalendar ----

_ICS_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})")
_ICS_TIME = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)")
_DURATION = re.compile(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
_ICS_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
_COMPONENTS_READ = {"VCALENDAR", "VEVENT", "VTIMEZONE", "STANDARD", "DAYLIGHT", "VALARM"}


class _Zones:
    """Resolves TZID names once and remembers the ones this machine does not know."""

    def __init__(self, owner_zone, lookup):
        self.owner, self.lookup, self.unknown, self._cache = owner_zone, lookup, set(), {}

    def get(self, name):
        if not name:
            return self.owner
        if name not in self._cache:
            found = self.lookup(name)
            if found is None:
                self.unknown.add(name)
            self._cache[name] = found if found is not None else self.owner
        return self._cache[name]


def _ics_value(value: str, params: dict, zones: _Zones):
    """(date or naive datetime, zone) of an iCalendar DATE or DATE-TIME value."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or _ICS_DATE.fullmatch(value):
        match = _ICS_DATE.fullmatch(value)
        if not match:
            raise ValueError(f"bad date {value!r}")
        return date(*map(int, match.groups())), zones.owner
    match = _ICS_TIME.fullmatch(value)
    if not match:
        raise ValueError(f"bad date-time {value!r}")
    y, mo, d, h, mi, s, utc = match.groups()
    moment = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    return moment, (timezone.utc if utc else zones.get(params.get("TZID")))


def _in_zone(value, value_zone, zone):
    """A parsed value moved onto the wall clock of zone (dates stay dates)."""
    if not isinstance(value, datetime):
        return value
    return _wall(_aware(value, value_zone), zone) if value_zone is not zone else value


def _duration(value: str) -> timedelta:
    match = _DURATION.fullmatch(value.strip())
    if not match or not any(match.groups()[1:]):
        raise ValueError(f"bad DURATION {value!r}")
    sign, weeks, days, hours, minutes, seconds = match.groups()
    if sign == "-":
        raise ValueError(f"negative DURATION {value!r}")
    try:
        span = timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                         minutes=int(minutes or 0), seconds=int(seconds or 0))
    except OverflowError:
        raise ValueError(f"DURATION {value!r} is too long") from None
    if span > timedelta(days=MAX_ALL_DAY):
        raise ValueError(f"DURATION {value!r} is longer than {MAX_ALL_DAY} days")
    return span


def _components(text: str):
    """VEVENT property lists and the names of other components that were skipped."""
    events, skipped, stack, current = [], {}, [], None
    calendar_seen = False
    for line in unfold(text):
        prop = split_property(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "BEGIN":
            component = value.strip().upper()
            if not stack:
                if component != "VCALENDAR" or calendar_seen:
                    raise ToolError("nothing was imported; invalid calendar component")
                calendar_seen = True
            elif component == "VEVENT" and stack != ["VCALENDAR"]:
                raise ToolError("nothing was imported; invalid event calendar component")
            if component == "VEVENT" and current is not None:
                raise ToolError("nothing was imported; nested VEVENT in calendar")
            stack.append(component)
            if component == "VEVENT":
                current = []
            elif component == "VALARM" or component not in _COMPONENTS_READ:
                skipped[component] = skipped.get(component, 0) + 1
        elif name == "END":
            component = value.strip().upper()
            if not stack or stack[-1] != component:
                raise ToolError("nothing was imported; mismatched calendar component")
            stack.pop()
            if component == "VEVENT" and current is not None:
                events.append(current)
                current = None
        elif current is not None and stack and stack[-1] == "VEVENT":
            current.append((name, params, value))
    if not calendar_seen or stack or current is not None:
        raise ToolError("nothing was imported; incomplete calendar component")
    return events, skipped


def _rrule(value: str, start, zone, zones: _Zones) -> dict:
    parts = {}
    for part in value.split(";"):
        key, _, val = part.partition("=")
        if key.strip():
            parts[key.strip().upper()] = val.strip()
    if parts.get("FREQ", "").upper() != "WEEKLY":
        raise LookupError(f"RRULE FREQ={parts.get('FREQ', '?')} is not supported (weekly only)")
    extra = sorted(set(parts) - {"FREQ", "INTERVAL", "UNTIL", "COUNT", "BYDAY", "WKST"})
    if extra:
        raise LookupError(f"RRULE {extra[0]} is not supported")
    first_day = start if not isinstance(start, datetime) else start.date()
    rule = {"interval": 1, "until": None, "count": None, "days": {first_day.weekday()},
            "except": set(), "wkst": 0}
    if "WKST" in parts:  # the day weeks start on; it decides which weeks INTERVAL=2 keeps
        code = parts["WKST"].upper()
        if code not in _ICS_DAYS:
            raise LookupError(f"RRULE WKST={code} is not a day")
        rule["wkst"] = _ICS_DAYS[code]
    try:
        if "INTERVAL" in parts:
            rule["interval"] = _int(int(parts["INTERVAL"]), "INTERVAL", 1, MAX_INTERVAL)
        if "COUNT" in parts:
            rule["count"] = _int(int(parts["COUNT"]), "COUNT", 1, MAX_OCCURRENCES)
    except ValueError as exc:
        raise LookupError(f"RRULE {exc}") from None
    if "UNTIL" in parts:
        until, until_zone = _ics_value(parts["UNTIL"], {}, zones)
        if isinstance(until, datetime):
            # a UTC (or floating) bound, compared as an instant
            rule["until"] = _aware(until, until_zone if until_zone is not zones.owner else zone)
        else:
            rule["until"] = until
    if "BYDAY" in parts:
        days = set()
        for code in parts["BYDAY"].split(","):
            code = code.strip().upper()
            if code not in _ICS_DAYS:
                raise LookupError(f"RRULE BYDAY={code} is not supported (plain days only)")
            days.add(_ICS_DAYS[code])
        rule["days"] = days
    return rule


def parse_ics(text: str, *, branch: str, zone, lookup: Callable = _tz_lookup):
    """Entries from iCalendar text and notes on what was skipped or read differently.

    Reads VEVENT with DTSTART, DTEND or DURATION (DATE, UTC, TZID or floating), SUMMARY,
    LOCATION, RRULE (FREQ=WEEKLY with INTERVAL, UNTIL, COUNT and plain BYDAY), EXDATE and
    RECURRENCE-ID (a moved occurrence). An event with anything else in its RRULE is skipped;
    RDATE, EXRULE, alarms and other components are ignored. Every one is named in the notes.
    """
    if not isinstance(text, str) or "BEGIN:VCALENDAR" not in text.upper():
        raise ToolError("ics must be iCalendar text (BEGIN:VCALENDAR ... END:VCALENDAR)")
    if len(text) > MAX_TEXT:
        raise ToolError(f"the calendar text is longer than {MAX_TEXT} characters")
    zones = _Zones(zone, lookup)
    raw_events, skipped = _components(text)
    entries, notes, overrides, cancelled, unread = [], [], [], 0, 0
    cancellations = []
    for number, props in enumerate(raw_events, start=1):
        first = {}
        for name, params, value in props:
            first.setdefault(name, (params, value))
        title = unescape(first.get("SUMMARY", ({}, ""))[1]).strip() or "(başlıksız)"
        label = f"event {number} ({title[:40]})"
        if first.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED":
            cancelled += 1
            if "RECURRENCE-ID" in first:
                try:
                    params, value = first["RECURRENCE-ID"]
                    moment, moment_zone = _ics_value(value, params, zones)
                    uid = first.get("UID", ({}, ""))[1].strip()
                    if not uid:
                        raise ValueError("cancelled occurrence has no UID")
                    cancellations.append((uid, moment, moment_zone))
                except ValueError as exc:
                    notes.append(f"{label}: skipped, {exc}")
                    unread += 1
            continue
        try:  # one bad event is reported and skipped, it never stops the parse
            entry, moved = _ics_entry(label, title, first, props, branch, zones, notes)
        except (LookupError, ValueError) as exc:
            notes.append(f"{label}: skipped, {exc}")
            unread += 1
            continue
        except Exception as exc:
            notes.append(f"{label}: skipped, unreadable ({type(exc).__name__})")
            unread += 1
            continue
        if moved is not None:
            overrides.append((entry.uid, *moved, entry))
        entries.append(entry)
    for uid, moment, moment_zone in cancellations:
        for master in (e for e in entries if e.uid == uid and e.weekly is not None):
            original = _in_zone(moment, moment_zone, master.zone)
            master.weekly["except"].add(original.date() if isinstance(original, datetime)
                                        else original)
    for uid, moved, moved_zone, entry in overrides:
        # a moved occurrence replaces the master's occurrence on the original day
        for master in (e for e in entries if uid and e.uid == uid and e is not entry
                       and e.weekly is not None):
            original = _in_zone(moved, moved_zone, master.zone)
            master.weekly["except"].add(original.date() if isinstance(original, datetime)
                                        else original)
    if cancelled:
        notes.append(f"{cancelled} cancelled event(s) skipped")
    for component, count in sorted(skipped.items()):
        what = "alarm(s) ignored (Igris reminders are used)" if component == "VALARM" \
            else f"{component} component(s) ignored"
        notes.append(f"{count} {what}")
    for name in sorted(zones.unknown):
        notes.append(f"time zone {name} is not known on this machine; its times were read "
                     "as the owner's local time")
    if len(entries) > MAX_ENTRIES:
        raise ToolError(f"the calendar has {len(entries)} events; at most {MAX_ENTRIES} per call")
    return entries, notes, unread


def _ics_entry(label, title, first, props, branch, zones, notes):
    """(Entry, (moved value, its zone) or None) of one VEVENT; raises ValueError or
    LookupError naming what cannot be read."""
    if "DTSTART" not in first:
        raise ValueError("no DTSTART")
    start, start_zone = _ics_value(first["DTSTART"][1], first["DTSTART"][0], zones)
    timed = isinstance(start, datetime)
    if "DTEND" in first:
        end, end_zone = _ics_value(first["DTEND"][1], first["DTEND"][0], zones)
        if timed != isinstance(end, datetime):
            raise ValueError("DTSTART and DTEND must both be dates or both have a time")
        end = _in_zone(end, end_zone, start_zone)
        if not timed:
            if end < start:
                raise ValueError("DTEND is before DTSTART")
            end = max(end - timedelta(days=1), start)  # DTEND of a date is exclusive
    elif "DURATION" in first:
        span = _duration(first["DURATION"][1])
        end = start + span if timed else start + timedelta(days=max(span.days, 1) - 1)
    elif timed:
        raise ValueError("no DTEND or DURATION")
    else:
        end = start
    weekly = None
    if "RRULE" in first:
        weekly = _rrule(first["RRULE"][1], start, start_zone, zones)
        for name, params, value in props:
            if name != "EXDATE":
                continue
            for part in value.split(","):
                moment, moment_zone = _ics_value(part, params, zones)
                moment = _in_zone(moment, moment_zone, start_zone)
                weekly["except"].add(moment.date() if isinstance(moment, datetime) else moment)
    for name in ("RDATE", "EXRULE"):
        if name in first:
            notes.append(f"{label}: {name} ignored")
    moved = None
    if "RECURRENCE-ID" in first:
        moved = _ics_value(first["RECURRENCE-ID"][1], first["RECURRENCE-ID"][0], zones)
    location = unescape(first.get("LOCATION", ({}, ""))[1]).strip()
    uid = first.get("UID", ({}, ""))[1].strip()
    return Entry(label, title, branch, kind_from_text(title, default=LESSON), location, start,
                 end, start_zone, weekly, uid), moved


# ---- expansion ----

def expand(entry: Entry, owner_zone, cut: date | None = None) -> list[Occurrence]:
    """Every occurrence of an entry, on the owner's clock; a repeat keeps its wall clock time
    in the entry's zone, so a 10:00 lesson stays at 10:00 after a DST change."""
    duration = entry.end - entry.start
    first_day = entry.start if entry.all_day else entry.start.date()
    starts = []
    rule = entry.weekly
    if rule is None:
        starts.append(entry.start)
    else:
        until, count = rule["until"], rule["count"]
        if cut is not None:  # the import's own until cuts every repeat
            if until is None or _until_day(until, entry.zone) > cut:
                until = cut
        wkst = rule.get("wkst", 0)
        week = first_day - timedelta(days=(first_day.weekday() - wkst) % 7)
        limit = first_day + timedelta(days=2 * HORIZON_DAYS)
        seen = 0
        while week <= limit:
            for offset in sorted((weekday - wkst) % 7 for weekday in rule["days"]):
                day = week + timedelta(days=offset)
                if day < first_day:
                    continue
                begin = day if entry.all_day else datetime.combine(day, entry.start.time())
                if isinstance(until, datetime):
                    moment = _aware(datetime.combine(day, datetime.min.time()), entry.zone) \
                        if entry.all_day else _aware(begin, entry.zone)
                    if moment > until:
                        return _place(entry, starts, duration, owner_zone)
                elif until is not None and day > until:
                    return _place(entry, starts, duration, owner_zone)
                if count is not None and seen >= count:
                    return _place(entry, starts, duration, owner_zone)
                seen += 1
                if day not in rule["except"]:
                    starts.append(begin)
            week += timedelta(weeks=rule["interval"])
    return _place(entry, starts, duration, owner_zone)


def _place(entry: Entry, starts: list, duration, owner_zone) -> list[Occurrence]:
    if entry.all_day:
        return [Occurrence(entry, day, day + duration) for day in starts]
    return [Occurrence(entry, _local(_aware(b, entry.zone), owner_zone),
                       _local(_aware(b + duration, entry.zone), owner_zone)) for b in starts]


def _until_day(until, zone) -> date:
    """The last day a repeat may fall on: an UNTIL instant read on the entry's clock."""
    return _wall(until, zone).date() if isinstance(until, datetime) else until


def _check(entry: Entry, today: date, horizon: date, cut: date | None) -> str | None:
    """Why an entry cannot be imported, or None."""
    if entry.all_day:
        if entry.end < entry.start:
            return "end must not be before start"
        if (entry.end - entry.start).days >= MAX_ALL_DAY:
            return f"an all-day entry covers at most {MAX_ALL_DAY} days"
    else:
        if entry.end <= entry.start:
            return ("end must be after start; a lesson past midnight ends on the next "
                    "day's date")
        if entry.end - entry.start > MAX_TIMED:
            return "a timed entry lasts at most 24 hours (use dates for an all-day entry)"
    first_day = entry.start if entry.all_day else entry.start.date()
    if first_day > horizon:
        return f"starts after {horizon} (at most 12 months ahead)"
    rule = entry.weekly
    if rule is not None:
        until = rule["until"]
        until_day = _until_day(until, entry.zone) if until is not None else None
        if until_day is None and rule["count"] is None and cut is None:
            return "a weekly repeat needs until or count (or pass until for the whole import)"
        if isinstance(until, datetime):  # a UTC UNTIL is an instant, not a calendar date
            first = entry.start if not entry.all_day else datetime.combine(
                entry.start, datetime.min.time())
            if until < _aware(first, entry.zone):
                return "the repeat ends (UNTIL) before the first lesson"
        elif until_day is not None and until_day < first_day:
            return "weekly.until is before the first lesson"
        if until_day is not None and until_day > horizon and cut is None:
            return f"weekly.until is after {horizon} (at most 12 months ahead)"
    return None


# ---- the chat tools ----

def _fmt(occ: Occurrence) -> str:
    begin, end = occ.begin, occ.end
    if not occ.timed:
        return f"{begin} {_DAYS[begin.weekday()]} (all day)" if end == begin else \
            f"{begin} - {end} (all day)"
    head = f"{begin:%Y-%m-%d} {_DAYS[begin.weekday()]} {begin:%H:%M}"
    if end.date() != begin.date():
        return f"{head} - {end:%Y-%m-%d} {_DAYS[end.weekday()]} {end:%H:%M}"
    return f"{head}-{end:%H:%M}"


def _instant(value, tz) -> datetime | None:
    if not isinstance(value, str) or not value or _DATE.fullmatch(value):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _local(moment, tz) if moment.tzinfo is not None else _aware(moment, tz)


def _span(first, second, tz):
    """(begin, end) of a timed item or event on the owner's clock; None when all-day."""
    begin = _instant(first, tz)
    if begin is None:
        return None
    end = _instant(second, tz)
    if end is None or end <= begin:
        end = begin + POINT
    return begin, end


def _fmt_span(begin: datetime, end: datetime) -> str:
    head = f"{begin:%Y-%m-%d} {_DAYS[begin.weekday()]} {begin:%H:%M}"
    if end.date() != begin.date():
        return f"{head} - {end:%Y-%m-%d} {_DAYS[end.weekday()]} {end:%H:%M}"
    return f"{head}-{end:%H:%M}"


def _ref(batch: str, occ: Occurrence) -> str:
    clock = f"{occ.begin:%H:%M}" if occ.timed else "all-day"
    digest = hashlib.sha1(f"{occ.entry.title.casefold()}|{occ.entry.branch}|{clock}"
                          .encode("utf-8")).hexdigest()[:10]
    day = occ.begin.date() if occ.timed else occ.begin
    return f"{IMPORT_PREFIX}{batch}:{day.isoformat()}:{digest}"


def _item(occ: Occurrence, ref: str) -> dict:
    if occ.timed:
        start, due = occ.begin.isoformat(timespec="minutes"), occ.end.isoformat(timespec="minutes")
    else:
        start, due = occ.begin.isoformat(), occ.end.isoformat()
    entry = occ.entry
    return {"branch": entry.branch, "kind": entry.kind, "title": entry.title, "start": start,
            "due": due, "location": entry.location, "source": SOURCE, "source_ref": ref,
            "status": "added", "mandatory": False}


def _batch_name(value) -> str:
    name = value.strip().lower() if isinstance(value, str) else ""
    if not _BATCH.fullmatch(name):
        raise ToolError("batch must be a short name of letters, digits and dashes, "
                        "such as 'school-2026-fall'")
    return name


def _past(item: dict, now: datetime, tz) -> bool:
    """True when a stored item is over (its end, else its start, has passed)."""
    last = item.get("due") or item.get("start") or ""
    if _DATE.fullmatch(last):
        return date.fromisoformat(last) < now.date()
    moment = _instant(last, tz)
    return moment is not None and moment <= now


class ImportTools:
    """agenda_import and agenda_import_remove for one owner's agenda."""

    def __init__(self, store, owner: str, *, busy: Callable | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                 zone=None, uploads: Callable | None = None, lookup: Callable = _tz_lookup):
        self.store, self.owner, self.busy, self.now = store, owner, busy, now
        self.zone, self.uploads, self.lookup = zone, uploads, lookup

    # ---- input ----

    def _source_text(self, ics, file_id, content):
        if file_id is not None:
            if self.uploads is None:
                raise ToolError("uploaded files are not available here; pass ics text instead")
            if not isinstance(file_id, str) or not file_id:
                raise ToolError("file_id must be an id from list_uploads")
            try:
                name, data = self.uploads(file_id)
            except Exception:
                raise ToolError("file not found; use an id from list_uploads") from None
            if len(data) > MAX_TEXT:
                raise ToolError(f"{name} is larger than {MAX_TEXT} bytes")
            return data.decode("utf-8-sig", errors="replace")
        return ics if ics is not None else content

    def _entries(self, entries, ics, file_id, content, branch):
        given = [n for n, v in (("entries", entries), ("ics", ics), ("file_id", file_id),
                                ("content", content)) if v is not None]
        if len(given) != 1:
            raise ToolError("give exactly one of entries, ics, file_id or a CONTENT block")
        if content is not None and "BEGIN:VCALENDAR" not in content.upper():
            try:
                entries = json.loads(content)
            except ValueError:
                raise ToolError("the CONTENT block must be iCalendar text or a JSON list "
                                "of entries") from None
            if isinstance(entries, dict) and "entries" in entries:
                entries = entries["entries"]
            content = None
        if isinstance(entries, str):
            try:
                entries = json.loads(entries)
            except ValueError:
                raise ToolError("entries must be a JSON list of objects") from None
        if entries is not None:
            return parse_entries(entries, branch=branch, zone=self.zone), [], 0, False
        text = self._source_text(ics, file_id, content)
        found, notes, unread = parse_ics(text, branch=branch, zone=self.zone, lookup=self.lookup)
        return found, notes, unread, True

    def _plan(self, batch, entries, ics, file_id, content, branch, until):
        """Everything an import would write, or a ToolError; writes nothing."""
        batch = _batch_name(batch)
        if branch not in BRANCHES:
            raise ToolError(f"branch must be one of: {', '.join(BRANCHES)}")
        now = _local(self.now(), self.zone)
        today = now.date()
        horizon = today + timedelta(days=HORIZON_DAYS)
        try:
            cut = _day(until, "until") if until is not None else None
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        if cut is not None and cut > horizon:
            raise ToolError(f"until is after {horizon} (at most 12 months ahead)")
        found, notes, unread, from_ics = self._entries(entries, ics, file_id, content, branch)
        kept, errors, expanded = [], [], {}
        for entry in found:
            try:
                problem = _check(entry, today, horizon, cut)
                if problem is None:
                    expanded[id(entry)] = expand(entry, self.zone, cut)
            except Exception as exc:  # an odd calendar event must not break the turn
                problem = f"unreadable ({type(exc).__name__})"
            if problem is None:
                kept.append(entry)
            elif from_ics:
                notes.append(f"{entry.label}: skipped, {problem}")
                unread += 1
            else:
                errors.append(f"{entry.label}: {problem}")
        if errors:
            raise ToolError("nothing was imported; fix these entries:\n" + "\n".join(errors[:20]))
        occurrences, past, by_ref = [], 0, {}
        for entry in kept:
            for occ in expanded[id(entry)]:
                if (occ.end <= now) if occ.timed else (occ.end < today):
                    past += 1
                    continue
                day = occ.begin.date() if occ.timed else occ.begin
                if day > horizon:
                    if from_ics:
                        notes.append(f"{entry.label}: occurrences after {horizon} skipped")
                        unread += 1
                        break
                    raise ToolError(f"nothing was imported; {entry.label} goes past {horizon} "
                                    "(at most 12 months ahead)")
                ref = _ref(batch, occ)
                if ref in by_ref:
                    other = by_ref[ref].entry
                    raise ToolError(f"nothing was imported; {other.label} and {entry.label} "
                                    f"both give {entry.title} on {_fmt(occ)}")
                by_ref[ref] = occ
                occurrences.append(occ)
        if len(occurrences) > MAX_OCCURRENCES:
            raise ToolError(f"nothing was imported; this would create {len(occurrences)} items, "
                            f"at most {MAX_OCCURRENCES} per call (split it or pass an earlier until)")
        occurrences.sort(key=lambda o: (o.begin if o.timed else _aware(
            datetime.combine(o.begin, datetime.min.time()), self.zone)))
        return batch, occurrences, by_ref, past, notes, now, unread

    # ---- report ----

    def _conflicts(self, batch, occurrences):
        prefix = f"{IMPORT_PREFIX}{batch}:"
        tz = self.zone
        busy = []
        for item in self.store.list(self.owner):
            if item["source_ref"].startswith(prefix):
                continue
            start, due = item.get("start"), item.get("due")
            span = _span(start or due or "", due if start else "", tz)
            if span is not None:
                busy.append((span, f"{item['title']} ({_fmt_span(*span)})"))
        note = None
        timed = [o for o in occurrences if o.timed]
        if self.busy is not None and timed:
            begin, end = timed[0].begin, max(o.end for o in timed)
            window = begin
            while window < end and note is None:
                stop = min(window + timedelta(days=60), end)
                try:
                    result = self.busy(window.timestamp(), stop.timestamp()) or {}
                except Exception as exc:  # a reader failure must not break the chat turn
                    result = {"status": exc.__class__.__name__}
                if result.get("status") != "ok":
                    note = f"Google Calendar could not be read (status: {result.get('status')})"
                    break
                for event in result.get("events") or []:
                    span = _span(event.get("start", ""), event.get("end", ""), tz)
                    if span is not None:
                        busy.append((span, f"{event.get('title', '')} ({_fmt_span(*span)}, "
                                           "Google Calendar)"))
                window = stop
        lines = []
        for occ in timed:
            for (b, e), label in busy:
                if occ.begin < e and b < occ.end:
                    lines.append(f"{_fmt(occ)} {occ.entry.title} overlaps {label}")
        inner = []
        for i, occ in enumerate(timed):
            for other in timed[i + 1:]:
                if other.begin >= occ.end:
                    break
                if other.entry is not occ.entry:
                    inner.append(f"{_fmt(occ)} {occ.entry.title} overlaps {other.entry.title} "
                                 f"({_fmt(other)})")
        return lines, inner, note

    def _summary(self, batch, occurrences, past, notes, counts, unread, preview):
        head = (f"preview batch={batch} (nothing was written to the calendar)" if preview
                else f"imported batch={batch}: created {counts['created']}, updated "
                     f"{counts['updated']}, unchanged {counts['unchanged']}, removed "
                     f"{counts['removed']}")
        lines = [head]
        if not occurrences:
            lines.append("occurrences: 0 (nothing upcoming in this timetable)")
        else:
            first, last = occurrences[0], occurrences[-1]
            lines.append(f"occurrences: {len(occurrences)} from {_fmt(first)} to {_fmt(last)}")
            per_day = {}
            for occ in occurrences:
                weekday = occ.begin.weekday()
                clock = f"{occ.begin:%H:%M}-{occ.end:%H:%M}" if occ.timed else "all day"
                key = (clock, occ.entry.title, occ.entry.branch)
                per_day.setdefault(weekday, {}).setdefault(key, 0)
                per_day[weekday][key] += 1
            lines.append("per weekday:")
            for weekday in sorted(per_day):
                slots = per_day[weekday]
                total = sum(slots.values())
                text = ", ".join(f"{clock} {title} [{branch}] x{n}"
                                 for (clock, title, branch), n in sorted(slots.items()))
                lines.append(f"  {_DAYS[weekday]} {total}: {text}")
        if past:
            lines.append(f"{past} occurrence(s) already over were skipped")
        if preview:
            lines.append(f"against this batch now: {counts['created']} new, {counts['updated']} "
                         f"changed, {counts['unchanged']} unchanged, {counts['removed']} "
                         "upcoming removed")
        if counts.get("kept_done"):
            lines.append(f"{counts['kept_done']} item(s) marked done are not in the timetable "
                         "any more and were kept (done items are never removed)")
        if unread:
            lines.append(f"{unread} event(s) could not be read, so nothing missing from this "
                         f"import is removed: {counts.get('kept', 0)} stored item(s) kept")
        conflicts, inner, note = self._conflicts(batch, occurrences)
        if conflicts:
            lines.append(f"conflicts with existing items ({len(conflicts)}; nothing is moved or "
                         "skipped):")
            lines += ["  " + c for c in conflicts[:SHOWN]]
            if len(conflicts) > SHOWN:
                lines.append(f"  ... and {len(conflicts) - SHOWN} more")
        else:
            lines.append("conflicts with existing items: none")
        if inner:
            lines.append(f"overlaps inside this import ({len(inner)}; another subgroup or a "
                         "duplicate?):")
            lines += ["  " + c for c in inner[:SHOWN]]
            if len(inner) > SHOWN:
                lines.append(f"  ... and {len(inner) - SHOWN} more")
        if note:
            lines.append(note + "; only Görev Takvimi items were checked.")
        if notes:
            lines.append("notes: " + "; ".join(notes[:20])
                         + (f"; ... and {len(notes) - 20} more" if len(notes) > 20 else ""))
        if preview:
            lines.append("Show this to the user; after they confirm, call agenda_import again "
                         "with the same arguments and \"preview\": false.")
        else:
            lines.append("The items are copied to Igris's Google calendars in the background "
                         "with the default reminders of their kind.")
        return "\n".join(lines)

    def _counts(self, batch, by_ref, now, remove_missing):
        """What replace_refs would do with this plan, and the items it gets."""
        prefix = f"{IMPORT_PREFIX}{batch}:"
        existing = {it["source_ref"]: it for it in self.store.list(self.owner, include_done=True)
                    if it["source_ref"].startswith(prefix)}
        items = [_item(occ, ref) for ref, occ in by_ref.items()]
        counts = {"created": 0, "updated": 0, "unchanged": 0, "removed": 0, "kept_done": 0,
                  "kept": 0}
        compared = ("title", "start", "due", "location", "kind")
        for item in items:
            old = existing.get(item["source_ref"])
            if old is None:
                counts["created"] += 1
            elif any((old.get(k) or "") != (item.get(k) or "") for k in compared):
                counts["updated"] += 1
            else:
                counts["unchanged"] += 1
        kept = 0
        for ref, old in existing.items():
            if ref in by_ref:
                continue
            if _past(old, now, self.zone):
                items.append(old)  # a lesson that is over stays as it is
                kept += 1
            elif old["status"] == "done":
                counts["kept_done"] += 1
            elif not remove_missing:
                counts["kept"] += 1
            else:
                counts["removed"] += 1
        return items, counts, prefix, kept

    # ---- tools ----

    def agenda_import(self, batch=None, entries=None, ics=None, file_id=None, content=None,
                      branch=DEFAULT_BRANCH, until=None, preview=True) -> str:
        if batch is None:
            raise ToolError("batch is required: a short name for this timetable, such as "
                            "'school-2026-fall' (the same name updates it later)")
        write = preview is False or (isinstance(preview, str) and preview.strip().lower()
                                     in ("false", "no", "0"))
        batch, occurrences, by_ref, past, notes, now, unread = self._plan(
            batch, entries, ics, file_id, content, branch or DEFAULT_BRANCH, until)
        # an event that could not be read may be a stored lesson: then nothing is removed
        remove_missing = not unread
        items, counts, prefix, kept = self._counts(batch, by_ref, now, remove_missing)
        if write:
            if not occurrences and not counts["removed"]:
                raise ToolError("nothing to import: the timetable has no upcoming occurrence")
            try:
                counts = self.store.replace_refs(self.owner, SOURCE, prefix, items,
                                                 remove_missing=remove_missing)
            except AgendaError as exc:
                raise ToolError(f"nothing was imported: {exc}") from exc
            counts["unchanged"] -= kept  # past items of the batch, passed through as they are
        return self._summary(batch, occurrences, past, notes, counts, unread,
                             preview=not write)

    def agenda_import_remove(self, batch=None, include_done=False) -> str:
        name = _batch_name(batch)
        include_done = include_done is True or (isinstance(include_done, str) and
                                                include_done.strip().lower() == "true")
        result = self.store.delete_refs(self.owner, SOURCE, f"{IMPORT_PREFIX}{name}:",
                                        include_done=include_done)
        removed, done = result["removed"], result["kept_done"]
        if not removed and not done:
            known = sorted({it["source_ref"].split(":")[1]
                            for it in self.store.list(self.owner, include_done=True)
                            if it["source_ref"].startswith(IMPORT_PREFIX)})
            raise ToolError(f"no items in batch {name}; imported batches: "
                            + (", ".join(known) or "none"))
        text = (f"removed batch={name}: {removed} item(s) deleted; their Google Calendar events "
                "are removed in the background")
        if done:
            text += (f"\n{done} item(s) marked done were kept; pass \"include_done\": true to "
                     "remove them too")
        return text


def register_import_tools(registry: ToolRegistry, store, owner: str, *,
                          busy: Callable | None = None,
                          now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
                          zone=None, uploads: Callable | None = None) -> ImportTools:
    """Register agenda_import and agenda_import_remove. ``uploads`` is
    ``file_id -> (name, bytes)`` of the user's own uploads, for an .ics file."""
    tools = ImportTools(store, owner, busy=busy, now=now, zone=zone, uploads=uploads)
    branches, kinds = "|".join(BRANCHES), "|".join(KINDS)
    example = ('{"batch": "school-2026-fall", "entries": [{"title": "Matematik", '
               '"start": "2026-11-16T10:00", "end": "2026-11-16T11:30", "location": "201", '
               '"weekly": {"until": "2026-12-20", "except": ["2026-11-30"]}}]}')
    registry.add(Tool(
        name="agenda_import",
        description=(
            "Put a class timetable or many items into Görev Takvimi in one call; weekly lessons "
            "become one item per occurrence. Args: batch (short name; importing the same batch "
            "again updates it instead of duplicating), entries = list of {title, start, end, "
            f"branch ({branches}, default okul), kind ({kinds}), location, weekly}}: one entry "
            "per weekly slot, start and end 'YYYY-MM-DDTHH:MM' local time of its FIRST lesson "
            "(past midnight the end has the next day's date); an all-day entry uses dates (end "
            "= its last day); "
            "weekly = {until 'YYYY-MM-DD' or count, interval (1 every week, 2 every other week), "
            "days ['Mon','Thu'] (default the start's day), except ['YYYY-MM-DD']}. Instead of "
            "entries: ics (iCalendar text), file_id (an uploaded .ics from list_uploads) or the "
            "text between <<<CONTENT and >>>CONTENT lines. Optional until cuts every repeat. "
            "preview is true by default: nothing is written, the result shows count, dates, "
            "weekdays and conflicts; write with \"preview\": false only after the user "
            f"confirmed. Example: {example}"),
        func=tools.agenda_import,
    ))
    registry.add(Tool(
        name="agenda_import_remove",
        description=("Remove the items of one imported batch from Görev Takvimi (and their "
                     "calendar events); items marked done stay unless include_done is true. "
                     "Args: batch, include_done=false."),
        func=tools.agenda_import_remove,
    ))
    return tools
