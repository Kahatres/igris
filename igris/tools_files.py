"""Read-only file tools confined to a workspace directory."""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from igris.tools import Tool, ToolError, ToolRegistry

_BINARY_PROBE = 1024


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_PROBE]


class Workspace:
    """A directory root that file tools may read from, and nothing else."""

    def __init__(self, root: Path, max_bytes: int = 20000, max_results: int = 50):
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.max_results = max_results

    def resolve(self, rel: str) -> Path:
        """Resolve ``rel`` inside the root or raise ToolError."""
        rel = "." if rel in (None, "") else str(rel)
        if "\x00" in rel:
            raise ToolError("invalid path")
        if Path(rel).is_absolute():
            raise ToolError(f"path outside workspace: {rel}")
        try:
            candidate = (self.root / rel).resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ToolError(f"cannot resolve path: {rel}") from exc
        if candidate != self.root and self.root not in candidate.parents:
            raise ToolError(f"path outside workspace: {rel}")
        return candidate

    def _read_bytes(self, path: Path) -> bytes:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ToolError(f"cannot read {path.name}: {exc}") from exc

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        target = self.resolve(path)
        if not target.exists():
            raise ToolError(f"file not found: {path}")
        if not target.is_file():
            raise ToolError(f"not a file: {path}")
        data = self._read_bytes(target)
        if _is_binary(data):
            raise ToolError(f"binary file: {path}")
        start = int(start)
        if start < 1:
            raise ToolError("start must be >= 1")
        if end is not None:
            end = int(end)
            if end < start:
                raise ToolError("end must be >= start")
        lines = data.decode("utf-8", errors="replace").splitlines()
        selected = lines[start - 1 : end]
        text = "\n".join(f"{start + i}: {line}" for i, line in enumerate(selected))
        raw = text.encode("utf-8")
        if len(raw) > self.max_bytes:
            text = raw[: self.max_bytes].decode("utf-8", errors="ignore")
            text += "... [truncated]"
        return text

    def list_dir(self, path: str = ".") -> str:
        target = self.resolve(path)
        if not target.is_dir():
            raise ToolError(f"directory not found: {path}")
        try:
            entries = sorted(target.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            raise ToolError(f"cannot list {path}: {exc}") from exc
        out = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            out.append(entry.name + ("/" if entry.is_dir() else ""))
        return "\n".join(out)

    def search_text(self, query: str, path: str = ".", glob: str = "*") -> str:
        if not query:
            raise ToolError("empty query")
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"directory not found: {path}")
        needle = query.lower()
        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or not fnmatch.fnmatch(name, glob):
                    continue
                try:
                    fpath = (Path(dirpath) / name).resolve()
                except (OSError, RuntimeError):
                    continue
                if self.root not in fpath.parents or not fpath.is_file():
                    continue
                try:
                    data = fpath.read_bytes()
                except OSError:
                    continue
                if _is_binary(data):
                    continue
                rel = (Path(dirpath) / name).relative_to(self.root).as_posix()
                text = data.decode("utf-8", errors="replace")
                for num, line in enumerate(text.splitlines(), start=1):
                    if needle in line.lower():
                        if len(hits) >= self.max_results:
                            return "\n".join(hits) + "\n... [more results]"
                        hits.append(f"{rel}:{num}: {line.strip()}")
        return "\n".join(hits) if hits else "no matches"


def register_file_tools(registry: ToolRegistry, workspace: Workspace) -> None:
    """Register read_file, list_dir and search_text on the registry."""
    registry.add(Tool(
        name="read_file",
        description="Read a text file (args: path, start=1, end=None).",
        func=lambda path, start=1, end=None: workspace.read_file(path, start, end),
    ))
    registry.add(Tool(
        name="list_dir",
        description="List a directory (args: path='.').",
        func=lambda path=".": workspace.list_dir(path),
    ))
    registry.add(Tool(
        name="search_text",
        description="Case-insensitive text search (args: query, path='.', glob='*').",
        func=lambda query, path=".", glob="*": workspace.search_text(query, path, glob),
    ))
