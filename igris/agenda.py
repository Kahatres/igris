"""Task calendar items (Görev Takvimi): exams, assignments, meetings and the like, per owner.

Items come from Moodle, school Outlook, Gmail, the ESN calendar or manual entry. A non-empty
source_ref (Moodle UID, mail message id, ESN event id) makes every sync idempotent: the same
item is stored once, and a changed date updates it while keeping its status and calendar event.
"""

from contextlib import closing
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

BRANCHES = ("kisisel", "okul", "esn")
KINDS = ("exam", "assignment", "lab", "presentation", "attendance", "payment", "meeting",
         "lesson", "study", "other")
LESSON = "lesson"  # a class from an imported timetable (agenda_import's default kind)
SOURCES = ("moodle", "outlook", "gmail", "esn_calendar", "manual")
STATUSES = ("proposed", "added", "done", "dismissed")
ACTIVE = ("proposed", "added")
SECTIONS = ("overdue", "today", "week", "later")
MAX_TITLE, MAX_TEXT = 200, 300
# source_ref prefix of manual items that came from a timetable import (agenda_import); its
# lessons, once over, leave the sections instead of becoming overdue
IMPORT_PREFIX = "import:"
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# Keyword stems per kind, checked in this order (an "exam" wins over an "assignment").
# English, Turkish and Lithuanian (university timetable) words; matched in lower case.
KIND_WORDS = (
    ("exam", ("exam", "quiz", "midterm", "final", "finals", "sınav", "sinav", "vize", "egzamin", "koliokvium",
              "kontrolin")),
    ("lab", ("lab", "labs", "laborator", "laboratuvar")),
    ("presentation", ("presentation", "sunum", "pristatym")),
    ("attendance", ("attendance", "yoklama", "lankomum")),
    ("payment", ("payment", "ödeme", "kira", "fatura", "mokėjim")),
    ("assignment", ("assignment", "homework", "ödev", "teslim", "due", "deadline",
                    "submission", "užduot")),
)


_LETTER = "a-zçğıöşüėįųūčšž"


# stems that are prefixes of unrelated words must stand alone ("lab" is not "label");
# every other stem may start a longer word, so Turkish suffixes still match ("sınavı")
_WHOLE_WORDS = {"lab", "labs", "due", "kira", "final", "finals"}


def _stem_re(stem):
    tail = f"(?![{_LETTER}])" if stem in _WHOLE_WORDS else ""
    return re.compile(f"(?<![{_LETTER}])" + re.escape(stem) + tail)


_KIND_RES = tuple((kind, tuple(_stem_re(s) for s in stems)) for kind, stems in KIND_WORDS)


def kind_from_text(*texts, default="other"):
    """The agenda kind a title (or category) names, or ``default`` when no keyword matches."""
    words = " ".join(t for t in texts if isinstance(t, str)).lower()
    for kind, patterns in _KIND_RES:
        if any(p.search(words) for p in patterns):
            return kind
    return default


class AgendaError(ValueError):
    """Invalid agenda input."""


class AgendaMissing(AgendaError):
    """An item does not exist for this owner."""


def _clean(value, limit, field, *, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AgendaError(f"{field} must be text")
    value = " ".join(_CONTROL.sub(" ", value).split())[:limit]
    if required and not value:
        raise AgendaError(f"{field} must not be empty")
    return value


def _when(value, field):
    """An ISO 8601 date (all-day) or a date-time with an offset; None stays None."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise AgendaError(f"{field} must be an ISO 8601 date or date-time")
    try:
        if _DATE.fullmatch(value):
            return date.fromisoformat(value).isoformat()
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise AgendaError(f"{field} must be an ISO 8601 date or date-time") from None
    if moment.tzinfo is None:
        raise AgendaError(f"{field} needs a time zone offset")
    return value


def _moment(value, tz):
    """The instant an item counts from; a bare date is the start of that day in tz."""
    if _DATE.fullmatch(value):
        return datetime.combine(date.fromisoformat(value), dtime(), tz)
    return datetime.fromisoformat(value).astimezone(tz)


def _local_tz():
    return datetime.now().astimezone().tzinfo


class AgendaStore:
    def __init__(self, path: Path, *, clock=time.time, on_change=None):
        self.path, self.clock = Path(path), clock
        # called with the owner after every upsert, status change or delete (the Google
        # Calendar sync listens here); its errors never reach the caller
        self.on_change = on_change
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, branch TEXT NOT NULL,
                    kind TEXT NOT NULL, title TEXT NOT NULL, start TEXT, due TEXT,
                    all_day INTEGER NOT NULL, location TEXT NOT NULL, source TEXT NOT NULL,
                    source_ref TEXT NOT NULL, mandatory INTEGER NOT NULL, confidence REAL NOT NULL,
                    status TEXT NOT NULL, calendar_event_id TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS items_source
                    ON items(owner, source, source_ref) WHERE source_ref != '';
                CREATE INDEX IF NOT EXISTS items_owner ON items(owner, branch);
                CREATE INDEX IF NOT EXISTS items_owner_due ON items(owner, due);
            """)

    def _changed(self, owner):
        if self.on_change is not None:
            try:
                self.on_change(owner)
            except Exception:
                pass

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _owner(owner):
        if not isinstance(owner, str) or not owner or len(owner) > 64:
            raise AgendaError("Invalid owner")

    @staticmethod
    def _choice(value, allowed, field):
        if value not in allowed:
            raise AgendaError(f"{field} must be one of: {', '.join(allowed)}")
        return value

    @staticmethod
    def _row(row):
        item = dict(row)
        item["all_day"] = bool(item["all_day"])
        item["mandatory"] = bool(item["mandatory"])
        del item["owner"]
        return item

    def _validate(self, item):
        if not isinstance(item, dict):
            raise AgendaError("Item must be an object")
        start, due = _when(item.get("start"), "start"), _when(item.get("due"), "due")
        if start is None and due is None:
            raise AgendaError("An item needs a start or a due date")
        all_day = item.get("all_day")
        if all_day is None:
            all_day = all(v is None or _DATE.fullmatch(v) for v in (start, due))
        mandatory, confidence = item.get("mandatory", False), item.get("confidence", 1.0)
        if not isinstance(all_day, bool) or not isinstance(mandatory, bool):
            raise AgendaError("all_day and mandatory must be booleans")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1):
            raise AgendaError("confidence must be a number from 0 to 1")
        event = item.get("calendar_event_id")
        if event is not None and not isinstance(event, str):
            raise AgendaError("calendar_event_id must be text")
        return {
            "branch": self._choice(item.get("branch"), BRANCHES, "branch"),
            "kind": self._choice(item.get("kind", "other"), KINDS, "kind"),
            "title": _clean(item.get("title"), MAX_TITLE, "title", required=True),
            "start": start, "due": due, "all_day": int(all_day),
            "location": _clean(item.get("location"), MAX_TEXT, "location"),
            "source": self._choice(item.get("source"), SOURCES, "source"),
            "source_ref": _clean(item.get("source_ref"), MAX_TEXT, "source_ref"),
            "mandatory": int(mandatory), "confidence": float(confidence),
            "status": self._choice(item.get("status", "proposed"), STATUSES, "status"),
            "calendar_event_id": event,
        }

    def upsert(self, owner, item: dict):
        """Insert an item, or refresh the one with the same (owner, source, source_ref).

        A refresh changes only title, dates, location and kind; the owner's status and the
        Google Calendar event id stay. Returns (item, created).
        """
        self._owner(owner)
        fields = self._validate(item)
        now = self.clock()
        with self._lock, closing(self._connect()) as db, db:
            existing = None
            if fields["source_ref"]:
                existing = db.execute("SELECT id FROM items WHERE owner=? AND source=? AND source_ref=?",
                                      (owner, fields["source"], fields["source_ref"])).fetchone()
            if existing is not None:
                db.execute("UPDATE items SET title=?, start=?, due=?, all_day=?, location=?, kind=?, "
                           "updated=? WHERE id=?",
                           (fields["title"], fields["start"], fields["due"], fields["all_day"],
                            fields["location"], fields["kind"], now, existing["id"]))
                item_id, created = existing["id"], False
            else:
                item_id, created = secrets.token_hex(16), True
                if fields["source"] == "manual" and not fields["source_ref"]:
                    fields["source_ref"] = item_id  # a manual entry is its own reference
                columns = ["id", "owner", *fields, "created", "updated"]
                db.execute(f"INSERT INTO items ({', '.join(columns)}) VALUES "
                           f"({', '.join('?' * len(columns))})",
                           (item_id, owner, *fields.values(), now, now))
        self._changed(owner)
        return self.get(owner, item_id), created

    def replace_refs(self, owner, source, prefix, items, *, remove_missing=True):
        """Make the owner's items of ``source`` whose source_ref starts with ``prefix`` match
        ``items``, in one transaction: every item is validated first (nothing is written when
        one is invalid), new refs are inserted, known ones refreshed like ``upsert`` (status and
        calendar event stay), and items under the prefix that are not in ``items`` are deleted,
        except items the owner marked done, or all of them when ``remove_missing`` is false.

        Returns {"created", "updated", "unchanged", "removed", "kept_done", "kept"}
        ("kept": missing items left because remove_missing is false); on_change runs once.
        """
        self._owner(owner)
        self._choice(source, SOURCES, "source")
        if not isinstance(prefix, str) or not prefix:
            raise AgendaError("prefix must not be empty")
        rows, refs = [], set()
        for number, item in enumerate(items, start=1):
            try:
                fields = self._validate(item)
            except AgendaError as exc:
                raise AgendaError(f"item {number}: {exc}") from None
            ref = fields["source_ref"]
            if fields["source"] != source or not ref.startswith(prefix):
                raise AgendaError(f"item {number}: source_ref must start with {prefix}")
            if ref in refs:
                raise AgendaError(f"item {number}: duplicate source_ref {ref}")
            refs.add(ref)
            rows.append(fields)
        counts = {"created": 0, "updated": 0, "unchanged": 0, "removed": 0, "kept_done": 0,
                  "kept": 0}
        now = self.clock()
        compared = ("title", "start", "due", "all_day", "location", "kind")
        with self._lock, closing(self._connect()) as db, db:
            existing = {row["source_ref"]: row for row in db.execute(
                "SELECT * FROM items WHERE owner=? AND source=? AND substr(source_ref, 1, ?)=?",
                (owner, source, len(prefix), prefix))}
            for fields in rows:
                old = existing.get(fields["source_ref"])
                if old is None:
                    columns = ["id", "owner", *fields, "created", "updated"]
                    db.execute(f"INSERT INTO items ({', '.join(columns)}) VALUES "
                               f"({', '.join('?' * len(columns))})",
                               (secrets.token_hex(16), owner, *fields.values(), now, now))
                    counts["created"] += 1
                elif any(old[key] != fields[key] for key in compared):
                    db.execute("UPDATE items SET title=?, start=?, due=?, all_day=?, location=?, "
                               "kind=?, updated=? WHERE id=?",
                               (*(fields[key] for key in compared), now, old["id"]))
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
            for ref, old in existing.items():
                if ref in refs:
                    continue
                if old["status"] == "done":
                    counts["kept_done"] += 1  # the owner's record of what was done stays
                elif not remove_missing:
                    counts["kept"] += 1
                else:
                    db.execute("DELETE FROM items WHERE id=?", (old["id"],))
                    counts["removed"] += 1
        if counts["created"] or counts["updated"] or counts["removed"]:
            self._changed(owner)
        return counts

    def delete_refs(self, owner, source, prefix, *, include_done=False):
        """Delete the items of ``source`` whose source_ref starts with ``prefix``; items marked
        done stay unless ``include_done``. Returns {"removed", "kept_done"} (on_change runs
        once when any were removed)."""
        self._owner(owner)
        self._choice(source, SOURCES, "source")
        if not isinstance(prefix, str) or not prefix:
            raise AgendaError("prefix must not be empty")
        where = "owner=? AND source=? AND substr(source_ref, 1, ?)=?"
        args = (owner, source, len(prefix), prefix)
        with self._lock, closing(self._connect()) as db, db:
            done = db.execute(f"SELECT COUNT(*) FROM items WHERE {where} AND status='done'",
                              args).fetchone()[0]
            removed = db.execute(f"DELETE FROM items WHERE {where}"
                                 + ("" if include_done else " AND status!='done'"),
                                 args).rowcount
        if removed:
            self._changed(owner)
        return {"removed": removed, "kept_done": 0 if include_done else done}

    def get(self, owner, item_id):
        self._owner(owner)
        if not isinstance(item_id, str):
            raise AgendaMissing("Item not found")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM items WHERE owner=? AND id=?", (owner, item_id)).fetchone()
        if row is None:
            raise AgendaMissing("Item not found")
        return self._row(row)

    def set_status(self, owner, item_id, status):
        self._choice(status, STATUSES, "status")
        self.get(owner, item_id)
        with self._lock, closing(self._connect()) as db, db:
            db.execute("UPDATE items SET status=?, updated=? WHERE owner=? AND id=?",
                       (status, self.clock(), owner, item_id))
        self._changed(owner)
        return self.get(owner, item_id)

    def set_event_id(self, owner, item_id, event_id):
        """Remember (or forget, with None) the Google Calendar event of an item."""
        self._owner(owner)
        if event_id is not None and not isinstance(event_id, str):
            raise AgendaError("calendar_event_id must be text")
        with self._lock, closing(self._connect()) as db, db:
            db.execute("UPDATE items SET calendar_event_id=? WHERE owner=? AND id=?",
                       (event_id, owner, item_id))

    def delete(self, owner, item_id):
        """Remove a manual item; synced items would come back, so they are dismissed instead."""
        if self.get(owner, item_id)["source"] != "manual":
            raise AgendaError("Only manual items can be deleted; dismiss synced items")
        with self._lock, closing(self._connect()) as db, db:
            db.execute("DELETE FROM items WHERE owner=? AND id=?", (owner, item_id))
        self._changed(owner)

    def list(self, owner, *, branch=None, include_done=False):
        """Items sorted by due, else start; done and dismissed items only on request."""
        self._owner(owner)
        query, args = "SELECT * FROM items WHERE owner=?", [owner]
        if branch is not None:
            query += " AND branch=?"
            args.append(self._choice(branch, BRANCHES, "branch"))
        if not include_done:
            query += f" AND status IN ({', '.join('?' * len(ACTIVE))})"
            args.extend(ACTIVE)
        with closing(self._connect()) as db:
            items = [self._row(r) for r in db.execute(query, args)]
        tz = _local_tz()
        items.sort(key=lambda it: (_moment(it["due"] or it["start"], tz), it["created"]))
        return items

    def between(self, owner, start, end):
        """Every item but dismissed ones whose due (else start) falls in [start, end), for the
        month view; done items stay so the calendar shows what was finished."""
        if start.tzinfo is None or end.tzinfo is None:
            raise AgendaError("start and end need a time zone")
        self._owner(owner)
        tz = start.tzinfo
        with closing(self._connect()) as db:
            # Stored offsets need not match, and bare dates mean midnight in the
            # window's zone. Python also accepts ISO forms/precision SQLite does
            # not; reuse its comparison (including DST) inside the SQL predicate.
            db.create_function("in_window", 1, lambda value: start <= _moment(value, tz) < end)
            rows = db.execute("SELECT * FROM items WHERE owner=? AND status!='dismissed' "
                              "AND in_window(COALESCE(NULLIF(due, ''), start))", (owner,))
            items = [self._row(row) for row in rows]
        sort_tz = _local_tz()
        items.sort(key=lambda it: (_moment(it["due"] or it["start"], sort_tz), it["created"]))
        return items

    def sections(self, owner, branch, now=None):
        """Open items of one branch as overdue, today, this week (to Sunday) and later.

        An item counts from its due date, else its start; the day boundaries are those of
        now's time zone (the server's local zone by default). A dated item is overdue after
        its moment has passed; an all-day item only once its day is over. An imported
        timetable lesson (kind LESSON, source_ref starting with IMPORT_PREFIX) that is over
        is left out; any other imported kind (an exam) stays overdue until it is done.
        """
        if now is None:
            now = datetime.now().astimezone()
        if now.tzinfo is None:
            raise AgendaError("now needs a time zone")
        tz = now.tzinfo
        today = datetime.combine(now.date(), dtime(), tz)
        tomorrow = today + timedelta(days=1)
        week_end = today + timedelta(days=7 - now.weekday())  # next Monday 00:00
        result = {name: [] for name in SECTIONS}
        for item in self.list(owner, branch=branch):
            value = item["due"] or item["start"]
            moment = _moment(value, tz)
            if (moment < today) if _DATE.fullmatch(value) else (moment < now):
                if not (item["kind"] == LESSON and item["source_ref"].startswith(IMPORT_PREFIX)):
                    # an imported lesson that is over is not late; an imported exam is
                    result["overdue"].append(item)
            elif moment < tomorrow:
                result["today"].append(item)
            elif moment < week_end:
                result["week"].append(item)
            else:
                result["later"].append(item)
        for name in SECTIONS:
            result[name].sort(key=lambda it: _moment(it["due"] or it["start"], tz))
        return result
