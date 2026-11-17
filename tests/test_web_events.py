"""Cached change detection for the event stream: real SQLite files, no server."""
import sqlite3
from contextlib import closing
from types import SimpleNamespace

from igris.events import ChangeWatcher
from igris.web_events import DataVersions, SourceCache, digest


def make_db(path):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE items (name TEXT)")


def write(path, name):
    """A store-like write: a fresh connection per commit."""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("INSERT INTO items VALUES (?)", (name,))


def rows(path):
    with closing(sqlite3.connect(path)) as db:
        return [r[0] for r in db.execute("SELECT name FROM items ORDER BY rowid")]


def test_data_version_moves_only_on_commits(tmp_path):
    path = tmp_path / "a.sqlite3"
    make_db(path)
    versions = DataVersions()
    first = versions.version(path)
    assert first is not None and versions.version(path) == first
    rows(path)  # reads do not count
    assert versions.version(path) == first
    write(path, "x")
    second = versions.version(path)
    assert second != first and versions.version(path) == second
    versions.close()


def test_missing_file_has_no_version(tmp_path):
    assert DataVersions().version(tmp_path / "none.sqlite3") is None


def test_source_is_rebuilt_only_after_a_commit(tmp_path):
    path = tmp_path / "a.sqlite3"
    make_db(path)
    store = SimpleNamespace(path=path)
    builds = []

    def build():
        builds.append(1)
        return rows(path)

    cache = SourceCache()
    source = cache.source(("files", "u1"), store, build)
    assert source() == digest([]) and source() == digest([]) and len(builds) == 1
    write(path, "x")
    assert source() == digest(["x"]) and len(builds) == 2
    # another tab of the same user shares the cached digest
    assert cache.source(("files", "u1"), store, build)() == digest(["x"]) and len(builds) == 2
    # another user has an own entry
    assert cache.source(("files", "u2"), store, build)() == digest(["x"]) and len(builds) == 3


def test_watcher_reports_the_same_changes_as_with_raw_payloads(tmp_path):
    path = tmp_path / "a.sqlite3"
    make_db(path)
    store = SimpleNamespace(path=path)
    cached = ChangeWatcher({"files": SourceCache().source(("files", "u"), store, lambda: rows(path))})
    raw = ChangeWatcher({"files": lambda: rows(path)})
    for step in range(4):
        assert cached.poll() == raw.poll(), step
        if step % 2:
            write(path, str(step))


def test_store_without_a_path_is_called_directly():
    build = lambda: [1]  # noqa: E731
    assert SourceCache().source(("x",), SimpleNamespace(), build) is build


def test_unreadable_file_falls_back_to_building(tmp_path):
    store = SimpleNamespace(path=tmp_path / "gone.sqlite3")
    source = SourceCache().source(("x",), store, lambda: [2])
    assert source() == digest([2])


def test_stream_reports_agenda_and_file_changes_of_the_signed_in_user(tmp_path, monkeypatch):
    """The real handler with real stores: the first poll lists every source, a commit
    in between makes the next poll list exactly the changed source."""
    import json

    from igris import web
    from igris.accounts import AccountStore
    from igris.agenda import AgendaStore
    from igris.assistant import build_assistant
    from igris.config import Settings
    from test_web_accounts import Browser, Server

    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    settings = Settings(backends=("echo",), slow_backends=("echo",), user_backends=("echo",),
                        data_dir=tmp_path / "server", sessions_dir=tmp_path / "sessions",
                        personas_dir=tmp_path / "personas")
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    accounts.bootstrap("admin-test-password", "user-test-password")
    agenda = AgendaStore(tmp_path / "agenda.sqlite3")
    owner = {}
    sleeps = []

    def sleep(seconds):  # between polls: the user adds an agenda item, then a file
        sleeps.append(seconds)
        if len(sleeps) == 2:
            agenda.upsert(owner["id"], {"branch": "okul", "kind": "exam", "title": "Vize",
                                        "due": "2099-01-01", "source": "manual"})
        if len(sleeps) == 4:
            contexts.files.upload(owner["id"], "a.txt", b"x")

    from igris.user_context import UserContexts

    assistant = build_assistant(settings)
    contexts = UserContexts(assistant)
    server = web.create_server(assistant, port=0, accounts=accounts, contexts=contexts,
                               agenda=agenda, stream_settings={
                                   "interval": 1, "max_seconds": 6, "sleep": sleep,
                                   "clock": lambda: len(sleeps)})
    browser = Browser(server)
    owner["id"] = browser.login("guest", "user-test-password")["user"]["id"]
    _, body, _ = browser.request("GET", "/api/events")
    changes = [json.loads(line[6:])["keys"] for line in body.decode().splitlines()
               if line.startswith("data: {\"keys\"")]
    assert changes[0][:3] == ["tasks", "approvals", "files"] and "agenda" in changes[0]
    assert ["agenda"] in changes and ["files"] in changes


def test_failing_build_is_not_cached(tmp_path):
    path = tmp_path / "a.sqlite3"
    make_db(path)
    calls = []

    def build():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("locked")
        return ["ok"]

    source = SourceCache().source(("x",), SimpleNamespace(path=path), build)
    try:
        source()
    except RuntimeError:
        pass
    assert source() == digest(["ok"]) and len(calls) == 2
