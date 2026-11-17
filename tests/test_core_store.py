import sqlite3

import pytest

from igris.core_store import CoreStore


def test_round_trip_and_persistence(tmp_path):
    path = tmp_path / "core.db"
    store = CoreStore(path)
    doc_id = store.add("alice", "Vectors", "upload", "mixed", "Topics: vectors",
                       ["first", "second"], file_id="file-1", from_image=[False, True])
    doc = CoreStore(path).get("alice", doc_id)
    assert doc["id"] == doc_id
    assert doc["title"] == "Vectors"
    assert doc["source"] == "upload" and doc["read_as"] == "mixed"
    assert doc["file_id"] == "file-1" and doc["urls"] == []
    assert doc["card"] == "Topics: vectors"
    assert doc["pages"] == [{"n": 1, "text": "first", "from_image": False},
                            {"n": 2, "text": "second", "from_image": True}]
    assert store.list("alice")[0]["id"] == doc_id
    store.delete("alice", doc_id)
    with pytest.raises(KeyError):
        store.get("alice", doc_id)


def test_owner_isolation_on_every_operation(tmp_path):
    store = CoreStore(tmp_path / "core.db")
    doc_id = store.add("bob", "Private", "research", "text", "secret", ["secret"])
    assert store.list("alice") == []
    assert store.search("alice", "secret") == []
    for operation in (lambda: store.get("alice", doc_id),
                      lambda: store.set_page("alice", doc_id, 1, "replacement"),
                      lambda: store.delete("alice", doc_id)):
        with pytest.raises(KeyError):
            operation()
    assert store.get("bob", doc_id)["pages"][0]["text"] == "secret"


def test_search_locations_excerpt_and_edits(tmp_path):
    store = CoreStore(tmp_path / "core.db")
    doc_id = store.add("a", "Course", "research", "text", "Vector definitions",
                       ["Introduction", "x " * 400 + "vector projection"],
                       urls=["https://example.org/notes"])
    hits = store.search("a", "VECTOR")
    assert {(h["doc_id"], h["where"]) for h in hits} == {(doc_id, "card"), (doc_id, 2)}
    assert all(h["title"] == "Course" and len(h["excerpt"]) <= 300 for h in hits)
    assert all("vector" in h["excerpt"].lower() for h in hits)
    assert len(store.search("a", "vector", limit=1)) == 1
    assert store.search("a", "") == []
    assert store.search("a", "vector", limit=0) == []
    store.set_page("a", doc_id, 2, "determinant")
    assert [h["where"] for h in store.search("a", "vector")] == ["card"]
    assert store.search("a", "determinant")[0]["where"] == 2
    with pytest.raises(KeyError):
        store.set_page("a", doc_id, 3, "not a page")


@pytest.mark.parametrize("source,read_as,flags", [
    ("invalid", "text", None), ("upload", "invalid", None),
    ("upload", "image", []), ("upload", "image", ["yes"]),
])
def test_invalid_metadata(tmp_path, source, read_as, flags):
    store = CoreStore(tmp_path / "core.db")
    with pytest.raises(ValueError):
        store.add("a", "Title", source, read_as, "", ["page"], from_image=flags)


def test_substring_fallback_without_fts5(tmp_path, monkeypatch):
    connect = sqlite3.connect

    class NoFTS(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if "CREATE VIRTUAL TABLE" in sql.upper():
                raise sqlite3.OperationalError("no such module: fts5")
            return super().execute(sql, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: connect(*a, factory=NoFTS, **kw))
    store = CoreStore(tmp_path / "fallback.db")
    doc_id = store.add("a", "Fallback", "upload", "text", "", ["Eigenvector"])
    assert store.search("a", "eigenvector")[0]["doc_id"] == doc_id
    store.set_page("a", doc_id, 1, "replaced")
    assert store.search("a", "eigenvector") == []


def test_reserved_upload_is_finished_atomically_with_stable_id(tmp_path):
    from igris.core_store import CoreStore

    store = CoreStore(tmp_path / "core.sqlite3")
    identifier = store.reserve_upload("owner", "notes", "upload-id")
    assert store.get("owner", identifier)["read_as"] == "reading"
    with pytest.raises(KeyError):
        store.complete_upload("other", identifier, "text", "card", ["notes"])
    store.complete_upload("owner", identifier, "image", "card", ["notes"], from_image=[True])
    doc = store.get("owner", identifier)
    assert doc["id"] == identifier and doc["file_id"] == "upload-id"
    assert doc["pages"][0]["from_image"] is True
    assert store.search("owner", "notes")[0]["doc_id"] == identifier
    store.delete("owner", identifier)
    with pytest.raises(KeyError):
        store.complete_upload("owner", identifier, "text", "card", ["notes"])


def test_restart_only_fails_this_owners_pending_reads(tmp_path):
    store = CoreStore(tmp_path / "core.db")
    pending = store.reserve_upload("a", "notes", "upload")
    foreign = store.reserve_upload("b", "secret", "other")
    read = store.add("a", "done", "upload", "text", "card", ["text"])
    store.fail_pending("a", "Interrupted")
    assert store.get("a", pending)["read_as"] == "failed"
    assert store.get("a", pending)["error"] == "Interrupted"
    assert store.get("b", foreign)["read_as"] == "reading"
    assert store.get("a", read)["read_as"] == "text"
