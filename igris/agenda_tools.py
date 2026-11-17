"""Chat tools for the task calendar (Görev Takvimi): find free time, add, list, change and
remove items.

The tools are registered per signed-in user, so each user reaches only their own agenda.
Items added from chat are manual entries with status "added"; for the owner the Google
Calendar sync (``calendar_sync``) then puts them into Igris's own calendars with reminders.
``agenda_update`` changes a manual item in place through the store, so that same sync moves
its event; there is no second path to Google.
``busy`` (owner only) is ``(time_min, time_max) -> {"status", "events"}`` with the owner's
Google events in that window, so ``free_slots`` can avoid them. Times without an offset are
read in the local time zone of ``now()``.
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
import re
from typing import Callable

from igris.agenda import BRANCHES, KINDS, AgendaError, kind_from_text
from igris.tools import Tool, ToolError, ToolRegistry

MAX_DAYS = 31           # free_slots / agenda_list window
MAX_ITEMS = 20          # agenda_add items per call
POINT = timedelta(minutes=30)  # a deadline with one moment blocks this long
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HM = re.compile(r"(\d{1,2}):(\d{2})")
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _day(value, field: str) -> date:
    if not isinstance(value, str) or not _DATE.fullmatch(value.strip()):
        raise ToolError(f"{field} must be a date like 2026-11-19")
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ToolError(f"{field} must be a date like 2026-11-19") from None


def _clock(value, field: str) -> dtime:
    match = _HM.fullmatch(str(value).strip()) if value is not None else None
    if match is None or int(match.group(1)) > 24 or int(match.group(2)) > 59:
        raise ToolError(f"{field} must be a time like 09:00")
    hour, minute = int(match.group(1)), int(match.group(2))
    return dtime.max if (hour, minute) >= (24, 0) else dtime(hour, minute)


def _window(start_date, end_date, now: datetime) -> tuple[date, date]:
    first = _day(start_date, "start_date") if start_date else now.date()
    last = _day(end_date, "end_date") if end_date else first + timedelta(days=13)
    if last < first:
        raise ToolError("end_date must not be before start_date")
    if (last - first).days >= MAX_DAYS:
        raise ToolError(f"at most {MAX_DAYS} days at once")
    return first, last


def _moment(value: str, tz) -> datetime | None:
    """A timed ISO value as an aware datetime in tz; None for a bare date or junk."""
    if not isinstance(value, str) or not value or _DATE.fullmatch(value):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=tz)).astimezone(tz)


def _span(first: str, second: str, tz) -> tuple[datetime, datetime] | None:
    """The busy interval of an item or event; None when it is all-day."""
    begin = _moment(first, tz)
    if begin is None:
        return None
    end = _moment(second, tz)
    if end is None or end <= begin:
        end = begin + POINT
    return begin, end


def _localize(value, tz):
    """Agenda dates need an offset; a date-time without one is taken as local time."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if _DATE.fullmatch(value):
        return value
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return value  # the store rejects it with a clear message
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.isoformat(timespec="minutes")


def _fmt_when(item: dict, tz) -> str:
    """When an item happens; the end date is shown whenever it is not the start's day."""
    start, due = item.get("start"), item.get("due")
    span = _span(start or due or "", due if start else "", tz)
    if span is None:
        first = (start or due or "")[:10]
        last = (due or "")[:10] if start else ""
        if last and last != first:
            return f"{first} - {last} (all day)"
        return f"{first} (all day)"
    begin, end = span
    head = f"{begin:%Y-%m-%d} {_DAYS[begin.weekday()]} {begin:%H:%M}"
    if end.date() != begin.date():
        return f"{head} - {end:%Y-%m-%d} {_DAYS[end.weekday()]} {end:%H:%M}"
    return f"{head}-{end:%H:%M}"


def _order_error(start, due, tz) -> str | None:
    """A message when a timed item would end before (or when) it starts."""
    begin, end = _moment(start or "", tz), _moment(due or "", tz)
    if begin is not None and end is not None and end <= begin:
        return ("due must be after start; an item that ends on the next day "
                "(for example 22:00 to 08:00) needs the next day's date in due")
    return None


def _merge(spans: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for begin, end in sorted(spans):
        if merged and begin <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([begin, end])
    return [(b, e) for b, e in merged]


class AgendaTools:
    """The tool functions for one owner's agenda."""

    def __init__(self, store, owner: str, *, busy: Callable | None = None,
                 now: Callable[[], datetime] = _local_now):
        self.store, self.owner, self.busy, self.now = store, owner, busy, now

    def _tz(self):
        return self.now().tzinfo

    def _range(self, first: date, last: date):
        tz = self._tz()
        return (datetime.combine(first, dtime(), tz),
                datetime.combine(last + timedelta(days=1), dtime(), tz))

    def _google(self, begin: datetime, end: datetime):
        """(events, note) from the owner's Google Calendar; a note says when it was not read."""
        if self.busy is None:
            return [], None
        try:
            result = self.busy(begin.timestamp(), end.timestamp()) or {}
        except Exception as exc:  # a reader failure must not break the chat turn
            return [], f"Google Calendar could not be read ({exc.__class__.__name__})"
        status = result.get("status")
        if status != "ok":
            return [], f"Google Calendar could not be read (status: {status})"
        return list(result.get("events") or []), None

    def free_slots(self, start_date=None, end_date=None, minutes=60,
                   day_start="09:00", day_end="22:00") -> str:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise ToolError("minutes must be a whole number") from None
        if not 15 <= minutes <= 720:
            raise ToolError("minutes must be from 15 to 720")
        open_at, close_at = _clock(day_start, "day_start"), _clock(day_end, "day_end")
        if close_at <= open_at:
            raise ToolError("day_end must be after day_start")
        now = self.now()
        tz = now.tzinfo
        first, last = _window(start_date, end_date, now)
        begin, end = self._range(first, last)
        events, note = self._google(begin, end)
        spans, all_day = [], []
        for ev in events:
            span = _span(ev.get("start", ""), ev.get("end", ""), tz)
            if span is None:
                all_day.append(f"{str(ev.get('start', ''))[:10]} {ev.get('title', '')}")
            else:
                spans.append(span)
        for item in self.store.between(self.owner, begin, end):
            if item.get("status") == "done":
                continue
            start, due = item.get("start"), item.get("due")
            span = _span(start or due or "", due if start else "", tz)
            if span is not None:
                spans.append(span)
        busy = _merge(spans)
        lines = []
        day = first
        while day <= last:
            lo = datetime.combine(day, open_at, tz)
            hi = datetime.combine(day, close_at, tz)
            lo = max(lo, now) if day == now.date() else lo
            gaps, cursor = [], lo
            for b, e in busy:
                if e <= cursor or b >= hi:
                    continue
                if b > cursor:
                    gaps.append((cursor, b))
                cursor = max(cursor, e)
            if cursor < hi:
                gaps.append((cursor, hi))
            fit = [f"{b:%H:%M}-{e:%H:%M}" for b, e in gaps
                   if e - b >= timedelta(minutes=minutes)]
            label = f"{day:%Y-%m-%d} {_DAYS[day.weekday()]}"
            lines.append(f"{label}: {', '.join(fit) if fit else 'no free slot'}")
            day += timedelta(days=1)
        if all_day:
            lines.append("All-day events (not counted as busy): " + "; ".join(all_day))
        if note:
            lines.append(note + "; only Görev Takvimi items were counted as busy.")
        elif self.busy is None:
            lines.append("Google Calendar is not connected for this user; "
                         "only Görev Takvimi items were counted as busy.")
        return "\n".join(lines)

    def agenda_add(self, items=None, **single) -> str:
        if items is None:
            items = [single] if single else []
        elif single:
            raise ToolError("give either items or the fields of one item, not both")
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            raise ToolError("items must be a non-empty list")
        if len(items) > MAX_ITEMS:
            raise ToolError(f"at most {MAX_ITEMS} items per call")
        tz = self._tz()
        lines = []
        for number, raw in enumerate(items, start=1):
            if not isinstance(raw, dict):
                lines.append(f"{number}. error: item must be an object")
                continue
            title = raw.get("title")
            entry = {
                "branch": raw.get("branch"),
                "kind": raw.get("kind") or kind_from_text(title or ""),
                "title": title,
                "start": _localize(raw.get("start"), tz),
                "due": _localize(raw.get("due"), tz),
                "location": raw.get("location"),
                "source": "manual", "status": "added", "mandatory": bool(raw.get("mandatory")),
            }
            problem = _order_error(entry["start"], entry["due"], tz)
            if problem:
                lines.append(f"{number}. error: {problem}")
                continue
            try:
                saved, _ = self.store.upsert(self.owner, entry)
            except AgendaError as exc:
                lines.append(f"{number}. error: {exc}")
                continue
            lines.append(f"{number}. added id={saved['id']} [{saved['branch']}/{saved['kind']}] "
                         f"{saved['title']} — {_fmt_when(saved, tz)}")
        if self.busy is not None:
            lines.append("Added items are copied to Igris's Google calendars with reminders.")
        return "\n".join(lines)

    def agenda_list(self, start_date=None, end_date=None) -> str:
        now = self.now()
        tz = now.tzinfo
        first, last = _window(start_date, end_date, now)
        begin, end = self._range(first, last)
        items = self.store.between(self.owner, begin, end)
        if not items:
            return "no items"
        return "\n".join(f"id={it['id']} [{it['branch']}/{it['kind']}/{it['status']}] "
                         f"{it['title']} — {_fmt_when(it, tz)}" for it in items)

    def agenda_remove(self, id) -> str:  # noqa: A002 - the model passes "id"
        if not isinstance(id, str) or not id:
            raise ToolError("id must be an item id from agenda_list or agenda_add")
        try:
            self.store.delete(self.owner, id)
        except AgendaError as exc:
            raise ToolError(str(exc)) from exc
        return f"removed {id}"

    def agenda_update(self, id, title=None, start=None, due=None) -> str:  # noqa: A002
        """Change the title and/or times of one manual item, keeping its id, status and
        Google Calendar event (the sync moves the event and recomputes its reminders)."""
        if not isinstance(id, str) or not id:
            raise ToolError("id must be an item id from agenda_list or agenda_add")
        if title is None and start is None and due is None:
            raise ToolError("give at least one of title, start, due")
        try:
            item = self.store.get(self.owner, id)
        except AgendaError as exc:
            raise ToolError(str(exc)) from exc
        if item.get("source") != "manual" or not item.get("source_ref"):
            # a synced item (Moodle, mail, ...) would be reset by its next sync
            raise ToolError("only items added by hand or from chat can be changed; "
                            "this item comes from a synced source")
        tz = self._tz()
        changed = {k: v for k, v in item.items() if k != "all_day"}  # recomputed from the dates
        if title is not None:
            changed["title"] = title
        if start is not None:
            changed["start"] = _localize(start, tz)  # "" or null clears it
        if due is not None:
            changed["due"] = _localize(due, tz)
        problem = _order_error(changed.get("start"), changed.get("due"), tz)
        if problem:
            raise ToolError(problem)
        try:
            saved, created = self.store.upsert(self.owner, changed)
        except AgendaError as exc:
            raise ToolError(str(exc)) from exc
        if created or saved["id"] != id:  # cannot happen with a manual item's own source_ref
            raise ToolError("the item could not be updated in place")
        line = (f"updated id={saved['id']} [{saved['branch']}/{saved['kind']}] "
                f"{saved['title']} — {_fmt_when(saved, tz)}")
        if self.busy is not None:
            line += "\nThe item's Google Calendar event and reminders follow the change."
        return line


def register_agenda_tools(registry: ToolRegistry, store, owner: str, *,
                          busy: Callable | None = None,
                          now: Callable[[], datetime] = _local_now) -> AgendaTools:
    """Register free_slots, agenda_add, agenda_list, agenda_update and agenda_remove."""
    tools = AgendaTools(store, owner, busy=busy, now=now)
    branches, kinds = "|".join(BRANCHES), "|".join(KINDS)
    registry.add(Tool(
        name="free_slots",
        description=("Free time in the user's calendar (Google Calendar and Görev Takvimi) per day. "
                     "Args: start_date, end_date (YYYY-MM-DD, default today and 14 days), "
                     "minutes=60 (shortest slot), day_start='09:00', day_end='22:00'."),
        func=tools.free_slots,
    ))
    registry.add(Tool(
        name="agenda_add",
        description=("Add items to the user's Görev Takvimi (and their calendar). Args: items = list "
                     f"of {{title, branch ({branches}), kind ({kinds}), start, due}}. A timed item "
                     "has start and due as 'YYYY-MM-DDTHH:MM' (local time); an all-day item or a "
                     "deadline day uses due 'YYYY-MM-DD'. An item past midnight ends on the next "
                     "day's date (22:00 Sat to 08:00 Sun: start '...-14T22:00', due '...-15T08:00'). "
                     "Add all items in one call."),
        func=tools.agenda_add,
    ))
    registry.add(Tool(
        name="agenda_list",
        description=("List Görev Takvimi items with their ids, start and end. "
                     "Args: start_date, end_date (YYYY-MM-DD)."),
        func=tools.agenda_list,
    ))
    registry.add(Tool(
        name="agenda_update",
        description=("Change one item added by hand or from chat (its calendar event and reminders "
                     "follow). Args: id, and any of title, start, due (same formats as "
                     "agenda_add; an empty start clears it). Use it to fix a wrong time instead "
                     "of adding a duplicate."),
        func=tools.agenda_update,
    ))
    registry.add(Tool(
        name="agenda_remove",
        description="Remove one Görev Takvimi item (and its calendar event). Args: id.",
        func=tools.agenda_remove,
    ))
    return tools
