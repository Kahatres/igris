"""Code-level approval gate for every action that leaves the system.

Sending mail, filling a form, changing a file: each such action must
consume an approval record from here. Only a human answer can move a
request from pending to approved; the model that asks for the action can
never approve it by itself.

Each request is also bound to an exact action payload: ``request``
stores the SHA-256 digest of the payload's canonical JSON form and
``consume`` rejects any other payload, so approving one mail can never
send another.

Security note: ``answer`` belongs only to authenticated human input
paths (web, telegram). It must never be exposed as a model tool.

The store is backed by a SQLite database file, so approvals survive a
restart. ``consume`` runs inside an immediate write transaction, which
makes a spend atomic so two threads can never both succeed. The clock is
read only after that write lock is held, so waiting on the lock cannot
let an expired approval pass. It is injected so tests control expiry.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional

__all__ = ["ApprovalError", "ApprovalStore", "HUMAN_SOURCES"]


class ApprovalError(ValueError):
    """An approval request is unknown, pending, denied, expired, or already used."""


# Only these authenticated human channels may approve or deny a request.
HUMAN_SOURCES = ("web", "telegram")

# An approval that outlives this is no longer a decision about the present.
MAX_TTL = 30 * 86400.0


def _payload_digest(payload: Any) -> str:
    """SHA-256 digest of the canonical JSON encoding of ``payload``.

    ``sort_keys`` and compact separators make the digest independent of
    key order and whitespace, so the same logical payload always hashes
    to the same value. Non-serializable payloads raise ApprovalError.
    """
    try:
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ApprovalError(
            f"payload must be JSON-serializable: {exc}"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _check_ttl(value: Any, name: str) -> float:
    """Validate ``ttl``/``default_ttl``: a finite, positive, non-bool number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApprovalError(f"{name} must be a number, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or number <= 0.0 or number > MAX_TTL:
        raise ApprovalError(
            f"{name} must be a positive number up to {MAX_TTL:.0f} seconds,"
            f" got {value!r}"
        )
    return number


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS approvals (
    request_id  TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    action      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    approved    INTEGER,
    source      TEXT,
    answered_at REAL,
    consumed_at REAL,
    payload_digest TEXT NOT NULL
)
"""

# Shared column list for SELECT/INSERT so the layout lives in one place.
_ROW_COLUMNS = (
    "request_id, owner, action, summary, created_at, expires_at,"
    " approved, source, answered_at, consumed_at, payload_digest"
)

# Row indices used below when reading columns of a raw row.
_IDX_CONSUMED_AT = 9
_IDX_APPROVED = 6
_IDX_EXPIRES_AT = 5
_IDX_OWNER = 1
_IDX_ACTION = 2
_IDX_SOURCE = 7
_IDX_PAYLOAD_DIGEST = 10


class ApprovalStore:
    """SQLite-backed approval gate with an injected clock for tests."""

    def __init__(
        self,
        path: str,
        clock: Callable[[], float] = time.time,
        default_ttl: float = 86400.0,
    ) -> None:
        self._path = path
        self._clock = clock
        self._default_ttl = _check_ttl(default_ttl, "default_ttl")
        # Reentrant lock: _connect() also takes it while creating the
        # shared connection for the in-memory special case.
        self._memory_lock = threading.RLock()
        self._memory_conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    # -- connection plumbing -------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Return a connection for this store.

        File-backed stores use a fresh connection per operation so several
        ApprovalStore instances (tests, restarts) can share the database.
        The in-memory special case holds one shared connection guarded by
        a lock, because an in-memory SQLite database lives inside a single
        connection.
        """
        if self._path is None or self._path == ":memory:":
            with self._memory_lock:
                if self._memory_conn is None:
                    self._memory_conn = sqlite3.connect(
                        ":memory:", isolation_level=None, timeout=30.0,
                        check_same_thread=False,
                    )
                return self._memory_conn
        conn = sqlite3.connect(self._path, isolation_level=None, timeout=30.0)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _session(self):
        """Yield a connection; close file connections, serialize memory ones."""
        conn = self._connect()
        if conn is self._memory_conn:
            self._memory_lock.acquire()
            try:
                yield conn
            finally:
                self._memory_lock.release()
        else:
            try:
                yield conn
            finally:
                conn.close()

    def _ensure_schema(self) -> None:
        with self._session() as conn:
            conn.execute(_SCHEMA)

    def close(self) -> None:
        """Release the in-memory connection, if one was created."""
        with self._memory_lock:
            if self._memory_conn is not None:
                self._memory_conn.close()
                self._memory_conn = None

    # -- row helpers ----------------------------------------------------

    def _fetch_row(self, conn: sqlite3.Connection, request_id: str):
        cur = conn.execute(
            f"SELECT {_ROW_COLUMNS} FROM approvals WHERE request_id = ?",
            (request_id,),
        )
        return cur.fetchone()

    def _derive_status(self, row: tuple, now: float) -> str:
        """Classify a row against the current time."""
        if row[_IDX_CONSUMED_AT] is not None:
            return "used"
        if row[_IDX_APPROVED] is None:
            return "expired" if now >= row[_IDX_EXPIRES_AT] else "pending"
        if not row[_IDX_APPROVED]:
            return "denied"
        # A yes that was never spent in time counts as expired too: the
        # approval was granted, but its window closed before consume.
        return "expired" if now >= row[_IDX_EXPIRES_AT] else "approved"

    # -- public API ------------------------------------------------------

    def request(
        self,
        owner: str,
        action: str,
        summary: str,
        *,
        payload: Any,
        ttl: Optional[float] = None,
    ) -> str:
        """Open a new approval request and return its opaque id.

        ``payload`` is the exact action this approval may ever cover: its
        canonical JSON digest is stored here and enforced by ``consume``.
        """
        ttl = _check_ttl(self._default_ttl if ttl is None else ttl, "ttl")
        now = self._clock()
        request_id = secrets.token_urlsafe(16)
        digest = _payload_digest(payload)
        with self._session() as conn:
            conn.execute(_SCHEMA)
            conn.execute(
                f"INSERT INTO approvals ({_ROW_COLUMNS})"
                " VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?)",
                (request_id, owner, action, summary, now, now + ttl, digest),
            )
        return request_id

    def answer(self, request_id: str, approved: bool, source: str) -> None:
        """Record the human answer exactly once.

        Only a pending request still inside its ttl can be answered, and
        a second answer always fails: the first human decision stands.

        ``answer`` belongs only to authenticated human input paths; it
        must never be exposed as a model tool.
        """
        if not isinstance(approved, bool):
            raise ApprovalError(
                f"approved must be a real bool,"
                f" got {type(approved).__name__}"
            )
        if source not in HUMAN_SOURCES:
            raise ApprovalError(
                f"source must be one of {HUMAN_SOURCES}, got {source!r}"
            )
        with self._session() as conn:
            try:
                # BEGIN IMMEDIATE first, then read the clock: waiting on
                # the write lock must not let an expired approval pass.
                conn.execute("BEGIN IMMEDIATE")
                now = self._clock()
                row = self._fetch_row(conn, request_id)
                if row is None:
                    raise ApprovalError(
                        f"unknown approval request: {request_id!r}"
                    )
                status = self._derive_status(row, now)
                if status != "pending":
                    raise ApprovalError(
                        f"approval request {request_id!r} is {status}"
                        " and can no longer be answered"
                    )
                conn.execute(
                    "UPDATE approvals"
                    " SET approved = ?, source = ?, answered_at = ?"
                    " WHERE request_id = ?",
                    (1 if approved else 0, source, now, request_id),
                )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback_if_active(conn)
                raise

    def status(self, request_id: str) -> str:
        """Classify a request: pending, approved, denied, expired or used."""
        with self._session() as conn:
            row = self._fetch_row(conn, request_id)
        if row is None:
            raise ApprovalError(f"unknown approval request: {request_id!r}")
        return self._derive_status(row, self._clock())

    def consume(
        self, owner: str, request_id: str, action: str, *, payload: Any
    ) -> None:
        """Spend an approval exactly once, atomically.

        Succeeds only when a human approved the request, its ttl has not
        passed, the canonical JSON digest of ``payload`` matches the
        payload that was requested, and both owner and action match what
        was requested. Every other case raises ApprovalError and leaves
        the request untouched.
        """
        digest = _payload_digest(payload)
        with self._session() as conn:
            try:
                # The immediate write transaction serializes concurrent
                # consumers: the loser sees consumed_at already set. The
                # clock is read only after the lock is held, so waiting
                # on the lock cannot let an expired approval pass.
                conn.execute("BEGIN IMMEDIATE")
                now = self._clock()
                row = self._fetch_row(conn, request_id)
                if row is None:
                    raise ApprovalError(
                        f"unknown approval request: {request_id!r}"
                    )
                if row[_IDX_CONSUMED_AT] is not None:
                    raise ApprovalError(
                        f"approval request {request_id!r} has already been used"
                    )
                if row[_IDX_APPROVED] is None:
                    if now >= row[_IDX_EXPIRES_AT]:
                        raise ApprovalError(
                            f"approval request {request_id!r} expired"
                            " before it was answered"
                        )
                    raise ApprovalError(
                        f"approval request {request_id!r} is still pending;"
                        " no human answered it yet"
                    )
                if not row[_IDX_APPROVED]:
                    raise ApprovalError(
                        f"approval request {request_id!r} was denied"
                        f" by {row[_IDX_SOURCE]!r}"
                    )
                if row[_IDX_OWNER] != owner or row[_IDX_ACTION] != action:
                    raise ApprovalError(
                        f"approval request {request_id!r} belongs to"
                        f" owner {row[_IDX_OWNER]!r} for action"
                        f" {row[_IDX_ACTION]!r}, not"
                        f" ({owner!r}, {action!r})"
                    )
                if row[_IDX_PAYLOAD_DIGEST] != digest:
                    raise ApprovalError(
                        f"approval request {request_id!r} was requested"
                        " with a different payload"
                    )
                if now >= row[_IDX_EXPIRES_AT]:
                    raise ApprovalError(
                        f"approval request {request_id!r} is approved but"
                        " expired before it was consumed"
                    )
                conn.execute(
                    "UPDATE approvals SET consumed_at = ?"
                    " WHERE request_id = ? AND consumed_at IS NULL",
                    (now, request_id),
                )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback_if_active(conn)
                raise

    def pending(self, owner: str) -> List[Dict[str, Any]]:
        """List the owner's open requests: unanswered, unspent, within ttl."""
        now = self._clock()
        with self._session() as conn:
            cur = conn.execute(
                f"SELECT {_ROW_COLUMNS} FROM approvals"
                " WHERE owner = ? AND approved IS NULL"
                " AND consumed_at IS NULL AND expires_at > ?"
                " ORDER BY created_at, request_id",
                (owner, now),
            )
            rows = cur.fetchall()
        return [
            {
                "request_id": row[0],
                "owner": row[_IDX_OWNER],
                "action": row[_IDX_ACTION],
                "summary": row[3],
                "created_at": row[4],
                "expires_at": row[_IDX_EXPIRES_AT],
            }
            for row in rows
        ]

    # -- transaction helpers ---------------------------------------------

    @staticmethod
    def _rollback_if_active(conn: sqlite3.Connection) -> None:
        """Roll back an open transaction so the connection stays usable."""
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
