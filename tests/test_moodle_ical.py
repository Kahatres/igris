from datetime import datetime, timedelta, timezone
import io
import urllib.error

import pytest

from igris import moodle_ical
from igris.agenda import AgendaStore, kind_from_text
from igris.agenda_sources import AgendaSync, moodle_source, sync_items

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)
SECRET = "https://moodle.example.lt/calendar/export_execute.php?userid=7&authtoken=SECRETTOKEN"

FEED = "\r\n".join([
    "BEGIN:VCALENDAR",
    "VERSION:2.0",
    "PRODID:-//Moodle Pty Ltd//NONSGML Moodle Version 2024//EN",
    "BEGIN:VEVENT",
    "UID:101@moodle.example.lt",
    "SUMMARY:Homework 3 is due",
    "DESCRIPTION:Submit the report\\, the code\\; and a\\nshort note",
    "CATEGORIES:Algorithms",
    "DTSTART:20261112T215900Z",
    "DTEND:20261112T215900Z",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:102@moodle.example.lt",
    "SUMMARY:Midterm exam: chapters 1\\,2 and a very long title that Moodle folds over",
    "  two lines",
    'DTSTART;TZID="Europe/Vilnius":20261120T100000',
    "DTEND;TZID=Europe/Vilnius:20261120T120000",
    "CATEGORIES:Databases",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:103@moodle.example.lt",
    "SUMMARY:Lab 2 opens",
    "DTSTART;VALUE=DATE:20261115",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "SUMMARY:no uid is skipped",
    "DTSTART:20261115T100000Z",
    "END:VEVENT",
    "BEGIN:VEVENT",
    "UID:104@moodle.example.lt",
    "SUMMARY:no date is skipped",
    "END:VEVENT",
    "END:VCALENDAR",
    "",
])


def test_parser_unfolds_unescapes_and_reads_every_date_form():
    events = {e["uid"]: e for e in moodle_ical.parse_events(FEED, default_tz=TZ)}
    assert set(events) == {"101@moodle.example.lt", "102@moodle.example.lt",
                           "103@moodle.example.lt", "104@moodle.example.lt"}
    hw = events["101@moodle.example.lt"]
    assert hw["description"] == "Submit the report, the code; and a\nshort note"
    assert hw["start"] == "2026-11-12T21:59:00+00:00" and hw["categories"] == ["Algorithms"]
    mid = events["102@moodle.example.lt"]
    assert mid["summary"].endswith("folds over two lines") and "1,2" in mid["summary"]
    assert mid["start"].startswith("2026-11-20T10:00:00")
    assert events["103@moodle.example.lt"]["start"] == "2026-11-15"


def test_unknown_tzid_and_floating_times_use_the_default_zone():
    text = ("BEGIN:VEVENT\nUID:a\nSUMMARY:x\nDTSTART;TZID=Nowhere/Land:20261101T090000\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:b\nSUMMARY:y\nDTSTART:20261101T090000\nEND:VEVENT\n")
    starts = [e["start"] for e in moodle_ical.parse_events(text, default_tz=TZ)]
    assert starts == ["2026-11-01T09:00:00+02:00", "2026-11-01T09:00:00+02:00"]


def test_bad_values_are_ignored_not_fatal():
    assert moodle_ical.parse_when("2026-11-01", {}, TZ) is None
    assert moodle_ical.parse_when("20261399T250000Z", {}, TZ) is None
    assert moodle_ical.parse_events("garbage\nBEGIN:VEVENT\nUID:x\nnot a line\nEND:VEVENT") == \
        [{"uid": "x"}]


def test_titles_drop_the_due_phrase_and_keep_only_the_course_code():
    item = moodle_ical.to_item({"uid": "u1", "summary": "submit LAB_2 (late submission) turi būti pateikta",
                                "categories": ["KnF52029_20353_1_20261"],
                                "start": "2026-12-20T21:00:00+00:00"})
    assert item["title"] == "KnF52029: submit LAB_2 (late submission)"
    plain = moodle_ical.to_item({"uid": "u2", "summary": "Essay is due", "categories": ["Ethics"],
                                 "start": "2026-12-01T10:00:00+00:00"})
    assert plain["title"] == "Ethics: Essay"


def test_a_lithuanian_quiz_opening_is_not_a_deadline():
    opens = moodle_ical.to_item({"uid": "q1", "summary": "Quiz_4 (09.11.2026 18:00 - 20:00) atidaroma",
                                 "categories": ["KnF52029_20353_1_20261"],
                                 "start": "2026-11-09T16:00:00+00:00"})
    assert opens["mandatory"] is False and opens["due"] is None


def test_mapping_to_okul_items():
    items = {i["source_ref"]: i for i in moodle_ical.items_from_ical(FEED, default_tz=TZ)}
    assert set(items) == {"101@moodle.example.lt", "102@moodle.example.lt", "103@moodle.example.lt"}
    hw = items["101@moodle.example.lt"]
    assert hw["kind"] == "assignment" and hw["due"] == "2026-11-12T21:59:00+00:00"
    assert hw["start"] is None and hw["title"] == "Algorithms: Homework 3"
    assert hw["branch"] == "okul" and hw["source"] == "moodle" and hw["mandatory"] is True
    assert hw["status"] == "added" and hw["confidence"] == 1.0
    mid = items["102@moodle.example.lt"]
    assert mid["kind"] == "exam" and mid["start"].startswith("2026-11-20T10:00") and mid["due"] is None
    lab = items["103@moodle.example.lt"]
    assert lab["kind"] == "lab" and lab["mandatory"] is False and lab["start"] == "2026-11-15"


@pytest.mark.parametrize("text, kind", [
    ("Final exam", "exam"), ("Vize sınavı", "exam"), ("SINAV", "exam"), ("Quiz 2", "exam"),
    ("Egzaminas", "exam"), ("Lab report due", "lab"), ("Label printing", "other"),
    ("Proje sunumu", "presentation"), ("Yoklama", "attendance"), ("Kira ödemesi", "payment"),
    ("Ödev 4 teslim", "assignment"), ("Assignment 1", "assignment"), ("Finally free", "other"),
    ("Užduotis 2", "assignment"),
])
def test_kind_keywords(text, kind):
    assert kind_from_text(text) == kind


class Response:
    def __init__(self, body):
        self.body = io.BytesIO(body)

    def read(self, limit=-1):
        return self.body.read(limit)


def test_fetch_reads_the_feed_and_never_leaks_the_link():
    calls = []
    text = moodle_ical.fetch(SECRET, opener=lambda req, timeout: calls.append(req) or
                             Response(FEED.encode()))
    assert "VEVENT" in text and calls[0].full_url == SECRET

    def fail(code):
        def opener(req, timeout):
            if code == "net":
                raise urllib.error.URLError("connect to " + SECRET)
            raise urllib.error.HTTPError(SECRET, code, "x", {}, None)
        return opener

    for code in (403, "net"):
        with pytest.raises(moodle_ical.MoodleError) as raised:
            moodle_ical.fetch(SECRET, opener=fail(code))
        assert "SECRETTOKEN" not in str(raised.value)
    with pytest.raises(moodle_ical.MoodleError):
        moodle_ical.fetch("http://moodle.example.lt/x", opener=fail(403))
    with pytest.raises(moodle_ical.MoodleError, match="did not return a calendar"):
        moodle_ical.fetch(SECRET, opener=lambda req, timeout: Response(b"<html>login</html>"))
    with pytest.raises(moodle_ical.MoodleError, match="5 MB"):
        moodle_ical.fetch(SECRET, opener=lambda req, timeout: Response(b"x" * (moodle_ical.MAX_BYTES + 1)))


@pytest.fixture
def store(tmp_path):
    return AgendaStore(tmp_path / "agenda.sqlite3")


def test_sync_is_idempotent_and_updates_dates_but_keeps_status(store):
    items = moodle_ical.items_from_ical(FEED, default_tz=TZ)
    assert sync_items(store, "o", "moodle", items, now=NOW)["added"] == 3
    again = sync_items(store, "o", "moodle", items, now=NOW)
    assert again == {"added": 0, "updated": 3, "dismissed": 0, "skipped": 0}
    hw = next(i for i in store.list("o") if i["source_ref"] == "101@moodle.example.lt")
    store.set_status("o", hw["id"], "done")
    moved = FEED.replace("DTSTART:20261112T215900Z\r\nDTEND:20261112T215900Z",
                         "DTSTART:20261113T215900Z\r\nDTEND:20261113T215900Z")
    sync_items(store, "o", "moodle", moodle_ical.items_from_ical(moved, default_tz=TZ), now=NOW)
    hw = store.get("o", hw["id"])
    assert hw["due"] == "2026-11-13T21:59:00+00:00" and hw["status"] == "done"
    assert len(store.list("o", include_done=True)) == 3


def test_removed_events_are_dismissed_only_when_open_and_upcoming(store):
    items = moodle_ical.items_from_ical(FEED, default_tz=TZ)
    past = dict(items[0], source_ref="old@moodle", due="2026-11-01T10:00:00+02:00")
    manual = {"branch": "okul", "kind": "other", "title": "kendi notum", "due": "2026-11-20",
              "source": "manual", "status": "added"}
    store.upsert("o", manual)
    sync_items(store, "o", "moodle", items + [past], now=NOW)
    done = next(i for i in store.list("o") if i["source_ref"] == "102@moodle.example.lt")
    store.set_status("o", done["id"], "done")
    counts = sync_items(store, "o", "moodle", [], now=NOW)
    assert counts["dismissed"] == 2  # homework and lab; the exam is done, the past one stays
    by_ref = {i["source_ref"]: i["status"] for i in store.list("o", include_done=True)}
    assert by_ref["101@moodle.example.lt"] == by_ref["103@moodle.example.lt"] == "dismissed"
    assert by_ref["102@moodle.example.lt"] == "done" and by_ref["old@moodle"] == "added"
    assert [i["title"] for i in store.list("o") if i["source"] == "manual"] == ["kendi notum"]


def test_a_bad_item_is_skipped_and_other_owners_are_untouched(store):
    items = moodle_ical.items_from_ical(FEED, default_tz=TZ)
    store.upsert("x", dict(items[0]))
    bad = dict(items[0], source_ref="bad", kind="nonsense")
    counts = sync_items(store, "o", "moodle", [bad] + items, now=NOW)
    assert counts["skipped"] == 1 and counts["added"] == 3
    sync_items(store, "o", "moodle", [], now=NOW)
    assert store.list("x")[0]["status"] == "added"


def test_moodle_source_states(store):
    sync = AgendaSync(store, lambda: "o", {"moodle": moodle_source(lambda: None)}, now=lambda: NOW,
                      clock=lambda: 50.0)
    assert sync.status()["moodle"]["state"] == "starting"
    assert sync.run()["moodle"] == {"state": "not_configured", "reason": "no_link", "error": "",
                                    "last_sync": None, "count": 0}

    def broken(req, timeout):
        raise urllib.error.HTTPError(SECRET, 503, "x", {}, None)

    sync.sources["moodle"] = moodle_source(lambda: SECRET, opener=broken)
    state = sync.run()["moodle"]
    assert state["state"] == "error" and "503" in state["error"] and "SECRET" not in state["error"]
    sync.sources["moodle"] = moodle_source(lambda: SECRET,
                                           opener=lambda req, timeout: Response(FEED.encode()))
    state = sync.run()["moodle"]
    assert state["state"] == "ok" and state["count"] == 3 and state["last_sync"] == 50.0
    assert len(store.list("o")) == 3


def test_without_an_owner_nothing_is_written(store):
    sync = AgendaSync(store, lambda: None, {"moodle": moodle_source(lambda: SECRET)})
    assert sync.run()["moodle"]["reason"] == "no_owner"
