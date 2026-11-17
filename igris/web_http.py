"""HTTP plumbing of the web server: the bounded threading server, request limits and
the response helpers every route module uses.

Route functions elsewhere take the request handler ``h`` (a subclass of BaseHandler)
and the server context ``ctx``. Standard library only; imports nothing from igris.
"""

import io
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

MAX_BODY = 64 * 1024
MAX_TEXT = 8000
MAX_HISTORY = 200
REQUEST_TIMEOUT = 30  # seconds a client may stall a socket read or write
# Seconds a client has in total to send one request line, its headers and a small body;
# a body may add one second per MIN_BODY_RATE bytes (granted only after authentication).
REQUEST_DEADLINE = 60
MIN_BODY_RATE = 32 * 1024
MAX_CONNECTIONS = 64  # connections served at once; each one holds a thread


class BoundedHTTPServer(ThreadingHTTPServer):
    """Limit live request threads, including clients still sending headers or bodies;
    a connection over the limit is closed at once."""

    def __init__(self, *args, max_connections=MAX_CONNECTIONS, **kwargs):
        if max_connections < 1:
            raise ValueError("max_connections must be positive")
        self._connections = threading.BoundedSemaphore(max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()


class _DeadlineReader(io.RawIOBase):
    """Socket reads bounded by a total deadline, not only per read: before each recv the
    socket timeout shrinks to the time left, so a client trickling one byte at a time
    cannot hold a connection slot. ``deadline`` None reads with the plain timeout."""

    def __init__(self, sock: socket.socket, timeout: float | None):
        super().__init__()
        self._sock = sock
        self._timeout = timeout
        self.deadline: float | None = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self.deadline is None:
            return self._sock.recv_into(buffer)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline exceeded")
        self._sock.settimeout(remaining if self._timeout is None
                              else min(self._timeout, remaining))
        try:
            return self._sock.recv_into(buffer)
        finally:  # writes keep the plain per-operation timeout
            self._sock.settimeout(self._timeout)


class BaseHandler(BaseHTTPRequestHandler):
    """Response helpers; ``ctx`` is set on the per-server subclass.

    Reading one request (request line, headers and body) must finish within
    ``request_deadline`` seconds, or the connection is closed. A route that streams for
    long after reading its request (the /api/events stream) ends the deadline itself."""

    server_version = "Igris"
    timeout = REQUEST_TIMEOUT  # slow clients must not pin a thread
    request_deadline = REQUEST_DEADLINE
    min_body_rate = MIN_BODY_RATE
    ctx = None
    _reader = None

    def setup(self):
        super().setup()
        if isinstance(self.connection, socket.socket):
            self.rfile.close()  # the plain socket file; reads go through the deadline
            self._reader = _DeadlineReader(self.connection, self.timeout)
            self.rfile = io.BufferedReader(self._reader)

    def handle_one_request(self):
        if self._reader is not None:
            self._reader.deadline = time.monotonic() + self.request_deadline
        super().handle_one_request()

    def allow_body(self, length: int) -> None:
        """Extend the read deadline for an accepted body of ``length`` bytes."""
        if self._reader is not None and self._reader.deadline is not None and length > 0:
            self._reader.deadline += length / self.min_body_rate

    def end_request_deadline(self) -> None:
        """The request is read; later reads use only the per-operation timeout."""
        if self._reader is not None:
            self._reader.deadline = None

    def log_message(self, format, *args):  # silence default logging
        pass

    def _send(self, status: int, body: bytes, ctype: str, *, cache: str = "no-store",
              etag: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if etag is not None:
            self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if getattr(self, "response_cookie", None):
            self.send_header("Set-Cookie", self.response_cookie)
        if getattr(self, "download_name", None):
            self.send_header("Content-Disposition", "attachment; filename=download; filename*=UTF-8''"
                             + quote(self.download_name, safe=""))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _failure(self, exc):
        """500; the exception text is shown only on a local server without accounts."""
        return self._error(500, "Request failed" if self.ctx.accounts is not None else str(exc))

    def _path(self) -> str:
        return urlsplit(self.path).path

    def _query(self) -> dict:
        return parse_qs(urlsplit(self.path).query)

    def _owner_id(self) -> str:
        return self.login.user.id if self.login is not None else "owner"

    def _is_admin(self) -> bool:
        return self.login is None or self.login.user.role == "admin"

    @staticmethod
    def _text_field(data, key, limit=MAX_TEXT):
        value = data.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{key} must be non-empty text of at most {limit} characters")
        return value
