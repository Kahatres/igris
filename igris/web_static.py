"""Static files of the web interface: which names are served, their types and ETags.

Route functions take the request handler ``h`` and the server context ``ctx``.
Standard library only; imports nothing from igris.
"""

import hashlib
import re
import threading
from pathlib import Path

STATIC_RE = re.compile(r"[a-z0-9_-]+\.(css|js|ttf|png|webmanifest)", re.ASCII)
VENDOR_RE = re.compile(
    r"vendor/(?:three/(?:three\.module\.min\.js|OrbitControls\.js)|"
    r"mathlive/(?:mathlive\.min\.mjs|mathlive-fonts\.css|fonts/[A-Za-z0-9_-]+\.(?:woff2?|ttf)))")
EXTRA_NAMES = ("study_fixture.json",)
STATIC_TYPES = {"css": "text/css; charset=utf-8", "js": "text/javascript; charset=utf-8",
                "ttf": "font/ttf", "png": "image/png",
                "webmanifest": "application/manifest+json; charset=utf-8",
                "mjs": "text/javascript; charset=utf-8", "woff": "font/woff",
                "woff2": "font/woff2", "json": "application/json; charset=utf-8"}
# asset names are not versioned, so a browser must revalidate every /static/ file; the
# ETag makes that a cheap 304 and a deploy is seen at once
STATIC_CACHE = "no-cache"


def allowed(name: str) -> bool:
    """Only flat lowercase asset names, the study fixture and the known vendor files."""
    return bool(STATIC_RE.fullmatch(name) or name in EXTRA_NAMES or VENDOR_RE.fullmatch(name))


def etag_matches(header: str | None, etag: str) -> bool:
    """True when an If-None-Match header names ``etag`` (weak or strong) or is ``*``."""
    if not header:
        return False
    for item in header.split(","):
        item = item.strip()
        if item == "*" or item.removeprefix("W/") == etag:
            return True
    return False


class StaticFiles:
    """Reads files under ``root``; content and ETag are kept per (mtime, size)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._cache: dict[str, tuple[tuple[int, int], bytes, str]] = {}
        self._lock = threading.Lock()

    def load(self, name: str) -> tuple[bytes, str, str] | None:
        """(content, content type, ETag), or None when ``name`` is not served."""
        if not allowed(name):
            return None
        target = self.root / name
        try:
            if not target.is_file() or self.root.resolve() not in target.resolve().parents:
                return None
            info = target.stat()
            key = (info.st_mtime_ns, info.st_size)
            with self._lock:
                hit = self._cache.get(name)
            if hit is not None and hit[0] == key:
                data, etag = hit[1], hit[2]
            else:
                data = target.read_bytes()
                etag = '"' + hashlib.sha256(data).hexdigest()[:32] + '"'
                with self._lock:
                    self._cache[name] = (key, data, etag)
        except OSError:
            return None
        return data, STATIC_TYPES[target.suffix[1:]], etag


def page(h, ctx, filename: str) -> None:
    """An HTML page from the static folder; pages are never cached."""
    try:
        data = (ctx.static_root / filename).read_bytes()
    except OSError:
        return h._error(404, "not found")
    return h._send(200, data, "text/html; charset=utf-8")


def serve(h, ctx, name: str, *, cached: bool = True) -> None:
    """A static file for the request handler ``h``; ``cached`` adds the ETag and
    revalidation caching (the service worker is sent without, so browsers always see a new one)."""
    found = ctx.static_files.load(name)
    if found is None:
        return h._error(404, "not found")
    data, ctype, etag = found
    if not cached:
        return h._send(200, data, ctype)
    if etag_matches(h.headers.get("If-None-Match"), etag):
        h.send_response(304)  # no body and no Content-Length
        h.send_header("Cache-Control", STATIC_CACHE)
        h.send_header("ETag", etag)
        h.send_header("X-Content-Type-Options", "nosniff")
        return h.end_headers()
    h._send(200, data, ctype, cache=STATIC_CACHE, etag=etag)
