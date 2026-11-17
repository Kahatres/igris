"""agenda_tools: chat tools over a real AgendaStore with a fixed clock and a fake Google reader."""
from datetime import datetime, timedelta, timezone

import pytest

from igris.agenda import AgendaStore
from igris.agenda_tools import register_agenda_tools
from igris.tools import ToolError, ToolRegistry

OWNER = "a" * 32
OTHER = "b" * 32
TR = timezone(timedelta(hours=3))
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=TR)  # Wednesday


@pytest.fixture
def store(tmp_path):
    return AgendaStore(tmp_path / "agenda.sqlite3", clock=lambda: 1000.0)


def make(store, owner=OWNER, events=None, status="ok", calls=None):
    reg = ToolRegistry()

    def busy(time_min, time_max):
        if calls is not None:
            calls.append((time_min, time_max))
        return {"status": status, "events": events or []}

    register_agenda_tools(reg, store, owner, busy=busy, now=lambda: NOW)
    return reg


def test_registers_five_tools(store):
    assert make(store).names() == ["agenda_add", "agenda_list", "agenda_remove", "agenda_update",
                                   "free_slots"]


def test_an_overnight_item_shows_its_end_date(store):
    reg = make(store)
    out = reg.run("agenda_add", {"items": [
        {"title": "Vardiya", "branch": "kisisel", "start": "2026-11-14T22:00",
         "due": "2026-11-15T08:00"}]})
    assert "2026-11-14 Sat 22:00 - 2026-11-15 Sun 08:00" in out
    listed = reg.run("agenda_list", {"start_date": "2026-11-14", "end_date": "2026-11-16"})
    assert "2026-11-14 Sat 22:00 - 2026-11-15 Sun 08:00" in listed
    # a same-day item keeps the short form, a multi-day all-day item shows both days
    reg.run("agenda_add", {"items": [{"title": "Gezi", "branch": "kisisel",
                                      "start": "2026-11-20", "due": "2026-11-22"}]})
    assert "2026-11-20 - 2026-11-22 (all day)" in reg.run(
        "agenda_list", {"start_date": "2026-11-20", "end_date": "2026-11-23"})


def test_add_rejects_an_end_before_the_start(store):
    out = make(store).run("agenda_add", {"items": [
        {"title": "Vardiya", "branch": "kisisel", "start": "2026-11-14T22:00",
         "due": "2026-11-14T08:00"}]})
    assert "1. error" in out and "next day's date" in out
    assert store.list(OWNER) == []


def _add_one(reg, **fields):
    item = {"title": "Vardiya", "branch": "kisisel", "start": "2026-11-14T22:00",
            "due": "2026-11-15T08:00", **fields}
    out = reg.run("agenda_add", {"items": [item]})
    return out.split("id=")[1].split()[0]


def test_update_changes_times_in_place_and_keeps_status_and_event(store):
    changes = []
    store.on_change = changes.append
    reg = make(store)
    item_id = _add_one(reg)
    store.set_event_id(OWNER, item_id, "evt1")
    changes.clear()
    out = reg.run("agenda_update", {"id": item_id, "start": "2026-11-15T22:00",
                                    "due": "2026-11-16T08:00", "title": "İş vardiyası"})
    assert out.startswith(f"updated id={item_id}")
    assert "2026-11-15 Sun 22:00 - 2026-11-16 Mon 08:00" in out
    assert "Google Calendar event" in out
    [item] = store.list(OWNER)
    assert item["id"] == item_id and item["title"] == "İş vardiyası"
    assert item["start"] == "2026-11-15T22:00+03:00" and item["due"] == "2026-11-16T08:00+03:00"
    assert item["status"] == "added" and item["calendar_event_id"] == "evt1"
    assert changes == [OWNER]  # the Google Calendar sync is asked to follow


def test_update_one_field_and_clear_the_start(store):
    reg = make(store)
    item_id = _add_one(reg)
    reg.run("agenda_update", {"id": item_id, "title": "Gece işi"})
    item = store.get(OWNER, item_id)
    assert item["title"] == "Gece işi" and item["start"] == "2026-11-14T22:00+03:00"
    reg.run("agenda_update", {"id": item_id, "start": "", "due": "2026-11-20"})
    item = store.get(OWNER, item_id)
    assert item["start"] is None and item["due"] == "2026-11-20" and item["all_day"] is True


def test_update_rejects_bad_input_and_other_owners_and_synced_items(store):
    reg = make(store)
    item_id = _add_one(reg)
    for args in ({"id": item_id}, {"id": "", "title": "x"}, {"title": "x"},
                 {"id": item_id, "due": "2026-11-14T21:00"}, {"id": "missing", "title": "x"},
                 {"id": item_id, "start": "", "due": ""}):
        with pytest.raises(ToolError):
            reg.run("agenda_update", args)
    other, _ = store.upsert(OTHER, {"branch": "okul", "title": "Başkası", "due": "2026-11-15",
                                    "source": "manual", "status": "added"})
    synced, _ = store.upsert(OWNER, {"branch": "okul", "title": "Moodle ödevi", "due": "2026-11-15",
                                     "source": "moodle", "source_ref": "uid-1", "status": "added"})
    for target in (other["id"], synced["id"]):
        with pytest.raises(ToolError):
            reg.run("agenda_update", {"id": target, "title": "değişti"})
    assert store.get(OTHER, other["id"])["title"] == "Başkası"
    assert store.get(OWNER, synced["id"])["title"] == "Moodle ödevi"
    assert store.get(OWNER, item_id)["due"] == "2026-11-15T08:00+03:00"


def test_add_many_items_in_one_call_as_added_manual_entries(store):
    reg = make(store)
    out = reg.run("agenda_add", {"items": [
        {"title": "Matematik kontrol work", "branch": "okul", "kind": "exam", "due": "2026-11-19"},
        {"title": "Çalış: Vektörler", "branch": "okul", "kind": "other",
         "start": "2026-11-12T18:00", "due": "2026-11-12T20:00"},
    ]})
    assert "1. added" in out and "2. added" in out
    items = store.list(OWNER)
    assert {i["title"] for i in items} == {"Matematik kontrol work", "Çalış: Vektörler"}
    assert all(i["status"] == "added" and i["source"] == "manual" for i in items)
    study = next(i for i in items if i["title"].startswith("Çalış"))
    # a time without an offset is read in the local zone of now()
    assert study["start"] == "2026-11-12T18:00+03:00" and study["due"] == "2026-11-12T20:00+03:00"
    assert "2026-11-12 Thu 18:00-20:00" in out
    assert "Google calendars" in out


def test_add_single_item_by_fields_and_kind_guessed_from_title(store):
    out = make(store).run("agenda_add", {"title": "Fizik sınavı", "branch": "okul",
                                         "due": "2026-11-20"})
    assert "added" in out and "(all day)" in out
    assert store.list(OWNER)[0]["kind"] == "exam"


def test_add_reports_a_bad_item_and_keeps_the_good_ones(store):
    out = make(store).run("agenda_add", {"items": [
        {"title": "İyi", "branch": "okul", "due": "2026-11-19"},
        {"title": "Kötü", "branch": "uzay", "due": "2026-11-19"},
        {"title": "Tarihsiz", "branch": "okul"},
    ]})
    assert "1. added" in out and "2. error" in out and "3. error" in out
    assert [i["title"] for i in store.list(OWNER)] == ["İyi"]


def test_add_limits(store):
    reg = make(store)
    with pytest.raises(ToolError):
        reg.run("agenda_add", {})
    with pytest.raises(ToolError):
        reg.run("agenda_add", {"items": [{"title": str(n), "branch": "okul", "due": "2026-11-19"}
                                         for n in range(21)]})
    with pytest.raises(ToolError):
        reg.run("agenda_add", {"items": [], "title": "x"})


def test_free_slots_subtracts_google_events_and_agenda_items(store):
    store.upsert(OWNER, {"branch": "okul", "kind": "lab", "title": "Lab",
                         "start": "2026-11-12T14:00:00+03:00", "due": "2026-11-12T16:00:00+03:00",
                         "source": "manual", "status": "added"})
    events = [
        {"title": "Ders", "start": "2026-11-12T09:00:00+03:00", "end": "2026-11-12T12:00:00+03:00",
         "all_day": False},
        {"title": "Tatil", "start": "2026-11-13", "end": "2026-11-14", "all_day": True},
    ]
    calls = []
    out = make(store, events=events, calls=calls).run(
        "free_slots", {"start_date": "2026-11-12", "end_date": "2026-11-13", "minutes": 90})
    lines = out.splitlines()
    assert lines[0] == "2026-11-12 Thu: 12:00-14:00, 16:00-22:00"
    assert lines[1] == "2026-11-13 Fri: 09:00-22:00"
    assert "All-day events" in out and "Tatil" in out
    begin, end = calls[0]
    assert begin == datetime(2026, 11, 12, tzinfo=TR).timestamp()
    assert end == datetime(2026, 11, 14, tzinfo=TR).timestamp()


def test_free_slots_today_starts_now_and_short_gaps_are_dropped(store):
    events = [{"title": "A", "start": "2026-11-11T11:00:00+03:00",
               "end": "2026-11-11T21:30:00+03:00", "all_day": False}]
    out = make(store, events=events).run("free_slots", {"start_date": "2026-11-11",
                                                        "end_date": "2026-11-11", "minutes": 60})
    assert out.splitlines()[0] == "2026-11-11 Wed: 10:00-11:00"


def test_free_slots_says_when_google_was_not_read(store):
    out = make(store, status="auth_down").run("free_slots", {"start_date": "2026-11-12",
                                                             "end_date": "2026-11-12"})
    assert "could not be read (status: auth_down)" in out
    reg = ToolRegistry()
    register_agenda_tools(reg, store, OWNER, now=lambda: NOW)
    assert "not connected" in reg.run("free_slots", {"start_date": "2026-11-12",
                                                     "end_date": "2026-11-12"})


def test_free_slots_rejects_bad_arguments(store):
    reg = make(store)
    for args in ({"start_date": "19.11"}, {"start_date": "2026-11-20", "end_date": "2026-11-19"},
                 {"start_date": "2026-11-01", "end_date": "2026-12-31"}, {"minutes": 5},
                 {"day_start": "22:00", "day_end": "09:00"}, {"day_start": "nine"}):
        with pytest.raises(ToolError):
            reg.run("free_slots", args)


def test_list_and_remove_stay_with_the_owner(store):
    reg = make(store)
    reg.run("agenda_add", {"title": "Benim", "branch": "kisisel", "due": "2026-11-15"})
    store.upsert(OTHER, {"branch": "okul", "title": "Başkası", "due": "2026-11-15",
                         "source": "manual", "status": "added"})
    listed = reg.run("agenda_list", {"start_date": "2026-11-11", "end_date": "2026-11-20"})
    assert "Benim" in listed and "Başkası" not in listed
    item_id = store.list(OWNER)[0]["id"]
    assert reg.run("agenda_remove", {"id": item_id}) == f"removed {item_id}"
    assert reg.run("agenda_list", {}) == "no items"
    other_id = store.list(OTHER)[0]["id"]
    with pytest.raises(ToolError):
        reg.run("agenda_remove", {"id": other_id})
