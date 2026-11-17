"""Named conversation sessions stored as one JSONL file per session."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from igris.memory import Memory

_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
DEFAULT_SESSION = "default"


class SessionError(ValueError):
    """Raised for invalid names or invalid session operations."""


def _check_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise SessionError(f"invalid session name: {name!r}")
    return name


class SessionManager:
    """Manage named sessions under a root directory."""

    def __init__(self, root: Path, max_history: int = 20, clock=time.time):
        self.root = Path(root)
        self.max_history = max_history
        self.clock = clock

    def _path(self, name: str) -> Path:
        return self.root / f"{_check_name(name)}.jsonl"

    @property
    def _active_file(self) -> Path:
        return self.root / "active.txt"

    def _read_active(self) -> str:
        try:
            text = self._active_file.read_text(encoding="utf-8").strip()
        except OSError:
            return DEFAULT_SESSION
        if _NAME_RE.fullmatch(text):
            return text
        return DEFAULT_SESSION

    def _write_active(self, name: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.root / "active.txt.tmp"
        tmp.write_text(name, encoding="utf-8")
        os.replace(tmp, self._active_file)

    def open(self, name: str) -> Memory:
        """Return the Memory for `name`; nothing is created until append."""
        return Memory(self._path(name), max_history=self.max_history, clock=self.clock)

    def current(self) -> tuple[str, Memory]:
        name = self._read_active()
        return name, self.open(name)

    def switch(self, name: str) -> Memory:
        memory = self.open(name)  # validates the name
        self._write_active(name)
        return memory

    def list(self) -> list[dict]:
        result: list[dict] = []
        if not self.root.is_dir():
            return result
        for path in self.root.glob("*.jsonl"):
            name = path.stem
            if not _NAME_RE.fullmatch(name) or not path.is_file():
                continue
            memory = self.open(name)
            entries = memory.history()
            updated = float(entries[-1]["ts"]) if entries else 0.0
            result.append({"name": name, "messages": len(memory), "updated": updated})
        result.sort(key=lambda d: (-d["updated"], d["name"]))
        return result

    def delete(self, name: str) -> None:
        path = self._path(name)
        if not path.is_file():
            raise SessionError(f"session not found: {name}")
        path.unlink()
        if self._read_active() == name:
            self._write_active(DEFAULT_SESSION)

    def rename(self, old: str, new: str) -> None:
        old_path = self._path(old)
        new_path = self._path(new)
        if not old_path.is_file():
            raise SessionError(f"session not found: {old}")
        if new_path.exists():
            raise SessionError(f"session already exists: {new}")
        was_active = self._read_active() == old
        os.replace(old_path, new_path)
        if was_active:
            self._write_active(new)
