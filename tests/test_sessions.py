import pytest

from igris.sessions import SessionError, SessionManager


class Clock:
    def __init__(self, t=100.0):
        self.t = t

    def __call__(self):
        self.t += 1.0
        return self.t


@pytest.fixture
def mgr(tmp_path):
    return SessionManager(tmp_path / "sessions", clock=Clock())


def test_open_creates_nothing(mgr, tmp_path):
    mgr.open("a")
    assert not (tmp_path / "sessions" / "a.jsonl").exists()
    assert mgr.list() == []


def test_default_active(mgr):
    name, mem = mgr.current()
    assert name == "default"
    assert len(mem) == 0


def test_persist_across_instances(tmp_path):
    root = tmp_path / "s"
    m1 = SessionManager(root, clock=Clock())
    m1.switch("work").append("user", "hello")
    m2 = SessionManager(root, clock=Clock())
    name, mem = m2.current()
    assert name == "work"
    assert [e["content"] for e in mem.history()] == ["hello"]


@pytest.mark.parametrize("bad", ["", "..", "a/b", "a.b", "x" * 41, "a b", "../x"])
def test_invalid_names(mgr, bad):
    with pytest.raises(SessionError):
        mgr.open(bad)
    with pytest.raises(SessionError):
        mgr.switch(bad)
    with pytest.raises(SessionError):
        mgr.delete(bad)
    with pytest.raises(SessionError):
        mgr.rename(bad, "ok")


def test_max_length_ok(mgr):
    mgr.open("x" * 40)


def test_invalid_active_file_falls_back(mgr):
    mgr.root.mkdir(parents=True)
    (mgr.root / "active.txt").write_text("../evil")
    assert mgr.current()[0] == "default"


def test_list_ordering_and_counts(mgr):
    mgr.open("a").append("user", "1")
    mgr.open("b").append("user", "1")
    mgr.open("b").append("assistant", "2")
    mgr.open("a").append("user", "3")
    items = mgr.list()
    assert [i["name"] for i in items] == ["a", "b"] or items[0]["updated"] >= items[1]["updated"]
    assert items[0]["name"] == "a"
    assert {i["name"]: i["messages"] for i in items} == {"a": 2, "b": 2}
    assert items[0]["updated"] > items[1]["updated"]


def test_list_empty_file_updated_zero(mgr):
    mgr.root.mkdir(parents=True)
    (mgr.root / "e.jsonl").write_text("")
    assert mgr.list() == [{"name": "e", "messages": 0, "updated": 0.0}]


def test_delete_missing(mgr):
    with pytest.raises(SessionError):
        mgr.delete("nope")


def test_delete_active_switches_to_default(mgr):
    mgr.switch("a").append("user", "x")
    mgr.delete("a")
    assert mgr.current()[0] == "default"
    assert mgr.list() == []


def test_delete_inactive_keeps_active(mgr):
    mgr.open("a").append("user", "x")
    mgr.switch("b")
    mgr.delete("a")
    assert mgr.current()[0] == "b"


def test_rename_basic(mgr):
    mgr.open("a").append("user", "x")
    mgr.rename("a", "b")
    assert [i["name"] for i in mgr.list()] == ["b"]
    assert len(mgr.open("b")) == 1


def test_rename_active_updates_pointer(mgr):
    mgr.switch("a").append("user", "x")
    mgr.rename("a", "b")
    assert mgr.current()[0] == "b"


def test_rename_inactive_keeps_pointer(mgr):
    mgr.open("a").append("user", "x")
    mgr.switch("c")
    mgr.rename("a", "b")
    assert mgr.current()[0] == "c"


def test_rename_to_existing_fails(mgr):
    mgr.open("a").append("user", "x")
    mgr.open("b").append("user", "y")
    with pytest.raises(SessionError):
        mgr.rename("a", "b")
    assert len(mgr.open("b")) == 1
    assert len(mgr.open("a")) == 1


def test_rename_missing_fails(mgr):
    with pytest.raises(SessionError):
        mgr.rename("a", "b")
