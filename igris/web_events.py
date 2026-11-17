"""Cheap change detection for the web event stream.

Every open tab polls its data sources every few seconds. A SQLite-backed source is
rebuilt and hashed only when its database changed: ``PRAGMA data_version`` on one
long-lived read-only connection per file moves whenever any other connection commits,
and the stores open a fresh connection for every read and write. Between changes the
source returns the digest of its last payload, so the stream reports the same keys as
before without reading or serialising unchanged data.

The /api/events route lives here too. Depends on events (imported where it is used).
"""

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from urllib.parse import quote


def digest(value) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class DataVersions:
    """``PRAGMA data_version`` of SQLite files, one read-only connection per file."""

    def __init__(self, connect=sqlite3.connect):
        self._connect = connect
        self._connections: dict[str, sqlite3.Connection] = {}
        self._lock = threading.Lock()

    def version(self, path) -> int | None:
        """The file's change counter, or None when it cannot be read (no caching then)."""
        key = str(path)
        with self._lock:
            db = self._connections.get(key)
            try:
                if db is None:
                    if not Path(key).is_file():
                        return None
                    uri = "file:" + quote(Path(key).resolve().as_posix(), safe="/:") + "?mode=ro"
                    db = self._connect(uri, uri=True, timeout=1, check_same_thread=False)
                    self._connections[key] = db
                # fetchall steps the statement to its end, so no read lock is kept
                return db.execute("PRAGMA data_version").fetchall()[0][0]
            except (sqlite3.Error, OSError):
                self._drop(key)
                return None

    def _drop(self, key):
        db = self._connections.pop(key, None)
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass

    def close(self) -> None:
        with self._lock:
            for key in list(self._connections):
                self._drop(key)


class SourceCache:
    """Digests of store-backed event sources, rebuilt only when their file changed."""

    def __init__(self, versions: DataVersions | None = None):
        self.versions = versions or DataVersions()
        self._digests: dict[tuple, tuple[int, str]] = {}
        self._lock = threading.Lock()

    def source(self, key: tuple, store, build):
        """A zero-argument source for ``build()``, cached on ``store.path``'s version.

        It returns the payload's digest, which changes exactly when the payload does.
        Stores without a SQLite ``path`` (test fakes) give their payload every time.
        """
        path = getattr(store, "path", None)
        if not isinstance(path, (str, Path)):
            return build

        def load():
            version = self.versions.version(path)
            if version is None:
                return digest(build())
            with self._lock:
                hit = self._digests.get(key)
            if hit is not None and hit[0] == version:
                return hit[1]
            # read the version first: a commit during build() shows on the next poll
            value = digest(build())
            with self._lock:
                self._digests[key] = (version, value)
            return value

        return load


def sources(h, ctx) -> dict:
    """What the v2 page shows, by the names its polls use; each one is per user."""
    owner = h._owner_id()
    cached = ctx.event_cache.source  # store-backed lists: rebuilt only after a commit
    contexts, agenda, inbox = ctx.contexts, ctx.agenda, ctx.inbox
    found = {}
    if h.login is not None:
        if ctx.tasks is not None:
            found["tasks"] = lambda: ctx.tasks.list(owner)
        if ctx.approvals is not None:
            found["approvals"] = lambda: ctx.approvals.pending(owner)
        found["files"] = cached(("files", owner), contexts.files,
                                lambda: contexts.files.list(owner))
        if agenda is not None:
            items = cached(("agenda", owner), agenda,
                           lambda: agenda.list(owner, include_done=True))
            sync = ctx.calendar_sync
            found["agenda"] = lambda: [items(), sync.status() if sync is not None else None]
    if h._is_admin():
        if contexts is not None and getattr(contexts, "core", None) is not None:
            found["core"] = cached(("core", owner), contexts.core,
                                   lambda: contexts.core.list(owner))
        if ctx.notices is not None:
            found["notices"] = lambda: ctx.notices.unread_count(owner)
        if inbox is not None:
            mails = cached(("inbox",), inbox.store, lambda: inbox.store.recent(50))
            found["inbox"] = lambda: [inbox.status(), mails()]
        found["calendar"] = ctx.calendar_loader
        if ctx.jobs is not None:
            found["jobs"] = lambda: [j.to_dict() for j in ctx.jobs.jobs()]
    return found


def events(h, ctx) -> None:
    """Server-sent events: tells the page which data changed, so it refetches only that."""
    from igris.events import ChangeWatcher, stream

    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream; charset=utf-8")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Content-Type-Options", "nosniff")
    h.send_header("X-Accel-Buffering", "no")
    h.send_header("Connection", "close")
    h.end_headers()
    h.close_connection = True
    end_deadline = getattr(h, "end_request_deadline", None)
    if end_deadline is not None:  # the stream legitimately stays open past the read deadline
        end_deadline()

    def write(chunk: bytes) -> None:
        h.wfile.write(chunk)
        h.wfile.flush()

    stream(ChangeWatcher(sources(h, ctx)), write, **ctx.stream_settings)
