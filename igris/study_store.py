"""Owner-scoped SQLite study plans, problems and progress.

The current exam is the earliest dated exam. JSON fields remain JSON-compatible;
getters return detached copies. Completed steps survive plan replacements.
"""

from contextlib import closing, contextmanager
from datetime import date as date_type, datetime
import json
from pathlib import Path
import secrets
import sqlite3
import threading


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-empty text")
    return value.strip()


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _moment(value):
    value = _text(value, "time")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("Study step times need a time zone offset")
    return moment


class StudyStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._transaction_db = None
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS exams (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    date TEXT NOT NULL, doc_ids TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS exams_owner ON exams(owner, date);
                CREATE TABLE IF NOT EXISTS topics (
                    owner TEXT NOT NULL, exam_id TEXT NOT NULL, name TEXT NOT NULL,
                    position INTEGER NOT NULL, requires TEXT NOT NULL,
                    PRIMARY KEY(owner, exam_id, name));
                CREATE TABLE IF NOT EXISTS problems (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, exam_id TEXT NOT NULL,
                    topic TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS problems_owner ON problems(owner, exam_id);
                CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, exam_id TEXT NOT NULL,
                    position INTEGER NOT NULL, done INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS steps_owner ON steps(owner, exam_id);
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, problem_id TEXT NOT NULL,
                    verdict TEXT NOT NULL, corrected INTEGER NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_owner ON attempts(owner, problem_id);
                CREATE TABLE IF NOT EXISTS mastery (
                    owner TEXT NOT NULL, exam_id TEXT NOT NULL, topic TEXT NOT NULL,
                    level INTEGER NOT NULL, PRIMARY KEY(owner, exam_id, topic));
            """)

    @contextmanager
    def _db(self):
        with self._lock:
            if self._transaction_db is not None:
                yield self._transaction_db
                return
            with closing(sqlite3.connect(self.path, timeout=10)) as db:
                db.row_factory = sqlite3.Row
                with db:
                    yield db

    @contextmanager
    def transaction(self):
        """Group store operations, rolling the complete batch back on failure."""
        with self._db() as db:
            previous = self._transaction_db
            self._transaction_db = db
            try:
                yield
            finally:
                self._transaction_db = previous

    @staticmethod
    def _get(db, table, owner, identifier):
        _text(owner, "owner")
        _text(identifier, "id")
        row = db.execute(f"SELECT * FROM {table} WHERE owner=? AND id=?",
                         (owner, identifier)).fetchone()
        if row is None:
            raise KeyError(identifier)
        return row

    def create_exam(self, owner, name, date, doc_ids):
        _text(owner, "owner")
        name = _text(name, "name")
        if not isinstance(date, str) or date_type.fromisoformat(date).isoformat() != date:
            raise ValueError("date must be YYYY-MM-DD")
        if not isinstance(doc_ids, list) or any(not isinstance(d, str) or not d for d in doc_ids):
            raise ValueError("doc_ids must be a list of document ids")
        identifier = secrets.token_hex(16)
        with self._db() as db:
            db.execute("INSERT INTO exams VALUES(?,?,?,?,?)",
                       (identifier, owner, name, date, _json(doc_ids)))
        return identifier

    def set_topics(self, owner, exam_id, topics):
        with self._db() as db:
            self._get(db, "exams", owner, exam_id)
            if not isinstance(topics, list):
                raise ValueError("topics must be a list")
            cleaned = []
            for topic in topics:
                if not isinstance(topic, dict):
                    raise ValueError("Each topic must be an object")
                name = _text(topic.get("name"), "topic name")
                order, requires = topic.get("order"), topic.get("requires")
                if type(order) is not int or order < 0:
                    raise ValueError("Topic order must be a non-negative integer")
                if not isinstance(requires, list) or any(not isinstance(r, str) for r in requires):
                    raise ValueError("requires must be a list of topic names")
                cleaned.append((name, order, requires))
            names = {t[0] for t in cleaned}
            if len(names) != len(cleaned):
                raise ValueError("Topic names must be unique")
            graph = {name: requires for name, _, requires in cleaned}
            visited, active = set(), set()

            def visit(name):
                if name not in names:
                    raise ValueError("A prerequisite names a missing topic")
                if name in active:
                    raise ValueError("Topic prerequisites must not contain a cycle")
                if name in visited:
                    return
                active.add(name)
                for requirement in graph[name]:
                    visit(requirement)
                active.remove(name)
                visited.add(name)

            for name in names:
                visit(name)
            db.execute("DELETE FROM topics WHERE owner=? AND exam_id=?", (owner, exam_id))
            db.executemany("INSERT INTO topics VALUES(?,?,?,?,?)",
                           [(owner, exam_id, n, order, _json(req)) for n, order, req in cleaned])

    def add_problem(self, owner, exam_id, topic, statement, *, givens=None, answer=None,
                    generated=False):
        with self._db() as db:
            self._get(db, "exams", owner, exam_id)
            topic, statement = _text(topic, "topic"), _text(statement, "statement")
            if type(generated) is not bool:
                raise ValueError("generated must be a boolean")
            if any(v is not None and not isinstance(v, dict) for v in (givens, answer)):
                raise ValueError("givens and answer must be objects or null")
            data = _json({"statement": statement, "givens": givens, "answer": answer,
                          "generated": generated})
            identifier = secrets.token_hex(16)
            db.execute("INSERT INTO problems VALUES(?,?,?,?,?)",
                       (identifier, owner, exam_id, topic, data))
        return identifier

    def set_steps(self, owner, exam_id, steps):
        with self._db() as db:
            self._get(db, "exams", owner, exam_id)
            if not isinstance(steps, list):
                raise ValueError("steps must be a list")
            cleaned, seen = [], set()
            for step in steps:
                if not isinstance(step, dict):
                    raise ValueError("Each step must be an object")
                item = {key: _text(step.get(key), key) for key in ("topic", "goal", "start", "end")}
                if "title" in step:
                    if not isinstance(step["title"], str) or len(step["title"]) > 80:
                        raise ValueError("title must be text of at most 80 characters")
                    item["title"] = step["title"]
                if _moment(item["end"]) <= _moment(item["start"]):
                    raise ValueError("end must be after start")
                item["agenda_id"] = step.get("agenda_id")
                if item["agenda_id"] is not None and not isinstance(item["agenda_id"], str):
                    raise ValueError("agenda_id must be text or null")
                identifier = step.get("id")
                if identifier is not None:
                    old = self._get(db, "steps", owner, identifier)
                    if old["exam_id"] != exam_id:
                        raise KeyError(identifier)
                    if identifier in seen:
                        raise ValueError("Duplicate step id")
                    seen.add(identifier)
                    if old["done"]:
                        continue
                cleaned.append((identifier or secrets.token_hex(16), item))
            db.execute("DELETE FROM steps WHERE owner=? AND exam_id=? AND done=0", (owner, exam_id))
            offset = db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM steps WHERE owner=? AND exam_id=?",
                                (owner, exam_id)).fetchone()[0]
            db.executemany("INSERT INTO steps VALUES(?,?,?,?,?,?)",
                           [(sid, owner, exam_id, offset + n, 0, _json(data))
                            for n, (sid, data) in enumerate(cleaned)])

    def step_done(self, owner, step_id):
        with self._db() as db:
            self._get(db, "steps", owner, step_id)
            db.execute("UPDATE steps SET done=1 WHERE owner=? AND id=?", (owner, step_id))

    def record_attempt(self, owner, problem_id, lines, verdict, line, reason, *,
                       should_be="", verified=False):
        with self._db() as db:
            self._get(db, "problems", owner, problem_id)
            if not isinstance(lines, list) or not lines or any(not isinstance(s, str) for s in lines):
                raise ValueError("lines must be a non-empty list of text")
            if verdict not in ("right", "wrong", "unsure"):
                raise ValueError("Invalid verdict")
            if line is not None and (type(line) is not int or not 1 <= line <= len(lines)):
                raise ValueError("line must name a submitted line")
            if verdict == "wrong" and line is None:
                raise ValueError("A wrong attempt needs its first wrong line")
            if not isinstance(reason, str):
                raise ValueError("reason must be text")
            if not isinstance(should_be, str) or type(verified) is not bool:
                raise ValueError("should_be must be text and verified must be boolean")
            identifier = secrets.token_hex(16)
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?)", (
                identifier, owner, problem_id, verdict, 0,
                _json({"lines": lines, "line": line, "reason": reason,
                       "should_be": should_be, "verified": verified})))
        return identifier

    def mark_corrected(self, owner, attempt_id):
        with self._db() as db:
            row = self._get(db, "attempts", owner, attempt_id)
            if row["verdict"] != "wrong":
                raise ValueError("Only a wrong attempt can be marked corrected")
            db.execute("UPDATE attempts SET corrected=1 WHERE owner=? AND id=?", (owner, attempt_id))

    def set_mastery(self, owner, exam_id, topic, level):
        with self._db() as db:
            self._get(db, "exams", owner, exam_id)
            topic = _text(topic, "topic")
            if type(level) is not int or not 0 <= level <= 3:
                raise ValueError("level must be an integer from 0 to 3")
            db.execute("INSERT INTO mastery VALUES(?,?,?,?) ON CONFLICT(owner,exam_id,topic) "
                       "DO UPDATE SET level=excluded.level", (owner, exam_id, topic, level))

    @staticmethod
    def _decode(row):
        result = dict(row)
        result.pop("owner", None)
        if "data" in result:
            result.update(json.loads(result.pop("data")))
        if "done" in result:
            result["done"] = bool(result["done"])
            result["state"] = "done" if result["done"] else "planned"
        if "corrected" in result:
            result["corrected"] = bool(result["corrected"])
        return result

    def _exam(self, db, row):
        result = self._decode(row)
        result["doc_ids"] = json.loads(result["doc_ids"])
        result["topics"] = [{"name": t["name"], "order": t["position"],
                              "requires": json.loads(t["requires"])} for t in db.execute(
            "SELECT * FROM topics WHERE owner=? AND exam_id=? ORDER BY position, rowid",
            (row["owner"], row["id"]))]
        result["problems"] = [self._decode(p) for p in db.execute(
            "SELECT * FROM problems WHERE owner=? AND exam_id=? ORDER BY rowid",
            (row["owner"], row["id"]))]
        return result

    def get_exam(self, owner, exam_id):
        with self._db() as db:
            return self._exam(db, self._get(db, "exams", owner, exam_id))

    def get_problem(self, owner, problem_id):
        with self._db() as db:
            return self._decode(self._get(db, "problems", owner, problem_id))

    def get_attempt(self, owner, attempt_id):
        with self._db() as db:
            return self._decode(self._get(db, "attempts", owner, attempt_id))

    def get_step(self, owner, step_id):
        with self._db() as db:
            return self._decode(self._get(db, "steps", owner, step_id))

    def attempts(self, owner, problem_id):
        """Read this problem's attempts in submission order for correction tracking."""
        with self._db() as db:
            self._get(db, "problems", owner, problem_id)
            return [self._decode(row) for row in db.execute(
                "SELECT * FROM attempts WHERE owner=? AND problem_id=? ORDER BY rowid", (owner, problem_id))]

    def steps(self, owner, exam_id):
        with self._db() as db:
            self._get(db, "exams", owner, exam_id)
            return [self._decode(row) for row in db.execute(
                "SELECT * FROM steps WHERE owner=? AND exam_id=? ORDER BY position, rowid", (owner, exam_id))]

    def overview(self, owner):
        _text(owner, "owner")
        with self._db() as db:
            exams = [self._exam(db, row) for row in db.execute(
                "SELECT * FROM exams WHERE owner=? ORDER BY date, rowid", (owner,))]
            if not exams:
                return {"exams": [], "current": None}
            exam = exams[0]
            levels = {r["topic"]: r["level"] for r in db.execute(
                "SELECT topic, level FROM mastery WHERE owner=? AND exam_id=?", (owner, exam["id"]))}
            counts = {r["topic"]: dict(r) for r in db.execute("""
                SELECT p.topic, COUNT(a.id) AS attempts,
                       SUM(CASE WHEN a.verdict='wrong' THEN 1 ELSE 0 END) AS mistakes,
                       SUM(COALESCE(a.corrected,0)) AS corrected
                FROM problems p LEFT JOIN attempts a ON a.problem_id=p.id AND a.owner=p.owner
                WHERE p.owner=? AND p.exam_id=? GROUP BY p.topic
            """, (owner, exam["id"]))}
            topics = list(dict.fromkeys([t["name"] for t in exam["topics"]] + list(counts) + list(levels)))
            mastery = [dict({"topic": topic, "level": levels.get(topic, 0)},
                            **{key: counts.get(topic, {}).get(key, 0)
                               for key in ("attempts", "mistakes", "corrected")}) for topic in topics]
            steps = [self._decode(row) for row in db.execute(
                "SELECT * FROM steps WHERE owner=? AND exam_id=? ORDER BY position, rowid", (owner, exam["id"]))]
            return {"exams": exams, "current": {"exam": exam, "steps": steps, "mastery": mastery}}
