"""Background tasks with deterministic fake runners and no network or CLI calls."""

import sqlite3
import threading
import time

import pytest

from igris.tasks import TaskError, TaskMissing, TaskQueue, WaitForEvent, WaitForUser

OWNER, OTHER = "a" * 32, "b" * 32


def finish(queue):
    with queue._lock:
        threads = list(queue._threads.values())
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()


def test_private_results_and_persistence(tmp_path):
    queue = TaskQueue(tmp_path / "q.sqlite3", lambda task: "result: " + task["prompt"])
    task = queue.submit(OWNER, "private request")
    assert queue.list(OTHER) == []
    for action in (queue.get, queue.cancel):
        with pytest.raises(TaskMissing):
            action(OTHER, task["id"])
    queue.poll()
    finish(queue)
    result = queue.get(OWNER, task["id"])
    assert result["status"] == "done" and result["result"] == "result: private request"
    assert TaskQueue(queue.path, lambda _: "unused").get(OWNER, task["id"])["result"] == result["result"]


def test_limits_fairness_and_running_cancellation(tmp_path):
    release = threading.Event()
    started = {OWNER: threading.Event(), OTHER: threading.Event()}
    def runner(task):
        started[task["owner"]].set()
        assert release.wait(5)
        return "discard after cancellation"
    queue = TaskQueue(tmp_path / "q.sqlite3", runner, max_workers=2, max_pending=2)
    first = queue.submit(OWNER, "first")
    second = queue.submit(OWNER, "second")
    other = queue.submit(OTHER, "other")
    with pytest.raises(TaskError, match="full"):
        queue.submit(OWNER, "overflow")
    queue.poll()
    try:
        assert all(event.wait(2) for event in started.values())
        assert queue.get(OWNER, second["id"])["status"] == "queued"
        assert queue.get(OTHER, other["id"])["status"] == "running"
        assert queue.cancel(OWNER, first["id"])["status"] == "cancel_requested"
        queue.poll()
        assert queue.get(OWNER, second["id"])["status"] == "queued"
    finally:
        release.set()
        finish(queue)
    assert queue.get(OWNER, first["id"])["status"] == "cancelled"
    assert queue.get(OWNER, first["id"])["result"] is None
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, second["id"])["status"] == "done"


def test_queued_cancel_never_calls_runner_and_close_stops_dispatch(tmp_path):
    calls = []
    queue = TaskQueue(tmp_path / "q.sqlite3", lambda task: calls.append(task))
    task = queue.submit(OWNER, "cancel")
    assert queue.cancel(OWNER, task["id"])["status"] == "cancelled"
    queue.poll()
    assert calls == []
    task = queue.submit(OWNER, "next")
    queue.close()
    queue.poll()
    assert queue.get(OWNER, task["id"])["status"] == "queued"


def test_recovery_never_replays_running_tasks(tmp_path):
    queue = TaskQueue(tmp_path / "q.sqlite3", lambda _: "unused")
    task = queue.submit(OWNER, "interrupted")
    with sqlite3.connect(queue.path) as db:
        db.execute("UPDATE tasks SET status='running' WHERE id=?", (task["id"],))
    calls = []
    recovered = TaskQueue(queue.path, lambda task: calls.append(task))
    assert recovered.get(OWNER, task["id"])["status"] == "interrupted"
    recovered.poll()
    assert calls == []


def test_errors_are_not_leaked(tmp_path):
    def fail(task):
        raise RuntimeError("secret server details")
    queue = TaskQueue(tmp_path / "q.sqlite3", fail)
    task = queue.submit(OWNER, "fail")
    queue.poll()
    finish(queue)
    result = queue.get(OWNER, task["id"])
    assert result["status"] == "failed"
    assert "secret" not in result["error"]


@pytest.mark.parametrize("prompt", ["", None, " " * 4, "x" * 8001])
def test_invalid_prompts(tmp_path, prompt):
    queue = TaskQueue(tmp_path / "q.sqlite3", lambda _: "unused")
    with pytest.raises(TaskError):
        queue.submit(OWNER, prompt)


def test_waiting_for_user_resume_flow(tmp_path):
    def runner(task):
        if task["resume"] is None:
            return WaitForUser("What is the server name?")
        assert task["resume"] == {"answer": "alpha", "payload": None, "wake_reason": "resume"}
        return "answered: " + task["resume"]["answer"]
    queue = TaskQueue(tmp_path / "q.sqlite3", runner)
    task = queue.submit(OWNER, "ask me")
    queue.poll()
    finish(queue)
    waiting = queue.get(OWNER, task["id"])
    assert waiting["status"] == "waiting_for_user"
    assert waiting["result"] is None and waiting["finished"] is None
    assert waiting["waiting_question"] == "What is the server name?"
    assert waiting["event_key"] is None and waiting["deadline"] is None
    # get and list expose the waiting fields.
    listed = queue.list(OWNER)[0]
    assert listed["status"] == "waiting_for_user"
    assert listed["waiting_question"] == "What is the server name?"
    assert listed["event_key"] is None and listed["deadline"] is None
    # No worker slot is held while waiting.
    with queue._lock:
        assert queue._threads == {}
    resumed = queue.resume(OWNER, task["id"], "alpha")
    assert resumed["status"] == "queued"
    assert resumed["waiting_question"] is None
    queue.poll()
    finish(queue)
    done = queue.get(OWNER, task["id"])
    assert done["status"] == "done" and done["result"] == "answered: alpha"


def test_resume_requires_waiting_and_correct_owner(tmp_path):
    queue = TaskQueue(tmp_path / "q.sqlite3", lambda _: WaitForUser("q?"))
    task = queue.submit(OWNER, "task")
    with pytest.raises(TaskMissing):
        queue.resume(OWNER, "unknown-id", "a")
    with pytest.raises(TaskMissing):
        queue.resume(OTHER, task["id"], "a")  # owner mismatch hides the task
    with pytest.raises(TaskError):
        queue.resume(OWNER, task["id"], "a")  # not waiting yet
    queue.poll()
    finish(queue)
    assert queue.resume(OWNER, task["id"], "ok")["status"] == "queued"
    with pytest.raises(TaskError):
        queue.resume(OWNER, task["id"], "again")  # no longer waiting


def test_fire_event_moves_tasks_and_carries_payload(tmp_path):
    def runner(task):
        if task["resume"] is None:
            return WaitForEvent("deploy-done")
        return "resumed %s %r" % (task["resume"]["wake_reason"], task["resume"]["payload"])
    queue = TaskQueue(tmp_path / "q.sqlite3", runner)
    mine = queue.submit(OWNER, "mine")
    theirs = queue.submit(OTHER, "theirs")
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, mine["id"])["status"] == "waiting_for_event"
    assert queue.get(OTHER, theirs["id"])["event_key"] == "deploy-done"
    assert queue.get(OWNER, mine["id"])["deadline"] is None
    with pytest.raises(TaskError):
        queue.resume(OTHER, theirs["id"], "a")  # event tasks wake via fire_event
    assert queue.fire_event("deploy-done", payload="ok") == 2
    assert queue.fire_event("deploy-done") == 0
    assert queue.fire_event("never-seen") == 0
    with pytest.raises(TaskError):
        queue.fire_event("")
    assert queue.get(OWNER, mine["id"])["status"] == "queued"
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, mine["id"])["result"] == "resumed event 'ok'"
    assert queue.get(OTHER, theirs["id"])["result"] == "resumed event 'ok'"


def test_event_deadline_timeout(tmp_path):
    now = [1000.0]
    def runner(task):
        if task["resume"] is None:
            return WaitForEvent("late", deadline=now[0] + 5, note="hold")
        return "woke: %s %r" % (task["resume"]["wake_reason"], task["resume"]["payload"])
    queue = TaskQueue(tmp_path / "q.sqlite3", runner, clock=lambda: now[0])
    task = queue.submit(OWNER, "wait for timeout")
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, task["id"])["status"] == "waiting_for_event"
    assert queue.get(OWNER, task["id"])["deadline"] == now[0] + 5
    queue.poll()  # before the deadline the task keeps waiting
    finish(queue)
    assert queue.get(OWNER, task["id"])["status"] == "waiting_for_event"
    now[0] += 10
    queue.poll()  # the deadline passed: poll moves the task back to queued
    finish(queue)
    done = queue.get(OWNER, task["id"])
    assert done["status"] == "done"
    assert done["event_key"] is None
    assert done["result"] == "woke: timeout None"


def test_cancel_while_waiting(tmp_path):
    queue = TaskQueue(tmp_path / "q.sqlite3",
                      lambda task: WaitForUser("q?") if task["prompt"] == "user" else WaitForEvent("evt"))
    user_task = queue.submit(OWNER, "user")
    event_task = queue.submit(OWNER, "event")
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, user_task["id"])["status"] == "waiting_for_user"
    # A single owner runs one task at a time, so the second task waits for the
    # next poll; the paused first task does not keep the owner busy.
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, event_task["id"])["status"] == "waiting_for_event"
    assert queue.cancel(OWNER, user_task["id"])["status"] == "cancelled"
    assert queue.get(OWNER, user_task["id"])["waiting_question"] is None
    assert queue.get(OWNER, event_task["id"])["status"] == "waiting_for_event"
    assert queue.cancel(OWNER, event_task["id"])["status"] == "cancelled"
    assert queue.fire_event("evt") == 0  # a cancelled task no longer waits
    assert queue.get(OWNER, event_task["id"])["status"] == "cancelled"


def test_waiting_tasks_hold_no_slot_and_do_not_block_owner(tmp_path):
    release = threading.Event()
    def runner(task):
        if task["prompt"] == "wait" and task["resume"] is None:
            return WaitForUser("confirm?")
        if task["prompt"] == "long":
            assert release.wait(5)
            return "long result"
        return "resumed"
    queue = TaskQueue(tmp_path / "q.sqlite3", runner, max_workers=1, max_pending=5)
    waiter = queue.submit(OWNER, "wait")
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, waiter["id"])["status"] == "waiting_for_user"
    with queue._lock:
        assert queue._threads == {}
    # The waiting task does not block the same owner's next task.
    blocker = queue.submit(OWNER, "long")
    queue.poll()
    try:
        assert queue.get(OWNER, blocker["id"])["status"] == "running"
        assert queue.resume(OWNER, waiter["id"], "yes")["status"] == "queued"
        # The resumed task keeps queuing behind the occupied worker slot.
        assert queue.get(OWNER, waiter["id"])["status"] == "queued"
        with pytest.raises(TaskError):
            queue.resume(OWNER, waiter["id"], "again")
    finally:
        release.set()
        finish(queue)
    assert queue.get(OWNER, blocker["id"])["status"] == "done"
    queue.poll()
    finish(queue)
    resumed = queue.get(OWNER, waiter["id"])
    assert resumed["status"] == "done" and resumed["result"] == "resumed"


def test_waiting_tasks_do_not_count_toward_pending_limit(tmp_path):
    queue = TaskQueue(tmp_path / "q.sqlite3", lambda _: WaitForUser("q?"), max_pending=1)
    first = queue.submit(OWNER, "one")
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, first["id"])["status"] == "waiting_for_user"
    # The waiting task consumes no pending capacity.
    second = queue.submit(OWNER, "two")
    assert queue.get(OWNER, second["id"])["status"] == "queued"


def test_waiting_tasks_survive_restart(tmp_path):
    calls = []
    def runner(task):
        calls.append(task["prompt"])
        if task["resume"] is None:
            return WaitForUser("confirm restart?")
        return "restarted with " + str(task["resume"]["answer"])
    path = tmp_path / "q.sqlite3"
    queue = TaskQueue(path, runner)
    task = queue.submit(OWNER, "long lived")
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, task["id"])["status"] == "waiting_for_user"
    recovered = TaskQueue(path, runner)
    # Startup recovery marks only running tasks interrupted, never waiting ones...
    assert recovered.get(OWNER, task["id"])["status"] == "waiting_for_user"
    recovered.poll()
    # ...and waiting tasks are not replayed.
    assert recovered.get(OWNER, task["id"])["status"] == "waiting_for_user"
    assert calls == ["long lived"]
    assert recovered.resume(OWNER, task["id"], "yes")["status"] == "queued"
    recovered.poll()
    finish(recovered)
    assert recovered.get(OWNER, task["id"])["result"] == "restarted with yes"
    assert calls == ["long lived", "long lived"]


def test_waiting_tasks_survive_pruning(tmp_path):
    queue = TaskQueue(tmp_path / "q.sqlite3", lambda _: WaitForEvent("keep-me"))
    waiter = queue.submit(OWNER, "waiter")
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, waiter["id"])["status"] == "waiting_for_event"
    with sqlite3.connect(queue.path) as db:
        db.executemany(
            "INSERT INTO tasks (id, owner, prompt, status, created) VALUES (?, ?, ?, 'done', ?)",
            [("old-%d" % index, OWNER, "old prompt", 1.0) for index in range(150)])
    queue.submit(OWNER, "after prune")  # submit runs its pruning DELETE
    # The waiting task is far older than 100 newer finished tasks yet is kept.
    assert queue.get(OWNER, waiter["id"])["status"] == "waiting_for_event"
    with sqlite3.connect(queue.path) as db:
        remaining = db.execute("SELECT COUNT(*) FROM tasks WHERE owner=?", (OWNER,)).fetchone()[0]
    # The waiting task is kept on top of the 100 newest rows, and the freshly
    # queued task is itself part of that newest 100.
    assert remaining == 101


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE tasks (
                sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                owner TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, started REAL, finished REAL,
                result TEXT, error TEXT
            )""")
        db.execute("CREATE INDEX tasks_owner ON tasks(owner, sequence)")
        db.execute("INSERT INTO tasks (id, owner, prompt, status, created) VALUES (?, ?, ?, 'done', ?)",
                   ("legacy", OWNER, "legacy prompt", 42.0))
    def runner(task):
        if task["resume"] is None:
            return WaitForUser("migration question")
        return "migrated resume"
    queue = TaskQueue(path, runner)
    # The old table gained the new columns and keeps its existing rows.
    legacy = queue.get(OWNER, "legacy")
    assert legacy["status"] == "done"
    assert legacy["waiting_question"] is None
    assert legacy["event_key"] is None and legacy["deadline"] is None
    # Waiting and resume flows work on the migrated schema.
    task = queue.submit(OWNER, "new task")
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, task["id"])["waiting_question"] == "migration question"
    assert queue.resume(OWNER, task["id"], "yes")["status"] == "queued"
    queue.poll()
    finish(queue)
    assert queue.get(OWNER, task["id"])["result"] == "migrated resume"


def wait_status(queue, owner, task_id, status):
    deadline = time.time() + 5
    while queue.get(owner, task_id)["status"] != status and time.time() < deadline:
        time.sleep(0.01)
    assert queue.get(owner, task_id)["status"] == status


@pytest.mark.parametrize("outcome", [WaitForUser(""), WaitForUser(None), WaitForEvent(""),
                                     WaitForEvent("key", deadline="soon"),
                                     WaitForEvent("key", deadline=float("nan"))])
def test_malformed_wait_fails_instead_of_staying_running(tmp_path, outcome):
    owner = "a" * 32
    queue = TaskQueue(tmp_path / "t.sqlite3", lambda task: outcome)
    task = queue.submit(owner, "work")
    queue.poll()
    wait_status(queue, owner, task["id"], "failed")


@pytest.mark.parametrize("answer", ["", "   ", None, 5, "x" * 8001])
def test_resume_rejects_bad_answers_and_keeps_waiting(tmp_path, answer):
    owner = "a" * 32
    queue = TaskQueue(tmp_path / "t.sqlite3", lambda task: WaitForUser("Which?"))
    task = queue.submit(owner, "work")
    queue.poll()
    wait_status(queue, owner, task["id"], "waiting_for_user")
    with pytest.raises(TaskError):
        queue.resume(owner, task["id"], answer)
    assert queue.get(owner, task["id"])["status"] == "waiting_for_user"


def test_fire_event_rejects_a_non_text_payload(tmp_path):
    queue = TaskQueue(tmp_path / "t.sqlite3", lambda task: "ok")
    with pytest.raises(TaskError):
        queue.fire_event("key", {"a": 1})
