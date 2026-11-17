"""Persistent user-owned text tasks with bounded background execution."""

from contextlib import closing
from dataclasses import dataclass
import math
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time


class TaskError(ValueError):
    """Invalid task input or exhausted queue capacity."""


class TaskMissing(TaskError):
    """A task does not exist for this owner."""


@dataclass(frozen=True)
class WaitForUser:
    """Runner outcome that pauses a task until the user supplies an answer."""

    question: str


@dataclass(frozen=True)
class WaitForEvent:
    """Runner outcome that pauses a task until an external event fires.

    When deadline passes, poll() wakes the task with wake_reason 'timeout'.
    """

    event_key: str
    deadline: float | None = None
    note: str = ""


class TaskQueue:
    def __init__(self, path: Path, runner, *, max_workers=2, max_pending=10, clock=time.time):
        if max_workers < 1 or max_pending < 1:
            raise ValueError("Queue limits must be positive")
        self.path, self.runner = Path(path), runner
        self.max_workers, self.max_pending, self.clock = max_workers, max_pending, clock
        self._lock = threading.RLock()
        self._threads = {}
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    sequence INTEGER PRIMARY KEY, id TEXT UNIQUE NOT NULL,
                    owner TEXT NOT NULL, prompt TEXT NOT NULL, status TEXT NOT NULL,
                    created REAL NOT NULL, started REAL, finished REAL,
                    result TEXT, error TEXT, waiting_question TEXT,
                    event_key TEXT, deadline REAL,
                    resume_answer TEXT, wake_payload TEXT, wake_reason TEXT
                );
                CREATE INDEX IF NOT EXISTS tasks_owner ON tasks(owner, sequence);
            """)
            # Databases created by older versions gain the waiting columns here.
            self._add_missing_columns(db)
            # There is one queue-owning server process. Never replay interrupted model calls.
            db.execute("UPDATE tasks SET status='interrupted', finished=?, "
                       "error='Server stopped before the task completed' "
                       "WHERE status IN ('running', 'cancel_requested')", (self.clock(),))

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _add_missing_columns(db):
        """Guarded ALTER TABLE migration for databases from older versions."""
        existing = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
        for name, declaration in (
            ("waiting_question", "TEXT"), ("event_key", "TEXT"), ("deadline", "REAL"),
            ("resume_answer", "TEXT"), ("wake_payload", "TEXT"), ("wake_reason", "TEXT"),
        ):
            if name not in existing:
                db.execute(f"ALTER TABLE tasks ADD COLUMN {name} {declaration}")

    @staticmethod
    def _owner(owner):
        if not isinstance(owner, str) or not re.fullmatch(r"[a-f0-9]{32}", owner):
            raise TaskError("Invalid owner")

    def submit(self, owner, prompt):
        self._owner(owner)
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 8000:
            raise TaskError("Task must contain 1 to 8000 characters")
        task_id = secrets.token_hex(16)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM tasks WHERE owner=? "
                               "AND status IN ('queued', 'running', 'cancel_requested')", (owner,)).fetchone()[0]
            if count >= self.max_pending:
                raise TaskError("User task queue is full")
            db.execute("INSERT INTO tasks (id, owner, prompt, status, created) "
                       "VALUES (?, ?, ?, 'queued', ?)", (task_id, owner, prompt, self.clock()))
            # Pruning never removes tasks that are waiting for a user or event.
            db.execute("DELETE FROM tasks WHERE owner=? AND status NOT IN "
                       "('queued','running','cancel_requested','waiting_for_user','waiting_for_event') AND sequence NOT IN "
                       "(SELECT sequence FROM tasks WHERE owner=? ORDER BY sequence DESC LIMIT 100)",
                       (owner, owner))
        return self.get(owner, task_id)

    def get(self, owner, task_id):
        self._owner(owner)
        if not isinstance(task_id, str):
            raise TaskMissing("Task not found")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM tasks WHERE owner=? AND id=?", (owner, task_id)).fetchone()
        if row is None:
            raise TaskMissing("Task not found")
        return dict(row)

    def list(self, owner):
        self._owner(owner)
        with closing(self._connect()) as db:
            return [dict(row) for row in db.execute(
                "SELECT id, prompt, status, created, started, finished, "
                "waiting_question, event_key, deadline FROM tasks "
                "WHERE owner=? ORDER BY sequence DESC LIMIT 110", (owner,))]

    def cancel(self, owner, task_id):
        self.get(owner, task_id)
        with closing(self._connect()) as db, db:
            db.execute("UPDATE tasks SET status='cancelled', finished=?, "
                       "waiting_question=NULL, event_key=NULL, deadline=NULL "
                       "WHERE owner=? AND id=? AND status IN "
                       "('queued', 'waiting_for_user', 'waiting_for_event')", (self.clock(), owner, task_id))
            db.execute("UPDATE tasks SET status='cancel_requested' "
                       "WHERE owner=? AND id=? AND status='running'", (owner, task_id))
        return self.get(owner, task_id)

    def resume(self, owner, task_id, answer):
        """Move a waiting_for_user task back to the queue with the user's answer."""
        self.get(owner, task_id)
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 8000:
            raise TaskError("Answer must contain 1 to 8000 characters")
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "UPDATE tasks SET status='queued', finished=NULL, waiting_question=NULL, "
                "event_key=NULL, deadline=NULL, resume_answer=?, wake_payload=NULL, "
                "wake_reason='resume' WHERE owner=? AND id=? AND status='waiting_for_user'",
                (answer, owner, task_id))
            if cursor.rowcount != 1:
                raise TaskError("Task is not waiting for user input")
        return self.get(owner, task_id)

    def fire_event(self, event_key, payload=""):
        """Wake every task waiting for event_key and return how many moved.

        This crosses owners on purpose, so it is for trusted server code only.
        Never pass it a key taken from a request; such keys must be unguessable
        (an approval id, for example).
        """
        if not isinstance(event_key, str) or not event_key:
            raise TaskError("Event key must be a non-empty string")
        if not isinstance(payload, str):
            raise TaskError("Event payload must be a string")
        with closing(self._connect()) as db, db:
            cursor = db.execute(
                "UPDATE tasks SET status='queued', finished=NULL, event_key=NULL, deadline=NULL, "
                "resume_answer=NULL, wake_payload=?, wake_reason='event' "
                "WHERE status='waiting_for_event' AND event_key=?", (payload, event_key))
            return cursor.rowcount

    def poll(self):
        with self._lock:
            if self._closed:
                return
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                # A deadline passing wakes waiting_for_event tasks; this happens
                # even when every worker slot is currently occupied.
                db.execute("UPDATE tasks SET status='queued', finished=NULL, event_key=NULL, deadline=NULL, "
                           "resume_answer=NULL, wake_payload=NULL, wake_reason='timeout' "
                           "WHERE status='waiting_for_event' AND deadline IS NOT NULL AND deadline<=?",
                           (self.clock(),))
                available = self.max_workers - len(self._threads)
                if available <= 0:
                    return
                busy = {row[0] for row in db.execute(
                    "SELECT owner FROM tasks WHERE status IN ('running', 'cancel_requested')")}
                selected = []
                for row in db.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY sequence"):
                    if row["owner"] in busy:
                        continue
                    selected.append(dict(row))
                    busy.add(row["owner"])
                    if len(selected) >= available:
                        break
                for task in selected:
                    db.execute("UPDATE tasks SET status='running', started=? WHERE id=?",
                               (self.clock(), task["id"]))
            for task in selected:
                thread = threading.Thread(target=self._run, args=(task,), daemon=True)
                self._threads[task["id"]] = thread
                thread.start()

    @staticmethod
    def _check_wait(outcome):
        """Reject a malformed wait here: failing at the UPDATE would leave the task running forever."""
        if isinstance(outcome, WaitForUser):
            valid = isinstance(outcome.question, str) and outcome.question.strip()
        else:
            deadline = outcome.deadline
            valid = (isinstance(outcome.event_key, str) and outcome.event_key
                     and (deadline is None or (isinstance(deadline, (int, float))
                                               and not isinstance(deadline, bool)
                                               and math.isfinite(deadline))))
        if not valid:
            raise TaskError("Invalid wait outcome")

    def _run(self, task):
        result, error, waiting = None, None, None
        try:
            if self.get(task["owner"], task["id"])["status"] != "cancel_requested":
                # Wake information recorded on the row becomes the resume context.
                resume = None
                if task["wake_reason"] is not None:
                    resume = {"answer": task["resume_answer"], "payload": task["wake_payload"],
                              "wake_reason": task["wake_reason"]}
                context = dict(task, resume=resume, is_cancelled=lambda: self.get(task["owner"], task["id"])["status"] == "cancel_requested")
                outcome = self.runner(context)
                if isinstance(outcome, (WaitForUser, WaitForEvent)):
                    self._check_wait(outcome)
                    waiting = outcome
                else:
                    result = str(outcome)
                    if len(result) > 100000:
                        result = result[:100000] + "\n[Output truncated]"
        except Exception:
            error = "Task could not be completed; check the configured model connection"
        try:
            with closing(self._connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT status FROM tasks WHERE id=?", (task["id"],)).fetchone()
                cancelled = row[0] == "cancel_requested"
                if cancelled:
                    db.execute("UPDATE tasks SET status='cancelled', result=NULL, error=NULL, finished=?, "
                               "waiting_question=NULL, event_key=NULL, deadline=NULL WHERE id=?",
                               (self.clock(), task["id"]))
                elif waiting is not None:
                    # The task pauses durably: no result, no finished time and
                    # no worker slot, and it stays resumable after a restart.
                    if isinstance(waiting, WaitForUser):
                        db.execute("UPDATE tasks SET status='waiting_for_user', result=NULL, error=NULL, finished=NULL, "
                                   "waiting_question=?, event_key=NULL, deadline=NULL, "
                                   "resume_answer=NULL, wake_payload=NULL, wake_reason=NULL WHERE id=?",
                                   (waiting.question, task["id"]))
                    else:
                        db.execute("UPDATE tasks SET status='waiting_for_event', result=NULL, error=NULL, finished=NULL, "
                                   "waiting_question=NULL, event_key=?, deadline=?, "
                                   "resume_answer=NULL, wake_payload=NULL, wake_reason=NULL WHERE id=?",
                                   (waiting.event_key, waiting.deadline, task["id"]))
                else:
                    status = "failed" if error else "done"
                    db.execute("UPDATE tasks SET status=?, result=?, error=?, finished=?, "
                               "waiting_question=NULL, event_key=NULL, deadline=NULL WHERE id=?",
                               (status, result, error, self.clock(), task["id"]))
        finally:
            with self._lock:
                self._threads.pop(task["id"], None)

    def close(self):
        """Stop dispatching; running calls finish within their backend timeout."""
        with self._lock:
            self._closed = True
