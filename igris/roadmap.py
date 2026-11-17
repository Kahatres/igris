"""Load and validate the Igris roadmap data."""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

__all__ = [
    "RoadmapError",
    "RELEASE_STATUSES",
    "PLAN_STATUSES",
    "default_path",
    "load",
    "validate",
    "summary",
]


class RoadmapError(ValueError):
    """Raised when the roadmap file is missing, unreadable or invalid."""


RELEASE_STATUSES = {"done"}
PLAN_STATUSES = {"in_progress", "planned", "idea"}

_ID_RE = re.compile(r"[a-z0-9-]{1,40}")


def default_path() -> Path:
    """Return the path of the bundled roadmap file."""
    return Path(__file__).parent / "roadmap.json"


def load(path: Path | None = None) -> dict:
    """Read a UTF-8 JSON roadmap, validate it and return the data."""
    p = Path(path) if path is not None else default_path()
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RoadmapError(f"cannot read roadmap {p}: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RoadmapError(f"invalid JSON in {p}: {exc}") from exc
    return validate(data)


def _text(value, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RoadmapError(f"{name} must be a non-empty string")


def _date(value, name: str) -> None:
    if not isinstance(value, str):
        raise RoadmapError(f"{name} must be an ISO date (YYYY-MM-DD)")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise RoadmapError(f"{name} must be an ISO date (YYYY-MM-DD)") from exc
    if len(value) != 10:
        raise RoadmapError(f"{name} must be an ISO date (YYYY-MM-DD)")


def _entry(entry, name: str, kind: str, statuses: set, seen: set) -> None:
    if not isinstance(entry, dict):
        raise RoadmapError(f"{name} must be an object")
    required = ["id", "title", "status", "items"]
    if kind == "releases":
        required.append("date")
    for key in required:
        if key not in entry:
            raise RoadmapError(f"{name}.{key} is missing")
    ident = entry["id"]
    if not isinstance(ident, str) or not _ID_RE.fullmatch(ident):
        raise RoadmapError(f"{name}.id must match [a-z0-9-]{{1,40}}")
    if ident in seen:
        raise RoadmapError(f"{name}.id duplicates '{ident}'")
    seen.add(ident)
    _text(entry["title"], f"{name}.title")
    if kind == "releases":
        _date(entry["date"], f"{name}.date")
    status = entry["status"]
    if not isinstance(status, str) or status not in statuses:
        raise RoadmapError(f"{name}.status '{status}' is not one of {sorted(statuses)}")
    items = entry["items"]
    if not isinstance(items, list):
        raise RoadmapError(f"{name}.items must be a list")
    for i, item in enumerate(items):
        _text(item, f"{name}.items[{i}]")
    if "built_by" in entry:
        _text(entry["built_by"], f"{name}.built_by")


def validate(data) -> dict:
    """Return data unchanged if it is a valid roadmap, else raise RoadmapError."""
    if not isinstance(data, dict):
        raise RoadmapError("roadmap must be an object")
    for key in ("updated", "releases", "plans"):
        if key not in data:
            raise RoadmapError(f"{key} is missing")
    _date(data["updated"], "updated")
    seen: set = set()
    for kind, statuses in (("releases", RELEASE_STATUSES), ("plans", PLAN_STATUSES)):
        entries = data[kind]
        if not isinstance(entries, list):
            raise RoadmapError(f"{kind} must be a list")
        for i, entry in enumerate(entries):
            _entry(entry, f"{kind}[{i}]", kind, statuses, seen)
    return data


def summary(data: dict) -> dict[str, int]:
    """Count releases and plans per status."""
    out = {"done": len(data.get("releases", [])), "in_progress": 0, "planned": 0, "idea": 0}
    for plan in data.get("plans", []):
        status = plan.get("status")
        if status in out and status != "done":
            out[status] += 1
    return out
