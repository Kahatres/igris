"""Calendar sync against an in-memory fake of the Google client (no network)."""

from datetime import datetime, timedelta, timezone
import threading

import pytest

from igris import gcal
from igris.agenda import AgendaStore
from igris.calendar_sync import CALENDAR_NAMES, CalendarSync, event_body, reminders, same_event

OWNER = "owner-id"
TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 11, 10, 20, 41, tzinfo=TZ)


class FakeGoogle:
    """Calendars as dicts of events; counts every write."""

    def __init__(self, scope_ok=True):
        self.calendars, self.writes, self.scope_ok, self.fail = {}, [], scope_ok, None
        self._next = 0

    def _id(self, prefix):
        self._next += 1
        return f"{prefix}{self._next}"

    def require_write(self):
        if self.fail:
            raise self.fail
        if not self.scope_ok:
            raise gcal.CalendarScopeError("no write scope")

    def primary_time_zone(self):
        return "Europe/Vilnius"

    def create_calendar(self, summary, zone=None):
        cid = self._id("cal")
        self.calendars[cid] = {"summary": summary, "zone": zone, "events": {}}
        self.writes.append(("create_calendar", summary))
        return cid

    def calendar_exists(self, cid):
        return cid in self.calendars

    def list_events(self, cid):
        if cid not in self.calendars:
            raise gcal.CalendarError("Calendar API returned HTTP 404 Not Found")
        # Google answers with its own time representation (UTC here)
        out = []
        for eid, body in self.calendars[cid]["events"].items():
            event = dict(body, id=eid)
            for key in ("start", "end"):
                if "dateTime" in event[key]:
                    moment = datetime.fromisoformat(event[key]["dateTime"]).astimezone(timezone.utc)
                    event[key] = {"dateTime": moment.isoformat().replace("+00:00", "Z")}
            out.append(event)
        return out

    def insert_event(self, cid, body):
        eid = self._id("ev")
        self.calendars[cid]["events"][eid] = body
        self.writes.append(("insert", cid, eid))
        return {"id": eid}

    def update_event(self, cid, eid, body):
        self.calendars[cid]["events"][eid] = body
        self.writes.append(("update", cid, eid))
        return {"id": eid}

    def delete_event(self, cid, eid):
        self.calendars[cid]["events"].pop(eid, None)
        self.writes.append(("delete", cid, eid))

    def events(self):
        return {eid: (cid, body) for cid, cal in self.calendars.items()
                for eid, body in cal["events"].items()}


@pytest.fixture
def env(tmp_path):
    agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    google = FakeGoogle()
    sync = CalendarSync(agenda, lambda: google, OWNER, tmp_path / "gcal.json", now=lambda: NOW)
    return agenda, google, sync


def add(agenda, **fields):
    item = {"branch": "okul", "kind": "exam", "title": "Matematik", "source": "manual",
            "status": "added", "due": "2026-11-14T10:00:00+02:00", **fields}
    return agenda.upsert(OWNER, item)[0]


def test_added_items_get_one_event_in_their_branch_calendar_and_the_id_is_stored(env):
    agenda, google, sync = env
    exam = add(agenda)
    add(agenda, branch="esn", kind="meeting", title="Toplantı", due="2026-11-13")
    add(agenda, title="Öneri", status="proposed")
    status = sync.sync()
    assert status["state"] == "ok" and status["created"] == 2
    assert status["last_sync"] == "2026-11-10T20:41:00+02:00"
    assert sorted(c["summary"] for c in google.calendars.values()) == [
        CALENDAR_NAMES["esn"], CALENDAR_NAMES["okul"]]
    assert all(c["zone"] == "Europe/Vilnius" for c in google.calendars.values())
    event_id = agenda.get(OWNER, exam["id"])["calendar_event_id"]
    cid, body = google.events()[event_id]
    assert google.calendars[cid]["summary"] == "Igris · Okul"
    assert body["summary"] == "Sınav: Matematik"
    assert body["extendedProperties"] == {"private": {"igris_ref": exam["id"]}}


def test_a_second_sync_changes_nothing(env):
    agenda, google, sync = env
    add(agenda)
    add(agenda, title="Proje", kind="assignment", due="2026-11-20")
    sync.sync()
    before = list(google.writes)
    status = sync.sync()
    assert google.writes == before
    assert (status["created"], status["updated"], status["deleted"]) == (0, 0, 0)


def test_a_changed_item_updates_its_event(env):
    agenda, google, sync = env
    exam = add(agenda, source="moodle", source_ref="uid-1", status="added")
    sync.sync()
    add(agenda, source="moodle", source_ref="uid-1", title="Matematik II",
        due="2026-11-15T09:00:00+02:00")
    status = sync.sync()
    assert status["updated"] == 1 and status["created"] == 0
    _, body = google.events()[agenda.get(OWNER, exam["id"])["calendar_event_id"]]
    assert body["summary"] == "Sınav: Matematik II"
    assert body["start"] == {"dateTime": "2026-11-15T09:00:00+02:00"}


def test_done_dismissed_and_deleted_items_lose_their_events(env):
    agenda, google, sync = env
    a, b, c = add(agenda, title="a"), add(agenda, title="b"), add(agenda, title="c")
    sync.sync()
    agenda.set_status(OWNER, a["id"], "done")
    agenda.set_status(OWNER, b["id"], "dismissed")
    agenda.delete(OWNER, c["id"])
    status = sync.sync()
    assert status["deleted"] == 3 and google.events() == {}
    assert agenda.get(OWNER, a["id"])["calendar_event_id"] is None


def test_moving_an_item_to_another_branch_moves_its_event(env):
    agenda, google, sync = env
    item = add(agenda, source="gmail", source_ref="m1")
    sync.sync()
    with agenda._lock:  # the store keeps branch on refresh; move it directly
        import sqlite3
        db = sqlite3.connect(agenda.path)
        db.execute("UPDATE items SET branch='kisisel' WHERE id=?", (item["id"],))
        db.commit()
        db.close()
    sync.sync()
    [(cid, _)] = google.events().values()
    assert google.calendars[cid]["summary"] == "Igris · Kişisel"


def test_events_without_an_igris_ref_are_left_alone_and_duplicates_are_removed(env):
    agenda, google, sync = env
    item = add(agenda)
    sync.sync()
    cid = next(iter(google.calendars))
    google.calendars[cid]["events"]["mine"] = {"summary": "owner's own note", "start": {"date": "2026-11-11"},
                                              "end": {"date": "2026-11-12"}}
    google.calendars[cid]["events"]["dup"] = event_body(agenda.get(OWNER, item["id"]))
    status = sync.sync()
    assert status["deleted"] == 1 and set(google.events()) == {"mine", agenda.get(OWNER, item["id"])["calendar_event_id"]}


def test_a_calendar_the_owner_deleted_is_created_again(env):
    agenda, google, sync = env
    add(agenda)
    sync.sync()
    google.calendars.clear()
    status = sync.sync()
    assert status["state"] == "ok" and status["created"] == 1 and len(google.calendars) == 1


def test_only_the_owner_items_are_synced(env):
    agenda, google, sync = env
    agenda.upsert("someone-else", {"branch": "okul", "title": "x", "source": "manual",
                                   "status": "added", "due": "2026-11-14"})
    assert sync.sync()["created"] == 0 and google.calendars == {}


@pytest.mark.parametrize("problem,state", [
    (gcal.CalendarScopeError("x"), "needs_consent"),
    (gcal.CalendarAuthError("x"), "auth_down"),
    (gcal.CalendarError("Calendar API returned HTTP 500"), "error"),
    (RuntimeError("boom"), "error"),
])
def test_failures_become_a_status_and_never_raise(env, problem, state):
    agenda, google, sync = env
    add(agenda)
    google.fail = problem
    status = sync.sync()
    assert status["state"] == state
    if state == "error":
        assert status["error"] in ("Calendar API returned HTTP 500", "RuntimeError")


def test_without_a_sign_in_or_an_owner(tmp_path):
    agenda = AgendaStore(tmp_path / "a.sqlite3")
    assert CalendarSync(agenda, lambda: None, OWNER, tmp_path / "g.json").sync()["state"] == "not_configured"
    assert CalendarSync(agenda, lambda: FakeGoogle(), lambda: None,
                        tmp_path / "g.json").sync()["state"] == "no_owner"
    google = FakeGoogle(scope_ok=False)
    assert CalendarSync(agenda, lambda: google, OWNER, tmp_path / "g.json").sync()["state"] == "needs_consent"


def test_calendar_ids_are_remembered_across_restarts(env, tmp_path):
    agenda, google, sync = env
    add(agenda)
    sync.sync()
    again = CalendarSync(agenda, lambda: google, OWNER, tmp_path / "gcal.json", now=lambda: NOW)
    add(agenda, title="İkinci")
    again.sync()
    assert len(google.calendars) == 1


# ---- event shape and reminders ----

def item(**fields):
    return {"id": "i1", "kind": "exam", "title": "T", "start": None,
            "due": "2026-11-14T10:00:00+02:00", "all_day": False, "location": "", **fields}


def test_reminders_per_kind_for_timed_events():
    assert reminders(item(kind="study")) == [60]
    assert reminders(item(kind="exam")) == [10080, 1440, 120]
    assert reminders(item(kind="assignment")) == [4320, 1440]
    assert reminders(item(kind="lab")) == reminders(item(kind="presentation")) == [4320, 1440]
    assert reminders(item(kind="attendance")) == [60]
    assert reminders(item(kind="other")) == [1440]
    assert reminders(item(kind="lesson")) == [30]  # an imported class
    assert event_body(item(kind="lesson"))["summary"] == "Ders: T"
    # payment at 18:30: one day before and 09:00 on the day (570 minutes before)
    assert reminders(item(kind="payment", due="2026-11-14T18:30:00+02:00")) == [1440, 570]
    assert reminders(item(kind="payment", due="2026-11-14T08:00:00+02:00")) == [1440]


def test_study_event_has_one_hour_reminder_and_syncs_idempotently(env):
    agenda, google, sync = env
    saved = add(agenda, kind="study", title="Vector practice")
    assert sync.sync()["created"] == 1
    event = google.events()[agenda.get(OWNER, saved["id"])["calendar_event_id"]][1]
    assert event["summary"].endswith(": Vector practice")
    assert event["reminders"]["overrides"] == [{"method": "popup", "minutes": 60}]
    assert sync.sync()["updated"] == 0


def test_all_day_reminders_land_at_nine_in_the_morning():
    assert reminders(item(kind="exam", due="2026-11-14", all_day=True)) == [9540, 900]
    assert reminders(item(kind="attendance", due="2026-11-14")) == [900]


def test_event_times():
    timed = event_body(item(due="2026-11-14T10:00:00+02:00"))
    assert timed["start"] == {"dateTime": "2026-11-14T10:00:00+02:00"}
    assert timed["end"] == {"dateTime": "2026-11-14T10:30:00+02:00"}
    span = event_body(item(start="2026-11-14T10:00:00+02:00", due="2026-11-14T12:00:00+02:00"))
    assert span["end"] == {"dateTime": "2026-11-14T12:00:00+02:00"}
    days = event_body(item(start="2026-11-14", due="2026-11-16", all_day=True, location="A1"))
    assert days["start"] == {"date": "2026-11-14"} and days["end"] == {"date": "2026-11-17"}
    assert days["location"] == "A1"
    assert days["reminders"]["useDefault"] is False


def test_same_event_compares_instants_not_strings():
    body = event_body(item())
    google = dict(body, start={"dateTime": "2026-11-14T08:00:00Z"},
                  end={"dateTime": "2026-11-14T08:30:00Z"})
    assert same_event(google, body)
    assert not same_event(dict(google, summary="other"), body)
    assert not same_event(dict(google, reminders={"useDefault": True}), body)


# ---- triggering ----

def test_request_wakes_the_loop_only_for_the_owner(env):
    agenda, google, sync = env
    sync.interval, sync.debounce = 60, 0
    stop = threading.Event()
    runs, first = [], threading.Event()
    real = sync.sync

    def counting():
        runs.append(1)
        if len(runs) >= 2:
            stop.set()
        result = real()
        first.set()
        return result

    sync.sync = counting
    thread = threading.Thread(target=sync.run, args=(stop,))
    thread.start()
    assert first.wait(5)  # the startup sync ran; now the loop waits
    sync.request("someone-else")
    assert thread.is_alive() and len(runs) == 1
    sync.request(OWNER)
    thread.join(5)
    assert not thread.is_alive() and len(runs) == 2


def test_agenda_changes_call_on_change(tmp_path):
    seen = []
    agenda = AgendaStore(tmp_path / "a.sqlite3", on_change=seen.append)
    it = add(agenda)
    agenda.set_status(OWNER, it["id"], "done")
    agenda.set_event_id(OWNER, it["id"], "ev")
    agenda.delete(OWNER, it["id"])
    assert seen == [OWNER, OWNER, OWNER]
    agenda.on_change = lambda owner: 1 / 0  # a broken listener never breaks the store
    add(agenda, title="b")


def test_a_large_import_is_paced_in_the_background_and_resumes_after_an_error(tmp_path):
    agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    google, pauses = FakeGoogle(), []
    sync = CalendarSync(agenda, lambda: google, OWNER, tmp_path / "gcal.json", now=lambda: NOW,
                        burst=20, pause=0.25, sleep=pauses.append)
    items = [{"branch": "okul", "kind": "other", "title": f"Ders {n}", "source": "manual",
              "source_ref": f"import:b:{n}", "status": "added",
              "start": f"2026-12-{1 + n % 28:02d}T{8 + n // 28:02d}:00:00+02:00",
              "due": f"2026-12-{1 + n % 28:02d}T{8 + n // 28:02d}:45:00+02:00"}
             for n in range(150)]
    agenda.replace_refs(OWNER, "manual", "import:b:", items[:60])
    real_insert, calls = google.insert_event, []

    def flaky(cid, body):  # Google refuses the 41st insert (rate limit)
        calls.append(cid)
        if len(calls) == 41:
            raise gcal.CalendarError("Calendar API returned HTTP 403 Forbidden")
        return real_insert(cid, body)

    google.insert_event = flaky
    assert sync.sync()["state"] == "error" and len(google.events()) == 40
    assert pauses == [0.25] * 21  # writes 21 to 41 waited
    agenda.replace_refs(OWNER, "manual", "import:b:", items)
    pauses.clear()
    status = sync.sync()
    assert status["state"] == "ok" and status["created"] == 110
    assert len(google.events()) == 150  # no duplicates after the resumed sync
    assert len(pauses) == 110 - 20
    assert sync.sync()["created"] == 0
