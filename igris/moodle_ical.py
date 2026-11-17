"""Moodle course deadlines from the calendar's iCal export link, as task calendar items.

The owner copies the export link once (Moodle: Calendar > Export calendar > all courses,
recent and next 60 days > Get calendar URL) and stores it with
``python tools/set_keys.py moodle`` as MOODLE_ICAL_URL. The link carries a private token,
so it is only ever passed to the opener: errors name HTTP codes and exception classes,
never the URL.

The parser covers what Moodle (and most iCal producers) write: folded lines, parameters,
escaped text, UTC, TZID and floating date-times and all-day dates. Recurrence rules are
not expanded; Moodle exports every event on its own.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import re
import urllib.error
import urllib.request

from igris.agenda import kind_from_text

MAX_BYTES = 5 * 1024 * 1024
_DATE = re.compile(r"(\d{4})(\d{2})(\d{2})")
_DATETIME = re.compile(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(Z?)")
# Moodle names the event type in the summary; an opening is not a deadline
_OPENS = re.compile(r"\b(opens|açılıyor|açılır|prasideda|atidaroma|atidaromas)\b", re.IGNORECASE)
# "Homework 3 is due" / "... turi būti pateikta": the due date already says it
_DUE_SUFFIX = re.compile(r"\s+(is due|turi būti pateikta|teslim edilmeli)\s*$", re.IGNORECASE)
# a course id such as "KnF52029_20353_1_20261" (code, group, part, term): only the code reads
_COURSE_ID = re.compile(r"^([A-Za-z]+\d+)(?:_\d+)+$")


class MoodleError(RuntimeError):
    """The export link could not be read; the message never contains the link."""


def fetch(url: str, *, opener=urllib.request.urlopen, timeout: float = 30) -> str:
    """The iCal text behind the export link (HTTPS only)."""
    if not isinstance(url, str) or not url.lower().startswith("https://"):
        raise MoodleError("The Moodle calendar link must be an https:// address")
    request = urllib.request.Request(url, headers={"User-Agent": "igris-agenda/1"})
    try:
        response = opener(request, timeout=timeout)
        try:
            raw = response.read(MAX_BYTES + 1)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except urllib.error.HTTPError as exc:
        raise MoodleError(f"Moodle returned HTTP {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise MoodleError(f"Moodle unreachable: {type(exc).__name__}") from None
    if len(raw) > MAX_BYTES:
        raise MoodleError("The Moodle calendar is larger than 5 MB")
    text = raw.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text:
        raise MoodleError("The Moodle link did not return a calendar (is it the export link?)")
    return text


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif line:
            lines.append(line)
    return lines


def split_property(line: str):
    """'DTSTART;TZID="Europe/Vilnius":2026…' -> ('DTSTART', {'TZID': 'Europe/Vilnius'}, '2026…')."""
    quoted, cut = False, -1
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            cut = i
            break
    if cut < 0:
        return None
    head, value = line[:cut], line[cut + 1:]
    parts = re.findall(r'(?:[^;"]|"[^"]*")+', head)
    if not parts:
        return None
    params = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.strip().upper()] = val.strip().strip('"')
    return parts[0].strip().upper(), params, value


def unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _zone(name: str | None, default_tz):
    if not name:
        return default_tz
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # unknown id, or no tz database on this machine
        return default_tz


def parse_when(value: str, params: dict, default_tz) -> str | None:
    """An iCal date or date-time as the agenda's ISO text: a date, or a time with offset."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or _DATE.fullmatch(value):
        m = _DATE.fullmatch(value)
        return date(*map(int, m.groups())).isoformat() if m else None
    m = _DATETIME.fullmatch(value)
    if not m:
        return None
    y, mo, d, h, mi, s, utc = m.groups()
    tz = timezone.utc if utc else _zone(params.get("TZID"), default_tz)
    try:
        moment = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), tzinfo=tz)
    except ValueError:
        return None
    return moment.isoformat()


def parse_events(text: str, *, default_tz=None) -> list[dict]:
    """Every VEVENT as {uid, summary, description, categories, location, start, end, due}."""
    if default_tz is None:
        default_tz = datetime.now().astimezone().tzinfo
    events, current = [], None
    for line in unfold(text):
        prop = split_property(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "BEGIN" and value.strip().upper() == "VEVENT":
            current = {}
        elif name == "END" and value.strip().upper() == "VEVENT":
            if current is not None and current.get("uid"):
                events.append(current)
            current = None
        elif current is None:
            continue
        elif name in ("UID", "SUMMARY", "DESCRIPTION", "LOCATION"):
            current[name.lower()] = unescape(value).strip()
        elif name == "CATEGORIES":
            current.setdefault("categories", []).extend(
                unescape(v).strip() for v in re.split(r"(?<!\\),", value) if v.strip())
        elif name in ("DTSTART", "DTEND", "DUE"):
            current[{"DTSTART": "start", "DTEND": "end", "DUE": "due"}[name]] = \
                parse_when(value, params, default_tz)
    return events


def to_item(event: dict) -> dict | None:
    """One Moodle event as an agenda item for the "okul" branch, or None without a date."""
    summary = event.get("summary") or "(başlıksız)"
    courses = event.get("categories") or []
    course = courses[0] if courses else ""
    start, end, due = event.get("start"), event.get("end"), event.get("due")
    if not (start or due):
        return None
    opening = bool(_OPENS.search(summary))
    kind = kind_from_text(summary, *courses, default="assignment" if not opening else "other")
    if due is None and not opening and (end in (None, start)) and kind != "exam":
        # Moodle writes a deadline as an event that starts and ends at the due moment
        start, due = None, start
    summary = _DUE_SUFFIX.sub("", summary) or summary
    match = _COURSE_ID.match(course)
    label = match.group(1) if match else course
    title = summary if not label or course.lower() in summary.lower() else f"{label}: {summary}"
    return {"branch": "okul", "kind": kind, "title": title, "start": start, "due": due,
            "location": event.get("location", ""), "source": "moodle",
            "source_ref": event["uid"], "mandatory": not opening, "confidence": 1.0,
            "status": "added"}


def items_from_ical(text: str, *, default_tz=None) -> list[dict]:
    return [item for item in (to_item(e) for e in parse_events(text, default_tz=default_tz))
            if item is not None]
