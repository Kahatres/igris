"""Owner-scoped storage and lookup for people and their contact details."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from igris.textfold import fold_marks as fold


class PeopleError(ValueError):
    """Raised when a person record is invalid or cannot be updated."""


@dataclass(frozen=True)
class Person:
    """A contact and the mail preferences associated with them."""

    id: str
    name: str
    aliases: tuple[str, ...]
    emails: tuple[str, ...]
    role: str
    importance: str
    language: str = "en"
    tone: str = "formal"
    identity: str = "personal"
    notes: str = ""


def valid_address(address: str) -> bool:
    """Return whether an address meets the store's basic safe-address rules."""
    if not isinstance(address, str) or not address or len(address) > 254:
        return False
    if any(char.isspace() or unicodedata.category(char) == "Cc" for char in address):
        return False
    if any(char in address for char in "<>,;") or address.count("@") != 1:
        return False
    local, domain = address.split("@")
    return bool(local and domain and "." in domain)


_FIELDS = {
    "name",
    "aliases",
    "emails",
    "role",
    "importance",
    "language",
    "tone",
    "identity",
    "notes",
}


class PeopleStore:
    """SQLite-backed, owner-isolated people store."""

    def __init__(self, path: str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS people (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    name TEXT NOT NULL,
                    aliases TEXT NOT NULL,
                    emails TEXT NOT NULL,
                    role TEXT NOT NULL,
                    importance TEXT NOT NULL,
                    language TEXT NOT NULL,
                    tone TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    folded TEXT NOT NULL
                )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS people_owner_idx ON people(owner)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _tuple_texts(value, field: str) -> tuple[str, ...]:
        if isinstance(value, str):
            raise PeopleError(f"{field} must be a sequence of strings")
        try:
            values = tuple(value)
        except TypeError as exc:
            raise PeopleError(f"{field} must be a sequence of strings") from exc
        if any(not isinstance(item, str) or not item.strip() for item in values):
            raise PeopleError(f"{field} must contain non-empty strings")
        return values

    @staticmethod
    def _validate_fields(fields: dict) -> dict:
        unknown = set(fields) - _FIELDS
        if unknown:
            raise PeopleError("unsupported person field")
        normalized = dict(fields)
        if "name" in normalized:
            name = normalized["name"]
            if not isinstance(name, str) or not name.strip():
                raise PeopleError("name must be a non-empty string")
            normalized["name"] = name.strip()
        for key in ("aliases", "emails"):
            if key in normalized:
                normalized[key] = PeopleStore._tuple_texts(normalized[key], key)
        if "emails" in normalized and any(
            not valid_address(address) for address in normalized["emails"]
        ):
            raise PeopleError("invalid email address")
        if "importance" in normalized and (
            not isinstance(normalized["importance"], str)
            or normalized["importance"] not in {"low", "normal", "high"}
        ):
            raise PeopleError("invalid importance")
        if "tone" in normalized and (
            not isinstance(normalized["tone"], str)
            or normalized["tone"] not in {"formal", "casual"}
        ):
            raise PeopleError("invalid tone")
        if "identity" in normalized and (
            not isinstance(normalized["identity"], str)
            or normalized["identity"] not in {"personal", "school"}
        ):
            raise PeopleError("invalid identity")
        for key in ("role", "language", "notes"):
            if key in normalized and not isinstance(normalized[key], str):
                raise PeopleError(f"{key} must be a string")
        return normalized

    @staticmethod
    def _folded(name: str, aliases: tuple[str, ...]) -> str:
        return fold(" ".join((name, *aliases)))

    @staticmethod
    def _person(row: sqlite3.Row) -> Person:
        return Person(
            id=row["id"],
            name=row["name"],
            aliases=tuple(json.loads(row["aliases"])),
            emails=tuple(json.loads(row["emails"])),
            role=row["role"],
            importance=row["importance"],
            language=row["language"],
            tone=row["tone"],
            identity=row["identity"],
            notes=row["notes"],
        )

    @staticmethod
    def _fold_words(row: sqlite3.Row) -> set[str]:
        return set(row["folded"].split())

    def add(
        self,
        owner: str,
        name: str,
        *,
        aliases=(),
        emails=(),
        role="",
        importance="normal",
        language="en",
        tone="formal",
        identity="personal",
        notes="",
    ) -> Person:
        fields = self._validate_fields(
            {
                "name": name,
                "aliases": aliases,
                "emails": emails,
                "role": role,
                "importance": importance,
                "language": language,
                "tone": tone,
                "identity": identity,
                "notes": notes,
            }
        )
        person_id = uuid.uuid4().hex
        person = Person(id=person_id, **fields)
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO people
                   (id, owner, name, aliases, emails, role, importance, language,
                    tone, identity, notes, folded)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    person.id,
                    owner,
                    person.name,
                    json.dumps(person.aliases, ensure_ascii=False),
                    json.dumps(person.emails, ensure_ascii=False),
                    person.role,
                    person.importance,
                    person.language,
                    person.tone,
                    person.identity,
                    person.notes,
                    self._folded(person.name, person.aliases),
                ),
            )
        return person

    def update(self, owner: str, person_id: str, **fields) -> Person:
        normalized = self._validate_fields(fields)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM people WHERE owner = ? AND id = ?",
                (owner, person_id),
            ).fetchone()
            if row is None:
                raise PeopleError("person not found")
            current = self._person(row)
            values = {field: getattr(current, field) for field in _FIELDS}
            values.update(normalized)
            updated = Person(id=current.id, **values)
            connection.execute(
                """UPDATE people SET name = ?, aliases = ?, emails = ?, role = ?,
                   importance = ?, language = ?, tone = ?, identity = ?, notes = ?,
                   folded = ? WHERE owner = ? AND id = ?""",
                (
                    updated.name,
                    json.dumps(updated.aliases, ensure_ascii=False),
                    json.dumps(updated.emails, ensure_ascii=False),
                    updated.role,
                    updated.importance,
                    updated.language,
                    updated.tone,
                    updated.identity,
                    updated.notes,
                    self._folded(updated.name, updated.aliases),
                    owner,
                    person_id,
                ),
            )
        return updated

    def remove(self, owner: str, person_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM people WHERE owner = ? AND id = ?", (owner, person_id)
            )
        return cursor.rowcount > 0

    def get(self, owner: str, person_id: str) -> Person | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM people WHERE owner = ? AND id = ?", (owner, person_id)
            ).fetchone()
        return self._person(row) if row is not None else None

    def find(self, owner: str, query: str) -> list[Person]:
        query_words = fold(query).split()
        if not query_words:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM people WHERE owner = ? ORDER BY name COLLATE NOCASE, id",
                (owner,),
            ).fetchall()
        return [
            self._person(row)
            for row in rows
            if all(
                any(word.startswith(query_word) for word in self._fold_words(row))
                for query_word in query_words
            )
        ]

    def by_address(self, owner: str, address: str) -> Person | None:
        if not isinstance(address, str):
            return None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM people WHERE owner = ? ORDER BY name COLLATE NOCASE, id",
                (owner,),
            ).fetchall()
        sought = address.casefold()
        for row in rows:
            person = self._person(row)
            if any(item.casefold() == sought for item in person.emails):
                return person
        return None

    def list(self, owner: str) -> list[Person]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM people WHERE owner = ? ORDER BY name COLLATE NOCASE, id",
                (owner,),
            ).fetchall()
        return [self._person(row) for row in rows]
