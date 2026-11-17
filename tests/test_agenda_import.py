"""agenda_import: timetable expansion, iCalendar parsing, preview and import over a real
AgendaStore, with a Europe/Vilnius-like zone written here (no tz database needed)."""
from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from igris.agenda import AgendaStore
from igris.agenda_import import (MAX_OCCURRENCES, Entry, expand, parse_entries, parse_ics,
                                 register_import_tools)
from igris.tools import ToolError, ToolRegistry

OWNER = "a" * 32


def _last_sunday(year, month):
    day = date(year, month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() + 1) % 7)


class Vilnius(tzinfo):
    """EET/EEST: UTC+2, summer time from the last Sunday of March 03:00 to the last Sunday
    of October 04:00 local time (the EU rule)."""

    def utcoffset(self, dt):
        return timedelta(hours=2) + self.dst(dt)

    def dst(self, dt):
        if dt is None:
            return timedelta(0)
        wall = dt.replace(tzinfo=None)
        begin = datetime.combine(_last_sunday(wall.year, 3), datetime.min.time()).replace(hour=3)
        end = datetime.combine(_last_sunday(wall.year, 10), datetime.min.time()).replace(hour=4)
        return timedelta(hours=1) if begin <= wall < end else timedelta(0)

    def tzname(self, dt):
        return "EEST" if self.dst(dt) else "EET"


VZ = Vilnius()
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=VZ)  # Tuesday, still summer time


@pytest.fixture
def store(tmp_path):
    return AgendaStore(tmp_path / "agenda.sqlite3", clock=lambda: 1000.0)


def make(store, *, now=NOW, busy=None, uploads=None, lookup=None, changes=None):
    reg = ToolRegistry()
    tools = register_import_tools(reg, store, OWNER, busy=busy, now=lambda: now, zone=VZ,
                                  uploads=uploads)
    if lookup is not None:
        tools.lookup = lookup
    if changes is not None:
        store.on_change = lambda owner: changes.append(owner)
    return reg


MATH = {"title": "Matematik", "start": "2026-10-19T10:00", "end": "2026-10-19T11:30",
        "location": "201", "weekly": {"until": "2026-12-20", "except": ["2026-11-30"]}}


def imported(store, title=None):
    return [it for it in store.list(OWNER, include_done=True)
            if it["source_ref"].startswith("import:") and (title is None or it["title"] == title)]


# ---- expansion ----

def _entry(start, end, weekly=None, zone=VZ):
    return Entry("entry 1", "Ders", "okul", "other", "", start, end, zone, weekly)


def _rule(**kw):
    rule = {"interval": 1, "until": None, "count": None, "days": None, "except": set()}
    rule.update(kw)
    return rule


def test_weekly_until_keeps_the_local_clock_across_the_october_change():
    entry = _entry(datetime(2026, 10, 19, 10, 0), datetime(2026, 10, 19, 11, 30),
                   _rule(until=date(2026, 11, 2), days={0}))
    got = expand(entry, VZ)
    assert [o.begin.isoformat() for o in got] == [
        "2026-10-19T10:00:00+03:00", "2026-10-26T10:00:00+02:00", "2026-11-02T10:00:00+02:00"]
    assert all(o.end.strftime("%H:%M") == "11:30" for o in got)


def test_weekly_keeps_the_local_clock_across_the_march_change():
    entry = _entry(datetime(2027, 3, 22, 18, 0), datetime(2027, 3, 22, 19, 30),
                   _rule(count=2, days={0}))
    assert [o.begin.isoformat() for o in expand(entry, VZ)] == [
        "2027-03-22T18:00:00+02:00", "2027-03-29T18:00:00+03:00"]


def test_biweekly_several_days_and_excluded_dates():
    entry = _entry(datetime(2026, 11, 2, 8, 0), datetime(2026, 11, 2, 9, 0),
                   _rule(interval=2, days={0, 3}, until=date(2026, 11, 30),
                         **{"except": {date(2026, 11, 5)}}))
    days = [o.begin.date().isoformat() for o in expand(entry, VZ)]
    assert days == ["2026-11-02", "2026-11-16", "2026-11-19", "2026-11-30"]


def test_count_counts_excluded_occurrences_and_cut_shortens_a_repeat():
    entry = _entry(datetime(2026, 11, 2, 8, 0), datetime(2026, 11, 2, 9, 0),
                   _rule(count=3, days={0}, **{"except": {date(2026, 11, 9)}}))
    assert [o.begin.day for o in expand(entry, VZ)] == [2, 16]
    open_ended = _entry(datetime(2026, 11, 2, 8, 0), datetime(2026, 11, 2, 9, 0),
                        _rule(days={0}))
    assert [o.begin.day for o in expand(open_ended, VZ, date(2026, 11, 16))] == [2, 9, 16]


def test_an_overnight_entry_ends_on_the_next_day():
    entry = _entry(datetime(2026, 11, 14, 22, 0), datetime(2026, 11, 15, 8, 0),
                   _rule(count=2, days={5}))
    got = expand(entry, VZ)
    assert [(o.begin.day, o.end.day, o.end.hour) for o in got] == [(14, 15, 8), (21, 22, 8)]


# ---- JSON entries ----

def test_entries_validation_names_every_bad_entry():
    with pytest.raises(ToolError) as err:
        parse_entries([MATH, {"title": "", "start": "2026-10-19T10:00"},
                       {"title": "X", "start": "2026-10-19T10:00", "end": "2026-10-19T09:00",
                        "note": "n"},
                       {"title": "Y", "start": "2026-10-19T10:00", "end": "2026-10-19T11:00",
                        "weekly": {"days": ["Moonday"], "until": "2026-12-01"}}],
                      branch="okul", zone=VZ)
    text = str(err.value)
    assert "entry 2: title must not be empty" in text
    assert "entry 3: unknown field 'note'" in text
    assert "entry 4: weekly.days has an unknown day" in text
    assert "entry 1" not in text


def test_an_offset_in_start_is_read_on_the_owner_clock():
    [entry] = parse_entries([{"title": "A", "start": "2026-11-16T08:00Z",
                              "end": "2026-11-16T09:00Z"}], branch="okul", zone=VZ)
    assert entry.start == datetime(2026, 11, 16, 10, 0)


# ---- the tool: preview, import, idempotency, removal ----

def test_preview_writes_nothing_and_summarises(store):
    changes = []
    reg = make(store, changes=changes)
    out = reg.run("agenda_import", {"batch": "Uni-fall", "entries": [MATH]})
    assert out.startswith("preview batch=uni-fall (nothing was written to the calendar)")
    assert "occurrences: 8 from 2026-10-19 Mon 10:00-11:30 to 2026-12-14 Mon 10:00-11:30" in out
    assert "Mon 8: 10:00-11:30 Matematik [okul] x8" in out
    assert "8 new, 0 changed, 0 unchanged, 0 upcoming removed" in out
    assert "\"preview\": false" in out
    assert imported(store) == [] and changes == []


def test_import_creates_items_with_dst_correct_offsets(store):
    changes = []
    reg = make(store, changes=changes)
    out = reg.run("agenda_import", {"batch": "uni-fall", "entries": [MATH], "preview": False})
    assert out.startswith("imported batch=uni-fall: created 8, updated 0, unchanged 0, removed 0")
    items = imported(store)
    assert len(items) == 8 and changes == [OWNER]  # one sync request for the whole batch
    starts = sorted(it["start"] for it in items)
    assert starts[0] == "2026-10-19T10:00+03:00" and starts[1] == "2026-10-26T10:00+02:00"
    assert "2026-11-30T10:00+02:00" not in starts  # excluded
    item = items[0]
    assert item["status"] == "added" and item["source"] == "manual" and item["branch"] == "okul"
    assert item["location"] == "201" and item["kind"] == "lesson"
    assert item["source_ref"].startswith("import:uni-fall:2026-")


def test_reimport_updates_in_place_and_removes_what_left(store):
    reg = make(store)
    reg.run("agenda_import", {"batch": "uni-fall", "entries": [MATH], "preview": False})
    first = {it["source_ref"]: it["id"] for it in imported(store)}
    store.set_status(OWNER, next(iter(first.values())), "done")
    again = reg.run("agenda_import", {"batch": "uni-fall", "entries": [MATH], "preview": False})
    assert "created 0, updated 0, unchanged 8, removed 0" in again
    moved = dict(MATH, location="305", weekly={"until": "2026-12-06"})
    out = reg.run("agenda_import", {"batch": "uni-fall", "entries": [moved], "preview": False})
    assert "created 1, updated 6, unchanged 0, removed 2" in out  # 11-30 back, 12-07/14 gone
    after = {it["source_ref"]: it for it in imported(store)}
    assert all(after[ref]["id"] == first[ref] for ref in after if ref in first)
    assert {it["location"] for it in after.values()} == {"305"}
    assert "done" in {it["status"] for it in after.values()}  # status kept


def test_reimport_keeps_lessons_that_are_over(store):
    make(store).run("agenda_import", {"batch": "b", "entries": [MATH], "preview": False})
    later = make(store, now=datetime(2026, 11, 10, 12, 0, tzinfo=VZ))
    out = later.run("agenda_import", {"batch": "b", "preview": False, "entries": [
        dict(MATH, title="Fizik")]})
    titles = sorted(it["title"] for it in imported(store))
    # four Matematik lessons were over and stay; the upcoming four became Fizik
    assert titles.count("Matematik") == 4 and titles.count("Fizik") == 4
    assert "removed 4" in out and "4 occurrence(s) already over were skipped" in out


def test_remove_deletes_a_whole_batch(store):
    reg = make(store)
    reg.run("agenda_import", {"batch": "uni-fall", "entries": [MATH], "preview": False})
    reg.run("agenda_import", {"batch": "gym", "entries": [
        {"title": "Spor", "branch": "kisisel", "start": "2026-10-20T18:00",
         "end": "2026-10-20T19:00", "weekly": {"count": 3}}], "preview": False})
    assert "8 item(s) deleted" in reg.run("agenda_import_remove", {"batch": "uni-fall"})
    assert {it["title"] for it in imported(store)} == {"Spor"}
    with pytest.raises(ToolError, match="imported batches: gym"):
        reg.run("agenda_import_remove", {"batch": "uni-fall"})


def test_past_occurrences_are_skipped_and_leave_the_sections(store):
    reg = make(store, now=datetime(2026, 11, 3, 12, 0, tzinfo=VZ))
    out = reg.run("agenda_import", {"batch": "b", "entries": [MATH], "preview": False})
    assert "3 occurrence(s) already over were skipped" in out and "created 5" in out
    later = datetime(2026, 11, 10, 12, 0, tzinfo=VZ)  # the 11-09 lesson is over now
    assert store.sections(OWNER, "okul", now=later)["overdue"] == []


def test_conflicts_with_an_overnight_shift_are_listed_not_skipped(store):
    store.upsert(OWNER, {"branch": "kisisel", "title": "Gece vardiyası", "source": "manual",
                         "status": "added", "start": "2026-11-14T22:00+02:00",
                         "due": "2026-11-15T08:00+02:00"})
    reg = make(store)
    entries = [{"title": "Lab", "kind": "lab", "start": "2026-11-14T21:00",
                "end": "2026-11-14T22:30"},
               {"title": "Seminer", "start": "2026-11-15T07:30", "end": "2026-11-15T09:00"},
               {"title": "Okuma", "start": "2026-11-15T08:00", "end": "2026-11-15T09:00"}]
    out = reg.run("agenda_import", {"batch": "b", "entries": entries})
    assert "conflicts with existing items (2; nothing is moved or skipped):" in out
    assert ("2026-11-14 Sat 21:00-22:30 Lab overlaps Gece vardiyası "
            "(2026-11-14 Sat 22:00 - 2026-11-15 Sun 08:00)") in out
    assert "2026-11-15 Sun 07:30-09:00 Seminer overlaps Gece vardiyası" in out
    assert "Okuma overlaps Gece" not in out  # starts when the shift ends
    assert "overlaps inside this import (1;" in out  # Seminer and Okuma
    out = reg.run("agenda_import", {"batch": "b", "entries": entries, "preview": False})
    assert "created 3" in out and len(imported(store)) == 3


def test_google_events_count_as_conflicts(store):
    calls = []

    def busy(time_min, time_max):
        calls.append((time_min, time_max))
        return {"status": "ok", "events": [{"title": "Dişçi", "start": "2026-10-26T10:30:00+02:00",
                                            "end": "2026-10-26T11:00:00+02:00"}]}

    out = make(store, busy=busy).run("agenda_import", {"batch": "b", "entries": [MATH]})
    assert "2026-10-26 Mon 10:00-11:30 Matematik overlaps Dişçi" in out and "Google Calendar" in out
    assert len(calls) == 1 and calls[0][1] - calls[0][0] <= 60 * 86400


def test_caps_and_validation_errors_write_nothing(store):
    reg = make(store)
    many = [dict(MATH, title=f"Ders {n}", weekly={"until": "2027-06-30"}) for n in range(12)]
    with pytest.raises(ToolError, match=f"at most {MAX_OCCURRENCES} per call"):
        reg.run("agenda_import", {"batch": "b", "entries": many, "preview": False})
    with pytest.raises(ToolError, match="entry 2: a timed entry lasts at most 24 hours"):
        reg.run("agenda_import", {"batch": "b", "preview": False, "entries": [
            MATH, {"title": "Uzun", "start": "2026-10-20T08:00", "end": "2026-10-21T09:00"}]})
    with pytest.raises(ToolError, match="entry 1: weekly.until is after 2027-10-14"):
        reg.run("agenda_import", {"batch": "b", "preview": False, "entries": [
            dict(MATH, weekly={"until": "2027-12-01"})]})
    with pytest.raises(ToolError, match="needs until or count"):
        reg.run("agenda_import", {"batch": "b", "preview": False, "entries": [
            dict(MATH, weekly={})]})
    with pytest.raises(ToolError, match="entry 1 and entry 2 both give Matematik"):
        reg.run("agenda_import", {"batch": "b", "preview": False, "entries": [MATH, MATH]})
    with pytest.raises(ToolError, match="batch must be a short name"):
        reg.run("agenda_import", {"batch": "my timetable!", "entries": [MATH]})
    with pytest.raises(ToolError, match="exactly one of"):
        reg.run("agenda_import", {"batch": "b", "entries": [MATH], "ics": "BEGIN:VCALENDAR"})
    assert imported(store) == []
    # an all-day entry may span days; a timed one past midnight is fine
    out = reg.run("agenda_import", {"batch": "b", "preview": False, "entries": [
        {"title": "Sınav haftası", "start": "2026-12-14", "end": "2026-12-18"},
        {"title": "Gece", "start": "2026-11-14T22:00", "end": "2026-11-15T08:00"}]})
    assert "created 2" in out
    exam = imported(store, "Sınav haftası")[0]
    assert (exam["start"], exam["due"], exam["kind"]) == ("2026-12-14", "2026-12-18", "exam")


def test_entries_may_come_in_a_content_block(store):
    import json

    out = make(store).run("agenda_import", {"batch": "b", "content": json.dumps([MATH])})
    assert "occurrences: 8" in out


# ---- iCalendar ----

ICS = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0",
    "BEGIN:VTIMEZONE", "TZID:Europe/Vilnius", "BEGIN:STANDARD", "TZOFFSETTO:+0200",
    "END:STANDARD", "END:VTIMEZONE",
    "BEGIN:VEVENT", "UID:math@example", "SUMMARY:Matematinė analizė (paskai",
    " ta)", "LOCATION:MIF 201\\, Naugarduko g.",
    "DTSTART;TZID=Europe/Vilnius:20261019T100000", "DTEND;TZID=Europe/Vilnius:20261019T113000",
    "RRULE:FREQ=WEEKLY;UNTIL=20261102T215959Z;BYDAY=MO", "EXDATE;TZID=Europe/Vilnius:20261026T100000",
    "BEGIN:VALARM", "TRIGGER:-PT15M", "ACTION:DISPLAY", "END:VALARM", "END:VEVENT",
    "BEGIN:VEVENT", "UID:prog@example", "SUMMARY:Programavimas", "DTSTART:20261020T060000Z",
    "DTEND:20261020T073000Z", "RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=2", "END:VEVENT",
    "BEGIN:VEVENT", "UID:daily@example", "SUMMARY:Kasdien", "DTSTART:20261020T060000Z",
    "DURATION:PT1H", "RRULE:FREQ=DAILY;COUNT=3", "END:VEVENT",
    "BEGIN:VEVENT", "UID:day@example", "SUMMARY:Egzaminas", "DTSTART;VALUE=DATE:20261215",
    "DTEND;VALUE=DATE:20261216", "END:VEVENT",
    "BEGIN:VEVENT", "UID:gone@example", "SUMMARY:Atšaukta", "STATUS:CANCELLED",
    "DTSTART:20261021T060000Z", "DTEND:20261021T070000Z", "END:VEVENT",
    "BEGIN:VTODO", "UID:t", "END:VTODO",
    "END:VCALENDAR", ""])


def no_tzdb(name):
    return None


def with_vilnius(name):
    return VZ if name == "Europe/Vilnius" else None


def test_ics_parses_folding_tzid_utc_rrule_exdate_and_reports_the_rest():
    entries, notes, unread = parse_ics(ICS, branch="okul", zone=VZ, lookup=with_vilnius)
    assert unread == 1  # the daily event
    by_title = {e.title: e for e in entries}
    assert set(by_title) == {"Matematinė analizė (paskaita)", "Programavimas", "Egzaminas"}
    math = by_title["Matematinė analizė (paskaita)"]
    assert math.location == "MIF 201, Naugarduko g."
    assert [o.begin.isoformat() for o in expand(math, VZ)] == [
        "2026-10-19T10:00:00+03:00", "2026-11-02T10:00:00+02:00"]  # 10-26 excluded
    prog = [o.begin.isoformat() for o in expand(by_title["Programavimas"], VZ)]
    assert prog == ["2026-10-20T09:00:00+03:00", "2026-11-03T08:00:00+02:00"]  # UTC rule
    exam = expand(by_title["Egzaminas"], VZ)
    assert [(o.begin, o.end) for o in exam] == [(date(2026, 12, 15), date(2026, 12, 15))]
    text = "; ".join(notes)
    assert "Kasdien): skipped, RRULE FREQ=DAILY is not supported" in text
    assert "1 cancelled event(s) skipped" in text
    assert "1 alarm(s) ignored" in text and "1 VTODO component(s) ignored" in text
    assert "not known on this machine" not in text


def test_ics_without_a_tz_database_reads_tzid_as_local_time_and_says_so():
    entries, notes, _ = parse_ics(ICS, branch="okul", zone=VZ, lookup=no_tzdb)
    math = next(e for e in entries if e.title.startswith("Matematinė"))
    assert expand(math, VZ)[0].begin.isoformat() == "2026-10-19T10:00:00+03:00"
    assert any("Europe/Vilnius is not known on this machine" in n for n in notes)


def test_ics_moved_occurrence_replaces_the_original():
    text = "\n".join([
        "BEGIN:VCALENDAR",
        "BEGIN:VEVENT", "UID:x", "SUMMARY:Fizika", "DTSTART:20261102T080000",
        "DTEND:20261102T093000", "RRULE:FREQ=WEEKLY;COUNT=3", "END:VEVENT",
        "BEGIN:VEVENT", "UID:x", "SUMMARY:Fizika", "RECURRENCE-ID:20261109T080000",
        "DTSTART:20261110T120000", "DTEND:20261110T133000", "END:VEVENT",
        "END:VCALENDAR"])
    entries, _, _ = parse_ics(text, branch="okul", zone=VZ)
    starts = sorted(o.begin.strftime("%m-%d %H:%M") for e in entries for o in expand(e, VZ))
    assert starts == ["11-02 08:00", "11-10 12:00", "11-16 08:00"]


def test_ics_import_through_the_tool_and_an_uploaded_file(store):
    files = {"f" * 32: ("tvarkarastis.ics", ICS.encode("utf-8"))}
    reg = make(store, uploads=lambda file_id: files[file_id], lookup=with_vilnius)
    out = reg.run("agenda_import", {"batch": "uni", "file_id": "f" * 32, "preview": False})
    assert "created 5" in out and "FREQ=DAILY is not supported" in out
    assert len(imported(store)) == 5
    with pytest.raises(ToolError, match="file not found"):
        reg.run("agenda_import", {"batch": "uni", "file_id": "0" * 32})
    again = reg.run("agenda_import", {"batch": "uni", "ics": ICS, "preview": False})
    assert "created 0, updated 0, unchanged 5" in again


def test_ics_repeat_without_end_needs_until(store):
    text = "\n".join(["BEGIN:VCALENDAR", "BEGIN:VEVENT", "UID:x", "SUMMARY:Anglų k.",
                      "DTSTART:20261021T080000", "DTEND:20261021T093000",
                      "RRULE:FREQ=WEEKLY", "END:VEVENT", "END:VCALENDAR"])
    reg = make(store)
    out = reg.run("agenda_import", {"batch": "b", "ics": text})
    assert "skipped, a weekly repeat needs until or count" in out and "occurrences: 0" in out
    out = reg.run("agenda_import", {"batch": "b", "ics": text, "until": "2026-11-11"})
    assert "occurrences: 4" in out


# ---- registration ----

def test_registers_two_tools_with_an_example(store):
    reg = make(store)
    assert reg.names() == ["agenda_import", "agenda_import_remove"]
    description = reg.get("agenda_import").description
    assert '"weekly": {"until": "2026-12-20"' in description and "preview" in description


# ---- review fixes: done items, unreadable events, parser robustness, lesson kind ----

TWO = [dict(MATH, weekly=None, start="2026-11-16T10:00", end="2026-11-16T11:30"),
       dict(MATH, title="Fizik", weekly=None, start="2026-11-17T10:00",
            end="2026-11-17T11:30")]


def test_reimport_and_remove_never_delete_done_items_unless_asked(store):
    reg = make(store)
    reg.run("agenda_import", {"batch": "b", "entries": TWO, "preview": False})
    math = imported(store, "Matematik")[0]
    store.set_status(OWNER, math["id"], "done")
    preview = reg.run("agenda_import", {"batch": "b", "entries": TWO[1:]})
    assert "0 upcoming removed" in preview and "1 item(s) marked done" in preview
    out = reg.run("agenda_import", {"batch": "b", "entries": TWO[1:], "preview": False})
    assert "removed 0" in out and "1 item(s) marked done" in out
    assert {it["title"] for it in imported(store)} == {"Matematik", "Fizik"}
    out = reg.run("agenda_import_remove", {"batch": "b"})
    assert "1 item(s) deleted" in out and "1 item(s) marked done were kept" in out
    assert [it["title"] for it in imported(store)] == ["Matematik"]
    out = reg.run("agenda_import_remove", {"batch": "b", "include_done": True})
    assert "1 item(s) deleted" in out and imported(store) == []


def _vevent(uid, *lines):
    return ["BEGIN:VEVENT", f"UID:{uid}", *lines, "END:VEVENT"]


def _cal(*events):
    return "\n".join(["BEGIN:VCALENDAR", *[line for ev in events for line in ev],
                      "END:VCALENDAR"])


A = _vevent("a", "SUMMARY:A", "DTSTART:20261116T100000", "DTEND:20261116T113000")
B = _vevent("b", "SUMMARY:B", "DTSTART:20261117T100000", "DTEND:20261117T113000")
B_BAD = _vevent("b", "SUMMARY:B", "DTSTART:20261301T100000", "DTEND:20261117T113000")


def test_an_unreadable_event_turns_off_removal_for_the_whole_reimport(store):
    reg = make(store)
    reg.run("agenda_import", {"batch": "b", "ics": _cal(A, B), "preview": False})
    preview = reg.run("agenda_import", {"batch": "b", "ics": _cal(A, B_BAD)})
    assert "0 upcoming removed" in preview
    assert "1 event(s) could not be read, so nothing missing from this import is removed: " \
           "1 stored item(s) kept" in preview
    out = reg.run("agenda_import", {"batch": "b", "ics": _cal(A, B_BAD), "preview": False})
    assert "removed 0" in out and "1 stored item(s) kept" in out and "event 2 (B): skipped" in out
    assert {it["title"] for it in imported(store)} == {"A", "B"}
    # once every event reads again, what left the timetable is removed as usual
    out = reg.run("agenda_import", {"batch": "b", "ics": _cal(A), "preview": False})
    assert "removed 1" in out and {it["title"] for it in imported(store)} == {"A"}


@pytest.mark.parametrize("lines, reason", [
    (["DTSTART;VALUE=DATE:20261120", "DTEND;VALUE=DATE:20261118"], "DTEND is before DTSTART"),
    (["DTSTART:20261120T100000", "DTEND;VALUE=DATE:20261121"], "must both be dates"),
    (["DTSTART:20261120T100000", "DURATION:P1000000000D"], "too long"),
    (["DTSTART:20261120T100000", "DURATION:P30D"], "longer than 14 days"),
    (["DTSTART:20261120T100000", "DURATION:-PT1H"], "negative DURATION"),
    (["DTSTART:20261301T100000", "DTEND:20261120T110000"], "skipped"),
    (["DTSTART:20261120T100000", "DTEND:20261120T110000", "RRULE:FREQ=WEEKLY;INTERVAL=x"],
     "skipped"),
])
def test_bad_events_are_reported_never_raised_or_corrected(store, lines, reason):
    text = _cal(_vevent("x", "SUMMARY:Kötü", *lines), A)
    entries, notes, unread = parse_ics(text, branch="okul", zone=VZ)
    assert [e.title for e in entries] == ["A"] and unread == 1
    assert any("event 1 (Kötü): skipped" in n and reason in n for n in notes), notes
    out = make(store).run("agenda_import", {"batch": "b", "ics": text})
    assert "occurrences: 1" in out


def test_wkst_decides_which_weeks_an_every_other_week_rule_keeps():
    def days(wkst):
        text = _cal(_vevent("w", "SUMMARY:W", "DTSTART:20261117T090000",
                            "DTEND:20261117T100000",
                            f"RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST={wkst}"))
        [entry], _, _ = parse_ics(text, branch="okul", zone=VZ)
        return [o.begin.strftime("%m-%d") for o in expand(entry, VZ)]

    # the RFC 5545 example, moved to November 2026
    assert days("MO") == ["11-17", "11-22", "12-01", "12-06"]
    assert days("SU") == ["11-17", "11-29", "12-01", "12-13"]


def test_a_utc_until_is_compared_as_an_instant(store):
    text = _cal(_vevent("u", "SUMMARY:Gece dersi",
                        "DTSTART;TZID=Europe/Vilnius:20261116T003000",
                        "DTEND;TZID=Europe/Vilnius:20261116T013000",
                        "RRULE:FREQ=WEEKLY;UNTIL=20261115T230000Z"))
    out = make(store, lookup=with_vilnius).run("agenda_import", {"batch": "b", "ics": text})
    assert "occurrences: 1 from 2026-11-16 Mon 00:30-01:30" in out and "skipped" not in out


def test_lessons_get_their_own_kind_and_keywords_still_win(store):
    reg = make(store)
    reg.run("agenda_import", {"batch": "b", "preview": False, "entries": [
        TWO[0], dict(TWO[1], title="Fizika lab"), dict(TWO[1], title="Vidurio egzaminas",
                                                        start="2026-11-18T10:00",
                                                        end="2026-11-18T11:30")]})
    kinds = {it["title"]: it["kind"] for it in imported(store)}
    assert kinds == {"Matematik": "lesson", "Fizika lab": "lab", "Vidurio egzaminas": "exam"}
    entries, _, _ = parse_ics(_cal(A), branch="okul", zone=VZ)
    assert entries[0].kind == "lesson"


def test_a_preview_from_an_upload_only_writes_the_file_audit_line(store):
    files = {"f" * 32: ("ders.ics", _cal(A).encode())}
    reads = []

    def uploads(file_id):
        reads.append(file_id)
        return files[file_id]

    out = make(store, uploads=uploads).run("agenda_import", {"batch": "b", "file_id": "f" * 32})
    assert out.startswith("preview batch=b (nothing was written to the calendar)")
    assert reads == ["f" * 32] and imported(store) == []


@pytest.mark.parametrize("ending", ["", "END:VCALENDAR", "END:VTODO\nEND:VCALENDAR"])
def test_truncated_or_mismatched_calendar_never_removes_existing_items(store, ending):
    reg = make(store)
    reg.run("agenda_import", {"batch": "b", "ics": _cal(A, B), "preview": False})
    broken = "\n".join(["BEGIN:VCALENDAR", *A, *B[:-1], ending])
    with pytest.raises(ToolError, match="calendar component"):
        reg.run("agenda_import", {"batch": "b", "ics": broken, "preview": False})
    assert {it["title"] for it in imported(store)} == {"A", "B"}


def test_cancelled_recurrence_is_excluded_from_master(store):
    master = _vevent("a", "SUMMARY:A", "DTSTART:20261116T100000",
                     "DTEND:20261116T113000", "RRULE:FREQ=WEEKLY;COUNT=3")
    cancelled = _vevent("a", "RECURRENCE-ID:20261123T100000", "STATUS:CANCELLED")
    reg = make(store)
    reg.run("agenda_import", {"batch": "b", "ics": _cal(master), "preview": False})
    out = reg.run("agenda_import", {"batch": "b", "ics": _cal(master, cancelled),
                                    "preview": False})
    assert "removed 1" in out
    assert [it["start"][:10] for it in imported(store)] == ["2026-11-16", "2026-11-30"]


@pytest.mark.parametrize("text", [
    "description: BEGIN:VCALENDAR",
    "BEGIN:VCALENDAR\nEND:VCALENDAR\nBEGIN:VCALENDAR\nEND:VCALENDAR",
    "BEGIN:VCALENDAR\nBEGIN:VTIMEZONE\n" + "\n".join(A)
    + "\nEND:VTIMEZONE\nEND:VCALENDAR",
])
def test_calendar_root_and_event_parent_are_validated(text):
    with pytest.raises(ToolError, match="calendar component"):
        parse_ics(text, branch="okul", zone=VZ)
