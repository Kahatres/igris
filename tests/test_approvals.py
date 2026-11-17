"""Tests for the approval gate in igris.approvals.

Every test injects a FakeClock so expiry is deterministic, and a fresh
SQLite file under tmp_path so approvals never leak between tests.
"""

import sqlite3
import threading

import pytest

from igris.approvals import MAX_TTL, ApprovalError, ApprovalStore


class FakeClock:
    """Injected clock: time only moves when a test advances it."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class EventClock:
    """Injected clock that logs every read into a shared event list."""

    def __init__(self, events: list) -> None:
        self.now = 1_000_000.0
        self.events = events

    def __call__(self) -> float:
        self.events.append("clock")
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_store(tmp_path, clock, **kwargs) -> ApprovalStore:
    """ApprovalStore on a private database file with the injected clock."""
    return ApprovalStore(str(tmp_path / "approvals.db"), clock=clock, **kwargs)


def make_recording_store(tmp_path, clock, events) -> ApprovalStore:
    """ApprovalStore whose connections log BEGIN/COMMIT into ``events``."""

    class RecordingConnection(sqlite3.Connection):
        """Connection that records the transaction statements it runs."""

        def execute(self, sql, *args):
            upper = sql.upper()
            if upper.startswith("BEGIN"):
                events.append("begin-immediate")
            elif upper.startswith("COMMIT"):
                events.append("commit")
            return super().execute(sql, *args)

    class RecordingStore(ApprovalStore):
        """ApprovalStore whose file connections are RecordingConnections."""

        def _connect(self):
            conn = sqlite3.connect(
                self._path, isolation_level=None, timeout=30.0,
                factory=RecordingConnection,
            )
            conn.execute("PRAGMA busy_timeout = 30000")
            return conn

    return RecordingStore(str(tmp_path / "approvals.db"), clock=clock)


def test_request_is_pending_and_listed(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request(
        "ops-agent", "send_mail", "mail to customer x",
        payload={"to": "customer-x"}, ttl=60,
    )

    assert isinstance(rid, str)
    assert rid
    assert store.status(rid) == "pending"

    open_requests = store.pending("ops-agent")
    assert [row["request_id"] for row in open_requests] == [rid]
    assert open_requests[0]["action"] == "send_mail"
    assert open_requests[0]["summary"] == "mail to customer x"
    assert open_requests[0]["owner"] == "ops-agent"
    assert store.pending("other-agent") == []


def test_lifecycle_covers_pending_approved_and_used(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request(
        "agent", "fill_form", "tax form 2026", payload={"form": "2026"}
    )

    assert store.status(rid) == "pending"

    store.answer(rid, True, source="web")
    assert store.status(rid) == "approved"

    assert store.consume(
        "agent", rid, "fill_form", payload={"form": "2026"}
    ) is None
    assert store.status(rid) == "used"

    # A used request is spent: gone from pending, and no second spend.
    assert store.pending("agent") == []
    with pytest.raises(ApprovalError):
        store.consume("agent", rid, "fill_form", payload={"form": "2026"})


def test_denied_status(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request(
        "agent", "change_file", "rewrite config.py", payload={"path": "config.py"}
    )

    store.answer(rid, False, source="telegram")
    assert store.status(rid) == "denied"

    with pytest.raises(ApprovalError):
        store.consume("agent", rid, "change_file", payload={"path": "config.py"})

    # A denial is final even long after the request was made.
    clock.advance(10 * 86400)
    assert store.status(rid) == "denied"


def test_expiry_before_answer(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock, default_ttl=100)
    rid = store.request("agent", "send_mail", "reminder", payload={"to": "x"})

    clock.advance(100)
    assert store.status(rid) == "expired"

    # Expired requests can no longer be answered or consumed.
    with pytest.raises(ApprovalError):
        store.answer(rid, True, source="web")
    with pytest.raises(ApprovalError):
        store.consume("agent", rid, "send_mail", payload={"to": "x"})
    assert store.status(rid) == "expired"
    assert store.pending("agent") == []

    # A new request on the same store is unaffected.
    other = store.request("agent", "send_mail", "reminder", payload={"to": "x"})
    assert store.status(other) == "pending"


def test_expiry_before_consume_rejects_approved_request(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock, default_ttl=100)
    rid = store.request("agent", "fill_form", "form", payload={"form": "a"})

    store.answer(rid, True, source="web")
    assert store.status(rid) == "approved"

    clock.advance(150)
    assert store.status(rid) == "expired"

    # The human said yes, but the approval window closed before consume.
    with pytest.raises(ApprovalError):
        store.consume("agent", rid, "fill_form", payload={"form": "a"})
    assert store.status(rid) == "expired"


def test_request_ttl_overrides_default(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock, default_ttl=10_000)

    short = store.request(
        "agent", "send_mail", "quick one", payload={"to": "x"}, ttl=10
    )
    clock.advance(11)
    assert store.status(short) == "expired"

    long_lived = store.request(
        "agent", "send_mail", "default ttl", payload={"to": "y"}
    )
    clock.advance(500)
    assert store.status(long_lived) == "pending"


def test_owner_mismatch_is_rejected(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request("agent-a", "send_mail", "hello", payload={"to": "x"})

    store.answer(rid, True, source="web")

    with pytest.raises(ApprovalError):
        store.consume("agent-b", rid, "send_mail", payload={"to": "x"})

    # The mismatch left the request untouched and still consumable
    # by its rightful owner exactly once.
    assert store.status(rid) == "approved"
    store.consume("agent-a", rid, "send_mail", payload={"to": "x"})
    assert store.status(rid) == "used"


def test_action_mismatch_is_rejected(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request("agent", "send_mail", "hello", payload={"to": "x"})

    store.answer(rid, True, source="web")

    # The approval covers exactly the action that was requested.
    with pytest.raises(ApprovalError):
        store.consume("agent", rid, "fill_form", payload={"to": "x"})

    assert store.status(rid) == "approved"
    store.consume("agent", rid, "send_mail", payload={"to": "x"})
    assert store.status(rid) == "used"


def test_payload_substitution_is_rejected(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request("agent", "send_mail", "hello", payload={"to": "alice"})

    store.answer(rid, True, source="telegram")
    assert store.status(rid) == "approved"

    # The human approved one mail; a different payload may never be sent
    # under the same approval, even with the same owner and action.
    with pytest.raises(ApprovalError):
        store.consume("agent", rid, "send_mail", payload={"to": "evil"})
    assert store.status(rid) == "approved"

    store.consume("agent", rid, "send_mail", payload={"to": "alice"})
    assert store.status(rid) == "used"


def test_payload_digest_ignores_key_order(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request(
        "agent", "send_mail", "hello",
        payload={"to": "alice", "body": {"subject": "hi", "text": "yo"}},
    )

    store.answer(rid, True, source="web")

    # The same logical payload with keys in another order digests equal,
    # because the digest is taken over canonical JSON (sort_keys, compact).
    store.consume(
        "agent", rid, "send_mail",
        payload={"body": {"text": "yo", "subject": "hi"}, "to": "alice"},
    )
    assert store.status(rid) == "used"


def test_non_serializable_payload_is_rejected(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)

    with pytest.raises(ApprovalError):
        store.request(
            "agent", "send_mail", "hello", payload={"to": object()}
        )


def test_answer_requires_real_bool(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request("agent", "send_mail", "hello", payload={"to": "alice"})

    # Anything that is not a real bool is rejected, including strings
    # that look like decisions such as the string 'false'.
    for bad_answer in ("false", "true", 1, 0, None, [True]):
        with pytest.raises(ApprovalError):
            store.answer(rid, bad_answer, source="web")

    # The request is untouched and still answerable by a human.
    assert store.status(rid) == "pending"
    store.answer(rid, True, source="web")
    assert store.status(rid) == "approved"


def test_answer_rejects_sources_outside_human_channels(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request("agent", "send_mail", "hello", payload={"to": "alice"})

    for bad_source in ("slack:alice", "phone:bob", "email", "model", "WEB", ""):
        with pytest.raises(ApprovalError):
            store.answer(rid, True, source=bad_source)

    assert store.status(rid) == "pending"
    store.answer(rid, True, source="telegram")
    store.consume("agent", rid, "send_mail", payload={"to": "alice"})
    assert store.status(rid) == "used"


def test_ttl_must_be_finite_positive_number(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)

    for bad_ttl in (0, -1, -0.5, float("inf"), float("nan"), True, "60", [60]):
        with pytest.raises(ApprovalError):
            store.request(
                "agent", "send_mail", "hello", payload={"to": "x"}, ttl=bad_ttl
            )

    for bad_default_ttl in (0, -1, True, "60", float("inf")):
        with pytest.raises(ApprovalError):
            ApprovalStore(
                str(tmp_path / "bad-default.db"), clock=clock,
                default_ttl=bad_default_ttl,
            )

    # A valid fractional ttl still works.
    ok = store.request(
        "agent", "send_mail", "hello", payload={"to": "x"}, ttl=0.5
    )
    clock.advance(0.4)
    assert store.status(ok) == "pending"


def test_clock_is_read_after_the_write_lock_is_held(tmp_path):
    """BEGIN IMMEDIATE must come before the first clock read.

    If answer or consume read the clock before acquiring the write
    lock, a caller waiting on a busy database would start from a stale,
    pre-lock time and might spend or answer an approval that expired
    while it waited on the lock.
    """
    events: list = []
    clock = EventClock(events)
    store = make_recording_store(tmp_path, clock, events)
    rid = store.request(
        "agent", "send_mail", "hello", payload={"to": "alice"}, ttl=100
    )

    for step in ("answer", "consume"):
        events.clear()
        if step == "answer":
            store.answer(rid, True, source="web")
        else:
            store.consume("agent", rid, "send_mail", payload={"to": "alice"})
        assert events == ["begin-immediate", "clock", "commit"], step


def test_consume_works_while_still_within_ttl(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request(
        "agent", "send_mail", "hello", payload={"to": "x"}, ttl=50
    )

    clock.advance(49)
    store.answer(rid, True, source="web")
    store.consume("agent", rid, "send_mail", payload={"to": "x"})
    assert store.status(rid) == "used"


def test_double_answer_raises_and_first_answer_stands(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)

    rid = store.request("agent", "send_mail", "first", payload={"to": "x"})
    store.answer(rid, True, source="web")
    with pytest.raises(ApprovalError):
        store.answer(rid, False, source="telegram")

    # The first human decision is the only one that counts.
    assert store.status(rid) == "approved"
    store.consume("agent", rid, "send_mail", payload={"to": "x"})
    assert store.status(rid) == "used"


def test_double_answer_after_denial(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)

    rid = store.request("agent", "send_mail", "second", payload={"to": "x"})
    store.answer(rid, False, source="telegram")
    with pytest.raises(ApprovalError):
        store.answer(rid, True, source="web")

    assert store.status(rid) == "denied"
    with pytest.raises(ApprovalError):
        store.consume("agent", rid, "send_mail", payload={"to": "x"})


def test_already_used_request_cannot_be_reanswered(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)
    rid = store.request("agent", "send_mail", "once", payload={"to": "x"})

    store.answer(rid, True, source="web")
    store.consume("agent", rid, "send_mail", payload={"to": "x"})

    with pytest.raises(ApprovalError):
        store.answer(rid, True, source="web")


def test_unknown_request_id(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock)

    with pytest.raises(ApprovalError):
        store.answer("nosuchid", True, source="web")
    with pytest.raises(ApprovalError):
        store.status("nosuchid")
    with pytest.raises(ApprovalError):
        store.consume("agent", "nosuchid", "send_mail", payload={"to": "x"})


def test_approvals_survive_restart(tmp_path):
    clock = FakeClock()
    db = tmp_path / "approvals.db"
    store = ApprovalStore(str(db), clock=clock)

    approved = store.request(
        "agent", "send_mail", "hello", payload={"to": "alice"}, ttl=5_000
    )
    store.answer(approved, True, source="web")
    denied = store.request(
        "agent", "send_mail", "no", payload={"to": "bob"}
    )
    store.answer(denied, False, source="telegram")
    unanswered = store.request(
        "agent", "change_file", "later", payload={"path": "config.py"}
    )
    store.close()

    # A new store instance over the same file, time moved forward.
    clock.advance(60)
    reopened = ApprovalStore(str(db), clock=clock)

    assert reopened.status(approved) == "approved"
    assert reopened.status(denied) == "denied"
    assert reopened.status(unanswered) == "pending"

    reopened.consume("agent", approved, "send_mail", payload={"to": "alice"})
    assert reopened.status(approved) == "used"
    with pytest.raises(ApprovalError):
        reopened.consume("agent", denied, "send_mail", payload={"to": "bob"})


def test_concurrent_consume_has_single_winner(tmp_path):
    clock = FakeClock()
    store = make_store(tmp_path, clock, default_ttl=600)
    rid = store.request("agent", "send_mail", "one shot", payload={"to": "x"})

    store.answer(rid, True, source="web")

    thread_count = 8
    barrier = threading.Barrier(thread_count)
    successes = []
    failures = []

    def worker():
        barrier.wait()
        try:
            store.consume("agent", rid, "send_mail", payload={"to": "x"})
            successes.append(threading.get_ident())
        except ApprovalError:
            failures.append(threading.get_ident())

    workers = [threading.Thread(target=worker) for _ in range(thread_count)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join()

    # Exactly one thread spent the approval; every other thread lost.
    assert len(successes) == 1
    assert len(failures) == thread_count - 1
    assert store.status(rid) == "used"


@pytest.mark.parametrize("payload", [float("nan"), {"a": float("inf")}])
def test_non_finite_payload_is_rejected(tmp_path, payload):
    store = ApprovalStore(str(tmp_path / "a.sqlite3"))
    with pytest.raises(ApprovalError):
        store.request("owner", "mail", "summary", payload=payload)


def test_ttl_has_an_upper_limit(tmp_path):
    store = ApprovalStore(str(tmp_path / "a.sqlite3"))
    with pytest.raises(ApprovalError):
        store.request("owner", "mail", "summary", payload={}, ttl=MAX_TTL + 1)
    assert store.request("owner", "mail", "summary", payload={}, ttl=MAX_TTL)
