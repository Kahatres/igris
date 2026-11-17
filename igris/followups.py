"""Durable follow-up timers that remind an owner about unanswered mail.

The store is backed by SQLite so timers survive a restart.  Every mutating
operation runs inside an immediate SQLite transaction, which makes a timer fire
exactly once even when several threads or stores call
:meth:`FollowupStore.due` at the same time.
"""

import math
import secrets
import sqlite3
import threading
import time

# A follow-up may be scheduled at most 90 days into the future.
MAX_AFTER_SECONDS = 90 * 24 * 60 * 60


class FollowupError(ValueError):
    """Raised when a follow-up timer cannot be created or changed."""


def _check_text(value, name):
    """Return *value* when it is a non-empty string, otherwise raise."""
    if not isinstance(value, str) or value == "":
        raise FollowupError(f"{name} must be a non-empty string")
    return value


def _check_after_seconds(value):
    """Validate the delay and return it as a float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FollowupError("after_seconds must be a number")
    if not math.isfinite(value):
        raise FollowupError("after_seconds must be finite")
    if value <= 0:
        raise FollowupError("after_seconds must be positive")
    if value > MAX_AFTER_SECONDS:
        raise FollowupError("after_seconds must be at most 90 days")
    return float(value)


class FollowupStore:
    """SQLite-backed store of follow-up timers."""

    def __init__(self, path, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._init_db()

    # -- internals ---------------------------------------------------------

    def _connect(self):
        conn = sqlite3.connect(str(self._path), timeout=30.0)
        # Take full control of transactions: no implicit begin.
        conn.isolation_level = None
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS followups ("
                    " id TEXT PRIMARY KEY,"
                    " owner TEXT NOT NULL,"
                    " thread_id TEXT NOT NULL,"
                    " note TEXT NOT NULL DEFAULT '',"
                    " due_at REAL NOT NULL,"
                    " status TEXT NOT NULL)"
                )
                # At most one open timer per (owner, thread_id).
                conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_followups_open"
                    " ON followups(owner, thread_id) WHERE status = 'open'"
                )
            finally:
                conn.close()

    @staticmethod
    def _row_to_dict(row):
        return {
            "id": row["id"],
            "owner": row["owner"],
            "thread_id": row["thread_id"],
            "note": row["note"],
            "due_at": row["due_at"],
            "status": row["status"],
        }

    # -- public API --------------------------------------------------------

    def add(self, owner, thread_id, after_seconds, note=""):
        """Create an open timer and return its identifier."""
        owner = _check_text(owner, "owner")
        thread_id = _check_text(thread_id, "thread_id")
        delay = _check_after_seconds(after_seconds)
        if not isinstance(note, str):
            raise FollowupError("note must be a string")

        with self._lock:
            # Read the clock only while the lock is held.
            due_at = self._clock() + delay
            timer_id = secrets.token_hex(16)
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "INSERT INTO followups"
                        " (id, owner, thread_id, note, due_at, status)"
                        " VALUES (?, ?, ?, ?, ?, 'open')",
                        (timer_id, owner, thread_id, note, due_at),
                    )
                except sqlite3.IntegrityError as exc:
                    conn.execute("ROLLBACK")
                    raise FollowupError(
                        "an open follow-up already exists for this owner"
                        " and thread"
                    ) from exc
                conn.execute("COMMIT")
            finally:
                conn.close()
        return timer_id

    def resolve(self, owner, thread_id):
        """Close the open timer for a reply; report whether one was open."""
        owner = _check_text(owner, "owner")
        thread_id = _check_text(thread_id, "thread_id")

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE followups SET status = 'resolved'"
                    " WHERE owner = ? AND thread_id = ? AND status = 'open'",
                    (owner, thread_id),
                )
                changed = cursor.rowcount
                conn.execute("COMMIT")
            finally:
                conn.close()
        return changed > 0

    def cancel(self, owner, followup_id):
        """Close the open timer; raise when it is unknown for this owner."""
        owner = _check_text(owner, "owner")
        followup_id = _check_text(followup_id, "followup_id")

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "UPDATE followups SET status = 'cancelled'"
                    " WHERE id = ? AND owner = ? AND status = 'open'",
                    (followup_id, owner),
                )
                changed = cursor.rowcount
                conn.execute("COMMIT")
            finally:
                conn.close()
        if changed == 0:
            raise FollowupError("unknown follow-up for this owner")

    def due(self):
        """Return due timers and mark them fired in one write transaction."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                # Read the clock only after the write lock is held, so one
                # timestamp decides what fires.
                now = self._clock()
                rows = conn.execute(
                    "SELECT id, owner, thread_id, note, due_at, status"
                    " FROM followups"
                    " WHERE status = 'open' AND due_at <= ?"
                    " ORDER BY due_at, id",
                    (now,),
                ).fetchall()
                if rows:
                    conn.executemany(
                        "UPDATE followups SET status = 'fired' WHERE id = ?",
                        [(row["id"],) for row in rows],
                    )
                conn.execute("COMMIT")
            finally:
                conn.close()

        result = []
        for row in rows:
            item = self._row_to_dict(row)
            item["status"] = "fired"
            result.append(item)
        return result

    def list(self, owner):
        """Return the open timers belonging to *owner*."""
        owner = _check_text(owner, "owner")
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT id, owner, thread_id, note, due_at, status"
                    " FROM followups"
                    " WHERE owner = ? AND status = 'open'"
                    " ORDER BY due_at, id",
                    (owner,),
                ).fetchall()
            finally:
                conn.close()
        return [self._row_to_dict(row) for row in rows]
