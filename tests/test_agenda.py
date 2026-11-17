"""AgendaStore: the task calendar's items, on a temporary SQLite file."""
from contextlib import closing
import sys
from datetime import datetime, timedelta, timezone

import pytest

from igris.agenda import AgendaError, AgendaMissing, AgendaStore

OWNER = "a" * 32
OTHER = "b" * 32
TR = timezone(timedelta(hours=3))


@pytest.fixture
def store(tmp_path):
    return AgendaStore(tmp_path / "agenda.sqlite3", clock=lambda: 1000.0)


def item(**fields):
    base = {"branch": "okul", "kind": "assignment", "title": "Ödev 1",
            "due": "2026-11-12T23:59:00+03:00", "source": "moodle", "source_ref": "uid-1"}
    base.update(fields)
    return base


def test_upsert_creates_then_updates_the_same_source_item(store):
    first, created = store.upsert(OWNER, item())
    assert created and first["title"] == "Ödev 1" and first["status"] == "proposed"
    store.set_status(OWNER, first["id"], "added")
    again, created = store.upsert(OWNER, item(title="Ödev 1 (uzatıldı)", due="2026-11-14T23:59:00+03:00",
                                             location="Moodle", kind="lab", status="dismissed",
                                             calendar_event_id="ignored"))
    assert not created and again["id"] == first["id"]
    assert again["title"] == "Ödev 1 (uzatıldı)" and again["due"] == "2026-11-14T23:59:00+03:00"
    assert again["location"] == "Moodle" and again["kind"] == "lab"
    assert again["status"] == "added" and again["calendar_event_id"] is None
    assert len(store.list(OWNER)) == 1


def test_same_ref_in_another_source_or_owner_is_a_new_item(store):
    store.upsert(OWNER, item())
    assert store.upsert(OWNER, item(source="gmail"))[1]
    assert store.upsert(OTHER, item())[1]
    assert len(store.list(OWNER)) == 2 and len(store.list(OTHER)) == 1


def test_items_without_source_ref_never_merge(store):
    store.upsert(OWNER, item(source="manual", source_ref=""))
    store.upsert(OWNER, item(source="manual", source_ref=""))
    assert len(store.list(OWNER)) == 2


def test_study_kind_is_stored_and_owner_scoped(store):
    saved, _ = store.upsert(OWNER, item(kind="study", source="manual", source_ref="study:step1"))
    assert saved["kind"] == "study" and saved["branch"] == "okul"
    assert store.get(OWNER, saved["id"])["kind"] == "study"
    with pytest.raises(AgendaMissing):
        store.get(OTHER, saved["id"])


@pytest.mark.parametrize("bad", [
    {"branch": "is"}, {"kind": "party"}, {"source": "fax"}, {"status": "maybe"},
    {"due": None, "start": None}, {"due": "yarın"}, {"due": "2026-11-12T10:00:00"},
    {"title": "  "}, {"title": 5}, {"confidence": 2}, {"mandatory": "yes"},
])
def test_invalid_fields_are_rejected(store, bad):
    with pytest.raises(AgendaError):
        store.upsert(OWNER, item(**bad))


def test_dates_all_day_and_cleaned_text(store):
    saved, _ = store.upsert(OWNER, item(due="2026-11-12", title="Sınav\x00 \n hazırlık" + "x" * 400))
    assert saved["all_day"] is True and saved["due"] == "2026-11-12"
    assert "\x00" not in saved["title"] and "\n" not in saved["title"] and len(saved["title"]) == 200
    timed, _ = store.upsert(OWNER, item(source_ref="uid-2", start="2026-11-12T09:00:00+03:00", due=None))
    assert timed["all_day"] is False and timed["start"] == "2026-11-12T09:00:00+03:00"
    assert timed["mandatory"] is False and timed["confidence"] == 1.0


def test_get_status_delete_and_ownership(store):
    manual, _ = store.upsert(OWNER, item(source="manual", source_ref=""))
    moodle, _ = store.upsert(OWNER, item())
    assert store.get(OWNER, manual["id"])["id"] == manual["id"]
    with pytest.raises(AgendaMissing):
        store.get(OTHER, manual["id"])
    with pytest.raises(AgendaMissing):
        store.set_status(OTHER, manual["id"], "done")
    with pytest.raises(AgendaError):
        store.set_status(OWNER, manual["id"], "gone")
    assert store.set_status(OWNER, manual["id"], "done")["status"] == "done"
    with pytest.raises(AgendaError):
        store.delete(OWNER, moodle["id"])  # synced items are dismissed, not deleted
    with pytest.raises(AgendaMissing):
        store.delete(OTHER, manual["id"])
    store.delete(OWNER, manual["id"])
    with pytest.raises(AgendaMissing):
        store.get(OWNER, manual["id"])


def test_list_sorts_by_due_else_start_and_hides_done(store):
    a, _ = store.upsert(OWNER, item(source_ref="a", due="2026-11-20"))
    b, _ = store.upsert(OWNER, item(source_ref="b", due=None, start="2026-11-11T08:00:00+03:00"))
    c, _ = store.upsert(OWNER, item(source_ref="c", due="2026-11-15T10:00:00+00:00", branch="esn"))
    d, _ = store.upsert(OWNER, item(source_ref="d", due="2026-11-12"))
    store.set_status(OWNER, d["id"], "done")
    assert [x["id"] for x in store.list(OWNER)] == [b["id"], c["id"], a["id"]]
    assert [x["id"] for x in store.list(OWNER, include_done=True)] == [b["id"], d["id"], c["id"], a["id"]]
    assert [x["id"] for x in store.list(OWNER, branch="esn")] == [c["id"]]
    with pytest.raises(AgendaError):
        store.list(OWNER, branch="nope")


def test_sections_across_midnight_and_week_boundaries(store):
    # Tuesday 10 November 2026, 23:30 in Istanbul (UTC+3); the week ends Sunday 15 November.
    now = datetime(2026, 11, 10, 23, 30, tzinfo=TR)
    refs = {
        "past_hour": "2026-11-10T22:00:00+03:00",
        "yesterday": "2026-11-09",
        "tonight": "2026-11-10T23:45:00+03:00",
        "today_all_day": "2026-11-10",
        "after_midnight_utc": "2026-11-10T21:10:00+00:00",  # 00:10 on Wednesday in Istanbul
        "sunday_late": "2026-11-15T23:59:00+03:00",
        "monday": "2026-11-16T00:00:00+03:00",
        "next_month": "2026-12-01",
    }
    for ref, due in refs.items():
        store.upsert(OWNER, item(source_ref=ref, title=ref, due=due))
    done, _ = store.upsert(OWNER, item(source_ref="done", title="done", due="2026-11-09"))
    store.set_status(OWNER, done["id"], "done")
    store.upsert(OWNER, item(source_ref="esn", title="esn", branch="esn", due="2026-11-10"))
    got = store.sections(OWNER, "okul", now)
    titles = {k: [x["title"] for x in v] for k, v in got.items()}
    assert titles == {"overdue": ["yesterday", "past_hour"],
                      "today": ["today_all_day", "tonight"],
                      "week": ["after_midnight_utc", "sunday_late"],
                      "later": ["monday", "next_month"]}
    with pytest.raises(AgendaError):
        store.sections(OWNER, "nope", now)


def test_sections_need_an_aware_now(store):
    with pytest.raises(AgendaError):
        store.sections(OWNER, "okul", datetime(2026, 11, 10, 12, 0))


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "agenda.sqlite3"
    saved, _ = AgendaStore(path).upsert(OWNER, item())
    assert AgendaStore(path).get(OWNER, saved["id"])["title"] == "Ödev 1"


def test_between_returns_the_window_with_done_items_but_not_dismissed_ones(store):
    tz = timezone(timedelta(hours=2))
    inside = store.upsert(OWNER, item(source_ref="a", due="2026-11-12T10:00:00+02:00"))[0]
    done = store.upsert(OWNER, item(source_ref="b", due="2026-11-30"))[0]
    gone = store.upsert(OWNER, item(source_ref="c", due="2026-11-13T10:00:00+02:00"))[0]
    store.upsert(OWNER, item(source_ref="d", due="2026-12-01T00:00:00+02:00"))
    store.upsert("someone-else", item(source_ref="a", due="2026-11-12T10:00:00+02:00"))
    store.set_status(OWNER, done["id"], "done")
    store.set_status(OWNER, gone["id"], "dismissed")
    window = store.between(OWNER, datetime(2026, 11, 1, tzinfo=tz), datetime(2026, 12, 1, tzinfo=tz))
    assert [i["id"] for i in window] == [inside["id"], done["id"]]
    with pytest.raises(AgendaError):
        store.between(OWNER, datetime(2026, 11, 1), datetime(2026, 12, 1))


@pytest.mark.parametrize("offset", [-5, 0, 3])
def test_between_matches_old_filter_for_mixed_dates_and_offsets(store, offset, monkeypatch):
    from igris.agenda import _moment

    tz = timezone(timedelta(hours=offset))
    start = datetime(2026, 11, 12, 0, 0, 0, 1, tzinfo=tz)
    end = datetime(2026, 11, 14, tzinfo=timezone(timedelta(hours=2)))
    dates = ["2026-11-11", "2026-11-12", "2026-11-13", "2026-11-14",
             "2026-11-12T01:00:00+14:00", "2026-11-11T23:59:59-12:00",
             "2026-11-13T23:00:00-05:00", "2026-11-14T01:00:00+09:00",
             start.isoformat(), (start - timedelta(microseconds=1)).isoformat(),
             (end - timedelta(microseconds=1)).isoformat(), end.isoformat(),
             "2026-11-13T12:00:00+03:00:01.123456"]
    if sys.version_info >= (3, 11):  # the basic ISO form needs 3.11's fromisoformat
        dates.append("20261113T120000+0300")
    for i, value in enumerate(dates):
        for owner in (OWNER, OTHER):
            store.upsert(owner, item(source_ref=str(i), due=value if i % 2 else None,
                                     start="2026-10-01" if i % 2 else value,
                                     branch="esn" if i % 3 else "okul",
                                     status=("proposed", "added", "done", "dismissed")[i % 4]))
    old = [it for it in store.list(OWNER, include_done=True)
           if it["status"] != "dismissed"
           and start <= _moment(it["due"] or it["start"], tz) < end]
    converted = []
    original_row = store._row

    def row(value):
        converted.append(value["id"])
        return original_row(value)

    monkeypatch.setattr(store, "_row", row)
    monkeypatch.setattr(store, "list", lambda *a, **kw: pytest.fail("must filter in SQL"))
    assert store.between(OWNER, start, end) == old
    assert len(converted) == len(old)  # only matching rows leave SQLite
    assert store.between(OWNER, end, start) == []


def test_owner_due_index_is_added_when_reopening_an_existing_store(store):
    def index_columns(db):
        return [tuple(row["name"] for row in db.execute(f"PRAGMA index_info('{idx['name']}')"))
                for idx in db.execute("PRAGMA index_list('items')")]

    with closing(store._connect()) as db, db:
        assert ("owner", "due") in index_columns(db)
        db.execute("DROP INDEX items_owner_due")
    reopened = AgendaStore(store.path)
    with closing(reopened._connect()) as db:
        assert ("owner", "due") in index_columns(db)


# ---- batches of manual items (timetable import) ----

def lesson(ref, title="Ders", start="2026-11-16T10:00+02:00"):
    return {"branch": "okul", "title": title, "start": start, "due": "2026-11-16T11:30+02:00",
            "source": "manual", "source_ref": f"import:b:{ref}", "status": "added"}


def test_replace_refs_inserts_refreshes_and_removes_in_one_go(store):
    changes = []
    store.on_change = changes.append
    store.upsert(OWNER, item())  # another source is never touched
    assert store.replace_refs(OWNER, "manual", "import:b:", [lesson(1), lesson(2)]) == {
        "created": 2, "updated": 0, "unchanged": 0, "removed": 0, "kept_done": 0, "kept": 0}
    first = {i["source_ref"]: i["id"] for i in store.list(OWNER) if i["source"] == "manual"}
    store.set_status(OWNER, first["import:b:1"], "done")
    counts = store.replace_refs(OWNER, "manual", "import:b:", [lesson(1, "Yeni"), lesson(3)])
    assert counts == {"created": 1, "updated": 1, "unchanged": 0, "removed": 1,
                      "kept_done": 0, "kept": 0}
    after = {i["source_ref"]: i for i in store.list(OWNER, include_done=True)}
    assert after["import:b:1"]["id"] == first["import:b:1"]
    assert after["import:b:1"]["status"] == "done" and after["import:b:1"]["title"] == "Yeni"
    assert "import:b:2" not in after and "uid-1" in after
    assert len(changes) == 4  # upsert, set_status and two batches: one notice each
    again = store.replace_refs(OWNER, "manual", "import:b:", [lesson(1, "Yeni"), lesson(3)])
    assert again["unchanged"] == 2 and len(changes) == 4  # nothing changed: no sync request


def test_replace_refs_writes_nothing_when_one_item_is_invalid(store):
    with pytest.raises(AgendaError, match="item 2: title must not be empty"):
        store.replace_refs(OWNER, "manual", "import:b:", [lesson(1), lesson(2, title="")])
    with pytest.raises(AgendaError, match="item 1: source_ref must start with import:b:"):
        store.replace_refs(OWNER, "manual", "import:b:", [dict(lesson(1), source_ref="x")])
    with pytest.raises(AgendaError, match="duplicate"):
        store.replace_refs(OWNER, "manual", "import:b:", [lesson(1), lesson(1)])
    assert store.list(OWNER) == []


def test_delete_refs_removes_a_batch_only(store):
    store.replace_refs(OWNER, "manual", "import:b:", [lesson(1), lesson(2)])
    store.replace_refs(OWNER, "manual", "import:bb:", [dict(lesson(1), source_ref="import:bb:1")])
    store.replace_refs(OTHER, "manual", "import:b:", [lesson(1)])
    assert store.delete_refs(OWNER, "manual", "import:b:") == {"removed": 2, "kept_done": 0}
    assert [i["source_ref"] for i in store.list(OWNER)] == ["import:bb:1"]
    assert len(store.list(OTHER)) == 1
    assert store.delete_refs(OWNER, "manual", "import:b:")["removed"] == 0


def test_an_imported_lesson_that_is_over_is_not_overdue_but_an_imported_exam_is(store):
    store.replace_refs(OWNER, "manual", "import:b:", [
        dict(lesson(1), kind="lesson"), dict(lesson(2), kind="exam"), lesson(3)])
    store.upsert(OWNER, dict(lesson(4), kind="lesson", source_ref="typed-by-hand"))
    late = datetime(2026, 11, 16, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    overdue = sorted(i["source_ref"] for i in store.sections(OWNER, "okul", late)["overdue"])
    # the exam and the "other" item stay late until done; a lesson typed by hand too
    assert overdue == ["import:b:2", "import:b:3", "typed-by-hand"]


def test_replace_refs_keeps_done_items_and_can_keep_every_missing_one(store):
    store.replace_refs(OWNER, "manual", "import:b:", [lesson(1), lesson(2), lesson(3)])
    ids = {i["source_ref"]: i["id"] for i in store.list(OWNER)}
    store.set_status(OWNER, ids["import:b:2"], "done")
    counts = store.replace_refs(OWNER, "manual", "import:b:", [lesson(1)], remove_missing=False)
    assert (counts["removed"], counts["kept"], counts["kept_done"]) == (0, 1, 1)
    counts = store.replace_refs(OWNER, "manual", "import:b:", [lesson(1)])
    assert (counts["removed"], counts["kept"], counts["kept_done"]) == (1, 0, 1)
    refs = {i["source_ref"] for i in store.list(OWNER, include_done=True)}
    assert refs == {"import:b:1", "import:b:2"}


def test_delete_refs_keeps_done_items_unless_asked(store):
    store.replace_refs(OWNER, "manual", "import:b:", [lesson(1), lesson(2)])
    done = next(i for i in store.list(OWNER) if i["source_ref"] == "import:b:2")
    store.set_status(OWNER, done["id"], "done")
    assert store.delete_refs(OWNER, "manual", "import:b:") == {"removed": 1, "kept_done": 1}
    assert store.delete_refs(OWNER, "manual", "import:b:", include_done=True) == {
        "removed": 1, "kept_done": 0}
    assert store.list(OWNER, include_done=True) == []
