from datetime import datetime, timedelta, timezone

import pytest

from igris import gcal
from igris.agenda import AgendaStore
from igris.agenda_sources import ESN_DAYS, AgendaSync, esn_item, esn_source, moodle_source

TZ = timezone(timedelta(hours=2))
NOW = datetime(2026, 11, 10, 12, 0, tzinfo=TZ)
EVENTS = [
    {"id": "e1", "title": "ESN haftalık toplantı", "start": "2026-11-12T18:00:00+02:00",
     "end": "2026-11-12T19:00:00+02:00", "all_day": False, "location": "Ofis"},
    {"id": "e2", "title": "Welcome week presentation", "start": "2026-11-20", "end": "2026-11-21",
     "all_day": True, "location": ""},
    {"id": "e3", "title": "Trip", "start": "2026-11-14T08:00:00Z", "end": "", "all_day": False,
     "location": ""},
]


class FakeCalendar:
    def __init__(self, events=EVENTS, error=None):
        self.events, self.error, self.calls = list(events), error, []

    def upcoming(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return list(self.events)


@pytest.fixture
def store(tmp_path):
    return AgendaStore(tmp_path / "agenda.sqlite3")


def test_esn_events_become_proposed_esn_items():
    items = [esn_item(e) for e in EVENTS]
    assert [i["kind"] for i in items] == ["meeting", "presentation", "meeting"]
    assert all(i["branch"] == "esn" and i["source"] == "esn_calendar" and i["status"] == "proposed"
               for i in items)
    assert items[0]["source_ref"] == "e1" and items[0]["location"] == "Ofis"
    assert items[1]["all_day"] is True and items[1]["start"] == "2026-11-20"
    assert items[2]["start"] == "2026-11-14T08:00:00+00:00"


def test_esn_sync_reads_each_calendar_for_sixty_days(store):
    client = FakeCalendar()
    sync = AgendaSync(store, lambda: "o", {"esn": esn_source(("a@group", "b@group"), lambda: client)},
                      now=lambda: NOW, clock=lambda: 9.0)
    state = sync.run()["esn"]
    assert state["state"] == "ok" and state["count"] == 6 and state["last_sync"] == 9.0
    assert [c["calendar_id"] for c in client.calls] == ["a@group", "b@group"]
    assert client.calls[0]["days"] == ESN_DAYS and client.calls[0]["now"] == NOW.timestamp()
    assert sorted(i["source_ref"] for i in store.list("o", branch="esn")) == ["e1", "e2", "e3"]


def test_esn_event_removed_from_the_calendar_is_dismissed(store):
    client = FakeCalendar()
    sync = AgendaSync(store, lambda: "o", {"esn": esn_source(("a@group",), lambda: client)},
                      now=lambda: NOW)
    sync.run()
    client.events = EVENTS[:1]
    assert sync.run()["esn"]["changes"]["dismissed"] == 2
    assert [i["source_ref"] for i in store.list("o")] == ["e1"]


@pytest.mark.parametrize("ids, factory, error, state, reason", [
    ((), lambda: FakeCalendar(), None, "not_configured", "no_calendar_id"),
    (("a",), lambda: None, None, "not_configured", "no_google"),
    (("a",), None, gcal.CalendarNotFoundError("404"), "error", "not_shared"),
    (("a",), None, gcal.CalendarScopeError("403"), "error", "needs_consent"),
    (("a",), None, gcal.CalendarAuthError("401"), "error", "auth_down"),
    (("a",), None, gcal.CalendarError("Calendar API returned HTTP 500"), "error", "error"),
])
def test_esn_problems_are_reported_per_reason(store, ids, factory, error, state, reason):
    if factory is None:
        client = FakeCalendar(error=error)
        factory = lambda: client  # noqa: E731
    sync = AgendaSync(store, lambda: "o", {"esn": esn_source(ids, factory)}, now=lambda: NOW)
    result = sync.run()["esn"]
    assert (result["state"], result["reason"]) == (state, reason)
    assert store.list("o") == []


def test_one_failing_source_does_not_stop_the_other(store):
    def explode(now):
        raise RuntimeError("parser bug")

    sync = AgendaSync(store, lambda: "o", {"moodle": explode,
                                           "esn": esn_source(("a",), lambda: FakeCalendar())},
                      now=lambda: NOW)
    status = sync.run()
    assert status["moodle"]["state"] == "error" and status["moodle"]["error"] == "RuntimeError"
    assert status["esn"]["state"] == "ok"
    assert set(sync.status()) == {"moodle", "esn"}


def test_moodle_source_is_listed_before_configuration(store):
    sync = AgendaSync(store, lambda: "o", {"moodle": moodle_source(lambda: "")})
    assert sync.run()["moodle"]["reason"] == "no_link"
