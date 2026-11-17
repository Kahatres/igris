import pytest

from igris.notices import BODY_LIMIT, KINDS, TITLE_LIMIT, NoticeStore, safe_add


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        self.now += 1
        return self.now


@pytest.fixture
def store(tmp_path):
    return NoticeStore(tmp_path / "n" / "notices.sqlite3", clock=Clock())


def test_add_returns_the_public_row(store):
    item = store.add("o", "approval", "Onay", "metin", source="approvals", ref="r1",
                     action={"approval": "r1"})
    assert item["owner"] == "o" and item["kind"] == "approval" and item["title"] == "Onay"
    assert item["body"] == "metin" and item["source"] == "approvals" and item["ref"] == "r1"
    assert item["action"] == {"approval": "r1"} and item["read_at"] is None
    assert len(item["id"]) == 16


def test_add_with_ref_is_idempotent_and_without_ref_is_not(store):
    first = store.add("o", "job_done", "Bitti", ref="j1")
    again = store.add("o", "job_done", "Başka başlık", ref="j1")
    assert again == first
    assert store.add("o", "job_failed", "Hata", ref="j1")["id"] != first["id"]
    assert store.add("x", "job_done", "Bitti", ref="j1")["id"] != first["id"]
    store.add("o", "reminder", "Bir")
    store.add("o", "reminder", "Bir")
    assert len(store.list("o")) == 4


def test_unknown_kind_bad_owner_and_bad_action_raise(store):
    with pytest.raises(ValueError):
        store.add("o", "nope", "x")
    with pytest.raises(ValueError):
        store.add("", "reminder", "x")
    with pytest.raises(ValueError):
        store.add("o", "reminder", "x", action=["not", "a", "dict"])
    assert "models" in KINDS and len(KINDS) == 11


def test_text_is_cut_and_cleaned(store):
    item = store.add("o", "reminder", "a\x00b\tc\n" + "t" * 500, "satır 1\nsatır\x07 2" + "b" * 5000)
    assert item["title"].startswith("a b c t") and len(item["title"]) == TITLE_LIMIT
    assert "\n" not in item["title"] and "\x00" not in item["title"]
    assert item["body"].startswith("satır 1\nsatır  2") and len(item["body"]) == BODY_LIMIT


def test_list_orders_unread_first_then_newest_and_limits(store):
    a = store.add("o", "reminder", "a")
    b = store.add("o", "reminder", "b")
    c = store.add("o", "reminder", "c")
    store.mark_read("o", [c["id"]])
    assert [n["title"] for n in store.list("o")] == ["b", "a", "c"]
    assert [n["title"] for n in store.list("o", unread_only=True)] == ["b", "a"]
    assert [n["title"] for n in store.list("o", limit=1)] == ["b"]
    assert {a["id"], b["id"]} == {n["id"] for n in store.list("o", unread_only=True)}


def test_mark_read_counts_and_ignores_others(store):
    a = store.add("o", "reminder", "a")
    b = store.add("o", "reminder", "b")
    other = store.add("x", "reminder", "z")
    assert store.mark_read("o", [a["id"], a["id"], other["id"], 5]) == 1
    assert store.mark_read("o", [a["id"]]) == 0
    assert store.mark_read("o", []) == 0
    assert store.unread_count("o") == 1 and store.unread_count("x") == 1
    assert store.mark_all_read("o") == 1 and store.mark_all_read("o") == 0
    assert store.unread_count("o") == 0
    assert store.list("x")[0]["id"] == other["id"] and b["id"]


def test_owners_are_isolated(store):
    store.add("o", "reminder", "mine")
    store.add("x", "reminder", "theirs")
    assert [n["title"] for n in store.list("o")] == ["mine"]
    assert [n["title"] for n in store.list("x")] == ["theirs"]


def test_store_survives_reopen(tmp_path):
    path = tmp_path / "notices.sqlite3"
    NoticeStore(path).add("o", "connection", "Gmail bağlantısı koptu", ref="gmail")
    again = NoticeStore(path)
    assert again.unread_count("o") == 1
    again.add("o", "connection", "Gmail bağlantısı koptu", ref="gmail")
    assert again.unread_count("o") == 1


def test_safe_add_swallows_errors_and_resolves_callable_owner(store):
    safe_add(None, "o", "reminder", "x")
    safe_add(store, None, "reminder", "x")
    safe_add(store, "o", "bad-kind", "x")
    safe_add(store, lambda: None, "reminder", "x")
    safe_add(store, lambda: "o", "reminder", "ok", ref="1")
    assert [n["title"] for n in store.list("o")] == ["ok"]
