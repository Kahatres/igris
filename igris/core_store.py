"""Owner-scoped SQLite library of document cards and editable pages."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import uuid


class CoreStore:
    """Persist cards, source metadata and pages without crossing owner boundaries."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS core_documents (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL,
                    source TEXT NOT NULL, read_as TEXT NOT NULL, card TEXT NOT NULL,
                    file_id TEXT, urls TEXT NOT NULL, error TEXT
                );
                CREATE INDEX IF NOT EXISTS core_owner ON core_documents(owner);
                CREATE TABLE IF NOT EXISTS core_pages (
                    doc_id TEXT NOT NULL REFERENCES core_documents(id) ON DELETE CASCADE,
                    n INTEGER NOT NULL, text TEXT NOT NULL, from_image INTEGER NOT NULL,
                    PRIMARY KEY(doc_id, n)
                );
            """)
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS core_fts USING "
                           "fts5(owner UNINDEXED, doc_id UNINDEXED, location UNINDEXED, text)")
            except sqlite3.OperationalError:
                self._fts = False
            else:
                self._fts = True
                # Rebuild also covers a database first opened on a build without FTS5.
                db.execute("DELETE FROM core_fts")
                db.execute("INSERT INTO core_fts SELECT owner, id, 'card', card "
                           "FROM core_documents")
                db.execute("INSERT INTO core_fts SELECT d.owner, p.doc_id, p.n, p.text "
                           "FROM core_pages p JOIN core_documents d ON d.id = p.doc_id")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    @staticmethod
    def _document(db, owner, doc_id):
        row = db.execute("SELECT * FROM core_documents WHERE owner = ? AND id = ?",
                         (owner, doc_id)).fetchone()
        if row is None:
            raise KeyError(doc_id)
        doc = dict(row)
        doc["urls"] = json.loads(doc["urls"])
        return doc

    def add(self, owner, title, source, read_as, card, pages, *, file_id=None,
            urls=(), error=None, from_image=None) -> str:
        """Store a document atomically and return its opaque id. Pages start at one."""
        if source not in ("upload", "research"):
            raise ValueError("source must be upload or research")
        if read_as not in ("text", "image", "mixed", "failed", "reading"):
            raise ValueError("read_as must be text, image, mixed, failed or reading")
        if not isinstance(pages, list) or any(not isinstance(p, str) for p in pages):
            raise ValueError("pages must be a list of strings")
        if from_image is None:
            from_image = [read_as == "image"] * len(pages)
        if (not isinstance(from_image, list) or len(from_image) != len(pages)
                or any(type(flag) is not bool for flag in from_image)):
            raise ValueError("from_image must be booleans aligned with pages")
        if any(not isinstance(value, str) for value in (owner, title, card)):
            raise ValueError("owner, title and card must be strings")
        if isinstance(urls, str) or any(not isinstance(url, str) for url in urls):
            raise ValueError("urls must be a sequence of strings")
        doc_id = uuid.uuid4().hex
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO core_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (doc_id, owner, title, source, read_as, card, file_id,
                        json.dumps(list(urls)), error))
            db.executemany("INSERT INTO core_pages VALUES (?, ?, ?, ?)",
                           [(doc_id, n, text, flag) for n, (text, flag)
                            in enumerate(zip(pages, from_image), 1)])
            if self._fts:
                db.executemany("INSERT INTO core_fts VALUES (?, ?, ?, ?)",
                               [(owner, doc_id, "card", card)] +
                               [(owner, doc_id, n, text) for n, text in enumerate(pages, 1)])
        return doc_id

    def reserve_upload(self, owner, title, file_id):
        """Persist a pending upload before scheduling any extraction/model work."""
        return self.add(owner, title, "upload", "reading", "", [], file_id=file_id)

    def complete_upload(self, owner, doc_id, read_as, card, pages, *, error=None,
                        from_image=None):
        """Replace a pending document atomically, without recreating a deleted one."""
        if read_as not in ("text", "image", "mixed", "failed"):
            raise ValueError("Invalid completed read_as")
        if not isinstance(card, str) or not isinstance(pages, list) or any(
                not isinstance(page, str) for page in pages):
            raise ValueError("card and pages must contain text")
        flags = [read_as == "image"] * len(pages) if from_image is None else from_image
        if not isinstance(flags, list) or len(flags) != len(pages) or any(
                type(flag) is not bool for flag in flags):
            raise ValueError("from_image must be booleans aligned with pages")
        with closing(self._connect()) as db, db:
            doc = self._document(db, owner, doc_id)
            if doc["read_as"] != "reading":
                raise ValueError("Document is no longer reading")
            db.execute("UPDATE core_documents SET read_as=?, card=?, error=? WHERE owner=? AND id=?",
                       (read_as, card, error, owner, doc_id))
            db.executemany("INSERT INTO core_pages VALUES (?, ?, ?, ?)",
                [(doc_id, n, text, flag) for n, (text, flag) in enumerate(zip(pages, flags), 1)])
            if self._fts:
                db.execute("DELETE FROM core_fts WHERE owner=? AND doc_id=?", (owner, doc_id))
                db.executemany("INSERT INTO core_fts VALUES (?, ?, ?, ?)",
                    [(owner, doc_id, "card", card)] +
                    [(owner, doc_id, n, text) for n, text in enumerate(pages, 1)])

    def fail_pending(self, owner, reason):
        """After a restart, surface interrupted reads instead of leaving them pending."""
        with closing(self._connect()) as db, db:
            db.execute("UPDATE core_documents SET read_as='failed', error=? "
                       "WHERE owner=? AND read_as='reading'", (reason, owner))

    def list(self, owner) -> list[dict]:
        """List metadata and cards, keeping full pages out of default context."""
        with closing(self._connect()) as db:
            rows = db.execute("SELECT id FROM core_documents WHERE owner = ? ORDER BY rowid",
                              (owner,)).fetchall()
            return [self._document(db, owner, row["id"]) for row in rows]

    def get(self, owner, doc_id) -> dict:
        with closing(self._connect()) as db, db:
            doc = self._document(db, owner, doc_id)
            doc["pages"] = [dict(row) for row in db.execute(
                "SELECT n, text, from_image FROM core_pages WHERE doc_id = ? ORDER BY n",
                (doc_id,))]
            for page in doc["pages"]:
                page["from_image"] = bool(page["from_image"])
            return doc

    def set_page(self, owner, doc_id, n, text):
        if type(n) is not int or n < 1:
            raise KeyError(n)
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        with closing(self._connect()) as db, db:
            self._document(db, owner, doc_id)
            cursor = db.execute("UPDATE core_pages SET text = ? WHERE doc_id = ? AND n = ?",
                                (text, doc_id, n))
            if not cursor.rowcount:
                raise KeyError(n)
            if self._fts:
                db.execute("UPDATE core_fts SET text = ? WHERE owner = ? AND doc_id = ? "
                           "AND location = ?", (text, owner, doc_id, n))

    def delete(self, owner, doc_id):
        with closing(self._connect()) as db, db:
            self._document(db, owner, doc_id)
            db.execute("DELETE FROM core_documents WHERE owner = ? AND id = ?", (owner, doc_id))
            if self._fts:
                db.execute("DELETE FROM core_fts WHERE owner = ? AND doc_id = ?", (owner, doc_id))

    def search(self, owner, query, limit=5) -> list[dict]:
        """Find literal keywords in cards and pages, with identical fallback semantics."""
        if not isinstance(query, str):
            raise ValueError("query must be a string")
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        query = query.strip().casefold()
        if not query or not limit:
            return []
        with closing(self._connect()) as db:
            matches = {}
            if self._fts:
                # Quote user input so punctuation cannot become FTS query syntax.
                phrase = '"' + query.replace('"', '""') + '"'
                rows = db.execute("SELECT doc_id, location, text FROM core_fts "
                                  "WHERE core_fts MATCH ? AND owner = ?", (phrase, owner))
                matches = {(r["doc_id"], str(r["location"])): r["text"] for r in rows}
            rows = db.execute("""
                SELECT d.id AS doc_id, d.title, 'card' AS location, d.card AS text,
                       d.rowid AS position, 0 AS page
                FROM core_documents d WHERE d.owner = ?
                UNION ALL
                SELECT d.id, d.title, p.n, p.text, d.rowid, p.n
                FROM core_documents d JOIN core_pages p ON p.doc_id = d.id
                WHERE d.owner = ? ORDER BY position, page
            """, (owner, owner))
            hits = []
            for row in rows:
                # Partial words and Unicode case folding behave the same without FTS5.
                text = matches.get((row["doc_id"], str(row["location"])), row["text"])
                start = text.casefold().find(query)
                if start < 0:
                    continue
                start = max(0, start - 80)
                hits.append({"doc_id": row["doc_id"], "title": row["title"],
                             "where": "card" if row["page"] == 0 else row["page"],
                             "excerpt": text[start:start + 300]})
                if len(hits) >= limit:
                    break
            return hits
