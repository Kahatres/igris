import threading

import pytest

from igris.followups import FollowupError, FollowupStore, MAX_AFTER_SECONDS


class FakeClock:
    """Deterministic clock for tests."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def store(tmp_path, clock):
    return FollowupStore(tmp_path / "followups.db", clock=clock)


def test_add_returns_open_timer(store, clock):
    timer_id = store.add("alice", "thread-1", 60, note="ping")
    assert isinstance(timer_id, str)
    assert len(timer_id) == 32

    timers = store.list("alice")
    assert len(timers) == 1
    timer = timers[0]
    assert timer["id"] == timer_id
    assert timer["owner"] == "alice"
    assert timer["thread_id"] == "thread-1"
    assert timer["note"] == "ping"
    assert timer["status"] == "open"
    assert timer["due_at"] == pytest.approx(clock.now + 60)


def test_note_defaults_to_empty_string(store):
    store.add("alice", "thread-1", 10)
    assert store.list("alice")[0]["note"] == ""


def test_resolved_status(store):
    store.add("alice", "thread-1", 10)

    assert store.resolve("alice", "thread-1") is True
    # Resolving again reports that nothing was open.
    assert store.resolve("alice", "thread-1") is False
    assert store.list("alice") == []
    # A resolved timer never fires.
    assert store.due() == []


def test_resolve_only_affects_matching_owner(store):
    store.add("alice", "thread-1", 10)
    assert store.resolve("bob", "thread-1") is False
    assert len(store.list("alice")) == 1


def test_cancelled_status(store):
    timer_id = store.add("alice", "thread-1", 10)

    store.cancel("alice", timer_id)
    assert store.list("alice") == []

    with pytest.raises(FollowupError):
        store.cancel("alice", timer_id)


def test_cancel_unknown_for_owner(store):
    timer_id = store.add("alice", "thread-1", 10)
    with pytest.raises(FollowupError):
        store.cancel("bob", timer_id)
    # The real owner still has the open timer.
    assert len(store.list("alice")) == 1

    with pytest.raises(FollowupError):
        store.cancel("alice", "does-not-exist")


def test_fired_status(store, clock):
    store.add("alice", "thread-1", 5)

    # Not yet due.
    assert store.due() == []

    clock.advance(10)
    fired = store.due()
    assert len(fired) == 1
    assert fired[0]["status"] == "fired"
    assert fired[0]["owner"] == "alice"

    # Fires exactly once.
    assert store.due() == []
    assert store.list("alice") == []


def test_concurrent_due_fires_once(store, clock):
    store.add("alice", "thread-1", 1)
    clock.advance(2)

    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(store.due())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    fired = [timer for result in results for timer in result]
    assert len(fired) == 1
    assert fired[0]["status"] == "fired"


def test_owner_isolation(store):
    store.add("alice", "thread-1", 10)
    store.add("bob", "thread-2", 10)
    store.add("bob", "thread-3", 10)

    alice = store.list("alice")
    bob = store.list("bob")

    assert [timer["owner"] for timer in alice] == ["alice"]
    assert sorted(timer["thread_id"] for timer in bob) == ["thread-2", "thread-3"]


def test_duplicate_open_timer_rejected(store):
    store.add("alice", "thread-1", 10)

    with pytest.raises(FollowupError):
        store.add("alice", "thread-1", 20)

    # The same thread for another owner is fine.
    store.add("bob", "thread-1", 10)
    # Another thread for the same owner is fine.
    store.add("alice", "thread-2", 10)

    assert len(store.list("alice")) == 2


def test_new_timer_after_old_one_closed(store):
    store.add("alice", "thread-1", 10)
    assert store.resolve("alice", "thread-1") is True

    new_id = store.add("alice", "thread-1", 10)
    timers = store.list("alice")
    assert len(timers) == 1
    assert timers[0]["id"] == new_id


def test_new_timer_after_cancel(store):
    first_id = store.add("alice", "thread-1", 10)
    store.cancel("alice", first_id)

    second_id = store.add("alice", "thread-1", 10)
    assert second_id != first_id
    assert store.list("alice")[0]["id"] == second_id


@pytest.mark.parametrize(
    "after_seconds",
    [
        0,
        -1,
        -0.5,
        True,
        False,
        float("inf"),
        float("-inf"),
        float("nan"),
        MAX_AFTER_SECONDS + 1,
        "10",
        None,
    ],
)
def test_invalid_after_seconds(store, after_seconds):
    with pytest.raises(FollowupError):
        store.add("alice", "thread-1", after_seconds)


def test_max_after_seconds_allowed(store):
    store.add("alice", "thread-1", MAX_AFTER_SECONDS)
    assert len(store.list("alice")) == 1


@pytest.mark.parametrize("owner", ["", None, 123, True])
def test_invalid_owner(store, owner):
    with pytest.raises(FollowupError):
        store.add(owner, "thread-1", 10)


@pytest.mark.parametrize("thread_id", ["", None, 123, True])
def test_invalid_thread_id(store, thread_id):
    with pytest.raises(FollowupError):
        store.add("alice", thread_id, 10)


def test_invalid_note(store):
    with pytest.raises(FollowupError):
        store.add("alice", "thread-1", 10, note=123)


def test_restart_preserves_timers(tmp_path, clock):
    path = tmp_path / "followups.db"

    first = FollowupStore(path, clock=clock)
    timer_id = first.add("alice", "thread-1", 30, note="waiting")

    # A brand new store instance reads the timer from the database.
    second = FollowupStore(path, clock=clock)
    timers = second.list("alice")
    assert len(timers) == 1
    assert timers[0]["id"] == timer_id
    assert timers[0]["note"] == "waiting"

    clock.advance(31)
    fired = second.due()
    assert len(fired) == 1
    assert fired[0]["id"] == timer_id

    # And the fired state survives another restart.
    third = FollowupStore(path, clock=clock)
    assert third.list("alice") == []
    assert third.due() == []


def test_restart_preserves_closed_state(tmp_path, clock):
    path = tmp_path / "followups.db"

    first = FollowupStore(path, clock=clock)
    first.add("alice", "thread-1", 10)
    assert first.resolve("alice", "thread-1") is True

    second = FollowupStore(path, clock=clock)
    assert second.list("alice") == []
    assert second.resolve("alice", "thread-1") is False


def test_timer_fires_exactly_at_its_due_time(tmp_path):
    now = [100.0]
    store = FollowupStore(tmp_path / "exact.sqlite3", clock=lambda: now[0])
    store.add("owner", "thread", 50)
    now[0] = 149.9
    assert store.due() == []
    now[0] = 150.0
    assert [item["thread_id"] for item in store.due()] == ["thread"]


def test_two_stores_on_one_database_fire_a_timer_once(tmp_path):
    path = tmp_path / "shared.sqlite3"
    stores = [FollowupStore(path, clock=lambda: 1000.0) for _ in range(4)]
    FollowupStore(path, clock=lambda: 0.0).add("owner", "thread", 1)
    results, gate = [], threading.Barrier(len(stores))

    def fire(store):
        gate.wait()
        results.extend(store.due())

    threads = [threading.Thread(target=fire, args=(store,)) for store in stores]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 1
