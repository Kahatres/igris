"""Notices for the owner's admin panel: one row per thing worth looking at.

Producers (the inbox watcher, multiagent jobs, the approval flow) call ``add``; the web
interface lists them and marks them read. A notice with a non-empty ``ref`` is written once
per (owner, kind, ref), so a producer that runs again after a restart adds nothing new.
"""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

KINDS = ("approval", "task_waiting", "mail_risky", "mail_important", "calendar_added",
         "calendar_question", "reminder", "job_done", "job_failed", "connection", "models")
TITLE_LIMIT = 200
BODY_LIMIT = 2000
LIST_LIMIT = 500
# newlines stay in a body; every other control character goes
_CONTROL_RE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")


def _clean(value, limit: int, *, single_line: bool) -> str:
    text = _CONTROL_RE.sub(" ", str(value or ""))
    text = " ".join(text.split()) if single_line else text.strip()
    return text[:limit]


class NoticeStore:
    def __init__(self, path: Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notices (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, kind TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL, source TEXT NOT NULL,
                    ref TEXT NOT NULL, created REAL NOT NULL, read_at REAL, action TEXT
                );
                CREATE INDEX IF NOT EXISTS notices_owner ON notices(owner, created);
                CREATE UNIQUE INDEX IF NOT EXISTS notices_ref
                    ON notices(owner, kind, ref) WHERE ref != '';
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _public(row) -> dict:
        item = dict(row)
        item["action"] = json.loads(item["action"]) if item["action"] else None
        return item

    def add(self, owner: str, kind: str, title: str, body: str = "", *, source: str = "",
            ref: str = "", action: dict | None = None) -> dict:
        if kind not in KINDS:
            raise ValueError(f"Unknown notice kind: {kind!r}")
        if not isinstance(owner, str) or not owner:
            raise ValueError("A notice needs an owner")
        if action is not None and not isinstance(action, dict):
            raise ValueError("A notice action must be a JSON object")
        ref = _clean(ref, TITLE_LIMIT, single_line=True)
        row = (secrets.token_hex(8), owner, kind, _clean(title, TITLE_LIMIT, single_line=True),
               _clean(body, BODY_LIMIT, single_line=False),
               _clean(source, TITLE_LIMIT, single_line=True), ref, self.clock(), None,
               json.dumps(action, ensure_ascii=False) if action is not None else None)
        with self._lock, closing(self._connect()) as db, db:
            if ref:
                found = db.execute("SELECT * FROM notices WHERE owner=? AND kind=? AND ref=?",
                                   (owner, kind, ref)).fetchone()
                if found is not None:
                    return self._public(found)
            db.execute("INSERT INTO notices (id, owner, kind, title, body, source, ref, created, "
                       "read_at, action) VALUES (?,?,?,?,?,?,?,?,?,?)", row)
            return self._public(db.execute("SELECT * FROM notices WHERE id=?",
                                           (row[0],)).fetchone())

    def list(self, owner: str, *, unread_only: bool = False, limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), LIST_LIMIT))
        where = "owner=?" + (" AND read_at IS NULL" if unread_only else "")
        with self._lock, closing(self._connect()) as db:
            rows = db.execute(f"SELECT * FROM notices WHERE {where} "
                              "ORDER BY read_at IS NOT NULL, created DESC, rowid DESC LIMIT ?",
                              (owner, limit)).fetchall()
        return [self._public(r) for r in rows]

    def mark_read(self, owner: str, ids) -> int:
        ids = [i for i in dict.fromkeys(ids) if isinstance(i, str)]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock, closing(self._connect()) as db, db:
            return db.execute(f"UPDATE notices SET read_at=? WHERE owner=? AND read_at IS NULL "
                              f"AND id IN ({marks})", (self.clock(), owner, *ids)).rowcount

    def mark_all_read(self, owner: str) -> int:
        with self._lock, closing(self._connect()) as db, db:
            return db.execute("UPDATE notices SET read_at=? WHERE owner=? AND read_at IS NULL",
                              (self.clock(), owner)).rowcount

    def unread_count(self, owner: str) -> int:
        with self._lock, closing(self._connect()) as db:
            return db.execute("SELECT COUNT(*) FROM notices WHERE owner=? AND read_at IS NULL",
                              (owner,)).fetchone()[0]


def safe_add(notices, owner, kind, title, body="", **fields) -> None:
    """Write a notice for a producer; a broken store never breaks the producer."""
    if notices is None or not owner:
        return
    try:
        notices.add(owner() if callable(owner) else owner, kind, title, body, **fields)
    except Exception:
        pass
