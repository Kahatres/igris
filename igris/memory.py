"""JSONL conversation memory for Igris."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

VALID_ROLES = ("user", "assistant", "tool")


class Memory:
    """Append-only conversation memory stored as one JSON object per line."""

    def __init__(self, path: Path, max_history: int = 20, clock=time.time):
        if not isinstance(max_history, int) or max_history < 1:
            raise ValueError("max_history must be >= 1")
        self.path = Path(path)
        self.max_history = max_history
        self._clock = clock
        self._lock = threading.Lock()
        self._count_cache = None

    def append(self, role: str, content: str, *, backend: str | None = None) -> dict:
        """Store a new entry and return it.

        ``backend`` names the model backend that produced an assistant reply; it is stored
        only when given, so older entries (without it) read the same.
        """
        if role not in VALID_ROLES:
            raise ValueError(f"invalid role: {role!r}")
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if backend is not None and not isinstance(backend, str):
            raise ValueError("backend must be a string")
        entry = {"ts": float(self._clock()), "role": role, "content": content}
        if backend:
            entry["backend"] = backend
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            self._count_cache = None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        return entry

    @staticmethod
    def _parse(raw):
        """Decode one entry, retaining the same validation for every read path."""
        raw = raw.strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        ts = obj.get("ts")
        if (
            isinstance(ts, bool)
            or not isinstance(ts, (int, float))
            or not isinstance(obj.get("role"), str)
            or not isinstance(obj.get("content"), str)
        ):
            return None
        return obj

    def _entries(self):
        """Stream valid entries without keeping the file's lines in memory."""
        try:
            with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    entry = self._parse(raw)
                    if entry is not None:
                        yield entry
        except FileNotFoundError:
            return

    def _read(self) -> list[dict]:
        """Read all valid entries, skipping corrupted lines."""
        return list(self._entries())

    @staticmethod
    def _tail_lines(fh):
        """Yield lines newest first, reading bounded blocks from a binary file."""
        position = fh.seek(0, 2)
        pending = b""
        while position:
            size = min(position, 8192)
            position -= size
            fh.seek(position)
            # Splitting before decoding keeps multibyte UTF-8 characters intact.
            # Treat CR and LF as boundaries, as the original text-mode reader did;
            # an extra empty line from CRLF is harmless, even across blocks.
            lines = re.split(b"[\r\n]", fh.read(size) + pending)
            pending = lines[0]
            for raw in reversed(lines[1:]):
                yield raw.decode("utf-8", errors="replace")
        if pending:
            yield pending.decode("utf-8", errors="replace")

    def history(self, limit: int | None = None) -> list[dict]:
        """Return the last `limit` entries, oldest to newest."""
        if limit is None:
            limit = self.max_history
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be >= 1")
        entries = []
        with self._lock:
            try:
                with open(self.path, "rb") as fh:
                    for raw in self._tail_lines(fh):
                        entry = self._parse(raw)
                        if entry is not None:
                            entries.append(entry)
                            if len(entries) == limit:
                                break
            except FileNotFoundError:
                pass
        entries.reverse()
        return entries

    def search(self, query: str, limit: int = 5) -> list[dict]:
        """Case-insensitive substring search, newest first."""
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be >= 1")
        if not query:
            return []
        needle = query.casefold()
        with self._lock:
            entries = self._read()
        result = []
        for entry in reversed(entries):
            if needle in entry["content"].casefold():
                result.append(entry)
                if len(result) >= limit:
                    break
        return result

    def clear(self) -> None:
        """Remove all stored entries."""
        with self._lock:
            self._count_cache = None
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass

    def __len__(self) -> int:
        with self._lock:
            try:
                stat = self.path.stat()
            except FileNotFoundError:
                self._count_cache = None
                return 0
            stamp = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            if self._count_cache is not None and self._count_cache[0] == stamp:
                return self._count_cache[1]
            # Counting raw lines would count corrupt entries too. Validate once,
            # stream the count, then reuse it until the file changes.
            count = sum(1 for _ in self._entries())
            self._count_cache = (stamp, count)
            return count
