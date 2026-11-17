"""web_http: resource limits, the response helpers and the route tables; the read deadline
uses a real local socket."""
import io
import socket
import threading
import time

import pytest

from igris import web
from igris.web_http import BaseHandler, BoundedHTTPServer
from igris.accounts import AccountStore
from igris.assistant import build_assistant
from igris.config import Settings
from test_web import FakeAssistant
from test_web_accounts import Connection, Server


class GuardedBody(io.BytesIO):
    """Request bytes whose body must never be read."""

    def read(self, size=-1):
        raise AssertionError("request body read before rejection")


class StalledBody(io.BytesIO):
    """A client that sent its headers and then stopped: the socket read times out."""

    def read(self, size=-1):
        raise TimeoutError("timed out")


def send(server, path, headers="", body_class=GuardedBody):
    raw = (f"POST {path} HTTP/1.0\r\nHost: 127.0.0.1:8770\r\n"
           f"Content-Type: application/json\r\n{headers}\r\n").encode()
    conn = Connection(raw)
    conn.input = body_class(raw)
    server.RequestHandlerClass(conn, ("127.0.0.1", 1), server)
    return conn


@pytest.fixture
def local(monkeypatch):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    return web.create_server(FakeAssistant(), port=0)


@pytest.fixture
def with_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    settings = Settings(backends=("echo",), slow_backends=("echo",), user_backends=("echo",),
                        data_dir=tmp_path / "server", sessions_dir=tmp_path / "sessions",
                        personas_dir=tmp_path / "personas")
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    accounts.bootstrap("admin-test-password", "user-test-password")
    return web.create_server(build_assistant(settings), port=0, accounts=accounts)


@pytest.mark.parametrize("path", ["/api/ask", "/api/files/upload", "/api/agenda/add"])
def test_unauthenticated_post_is_refused_before_its_body_is_read(with_accounts, path):
    conn = send(with_accounts, path, "Content-Length: 100\r\n")
    assert conn.output.startswith(b"HTTP/1.0 401")


def test_device_report_without_token_is_refused_before_its_body_is_read(local):
    conn = send(local, "/api/presence/report", "Content-Length: 100\r\n")
    assert conn.output.startswith(b"HTTP/1.0 404")


def test_wrong_device_token_is_refused_before_its_body_is_read(monkeypatch):
    from igris.presence import PresenceTracker

    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    server = web.create_server(FakeAssistant(), port=0, presence=PresenceTracker(),
                               edge_token="right-token")
    conn = send(server, "/api/presence/report",
                "Content-Length: 100\r\nAuthorization: Bearer wrong\r\n")
    assert conn.output.startswith(b"HTTP/1.0 401")


def test_oversized_body_is_refused_without_reading_it(local):
    conn = send(local, "/api/ask", f"Content-Length: {web.MAX_BODY + 1}\r\n")
    assert conn.output.startswith(b"HTTP/1.0 413")


def test_handler_has_a_read_timeout_and_drops_a_stalled_body(local):
    assert web.REQUEST_TIMEOUT == 30
    conn = send(local, "/api/ask", "Content-Length: 100\r\n", body_class=StalledBody)
    assert conn.timeout == web.REQUEST_TIMEOUT
    assert not conn.output  # timed out: closed without an answer


@pytest.fixture
def bare_server(monkeypatch):
    """BoundedHTTPServer without a socket; the base class records what it is asked."""
    base = web.ThreadingHTTPServer
    calls = {"accepted": [], "closed": []}
    monkeypatch.setattr(base, "__init__", lambda self, *args, **kwargs: None)
    monkeypatch.setattr(base, "process_request",
                        lambda self, request, address: calls["accepted"].append(request))
    monkeypatch.setattr(base, "process_request_thread", lambda self, request, address: None)
    monkeypatch.setattr(base, "shutdown_request",
                        lambda self, request: calls["closed"].append(request))
    return calls


def test_excess_connections_are_closed_and_slots_come_back(bare_server):
    server = web.BoundedHTTPServer(("127.0.0.1", 0), None, max_connections=1)
    server.process_request("first", None)
    server.process_request("excess", None)
    assert bare_server == {"accepted": ["first"], "closed": ["excess"]}
    server.process_request_thread("first", None)  # the first request finished
    server.process_request("next", None)
    assert bare_server["accepted"] == ["first", "next"]


def test_default_connection_cap(bare_server):
    server = web.BoundedHTTPServer(("127.0.0.1", 0), None)
    for n in range(web.MAX_CONNECTIONS + 1):
        server.process_request(n, None)
    assert len(bare_server["accepted"]) == web.MAX_CONNECTIONS == 64
    assert bare_server["closed"] == [web.MAX_CONNECTIONS]


def test_slot_is_released_when_a_thread_cannot_start(bare_server, monkeypatch):
    def fail(self, request, address):
        raise RuntimeError("cannot start")

    monkeypatch.setattr(web.ThreadingHTTPServer, "process_request", fail)
    server = web.BoundedHTTPServer(("127.0.0.1", 0), None, max_connections=1)
    for _ in range(2):  # a leaked slot would turn the second call into a refusal
        with pytest.raises(RuntimeError, match="cannot start"):
            server.process_request(None, None)


def test_connection_cap_must_be_positive(bare_server):
    with pytest.raises(ValueError):
        web.BoundedHTTPServer(("127.0.0.1", 0), None, max_connections=0)


def test_text_field():
    from igris.web_http import BaseHandler

    assert BaseHandler._text_field({"a": "x"}, "a") == "x"
    for bad in ({}, {"a": " "}, {"a": 3}, {"a": "x" * 9}):
        with pytest.raises(ValueError):
            BaseHandler._text_field(bad, "a", 8)


def test_route_tables_keep_every_path():
    """The split must not add, drop or rename a route."""
    assert set(web.POST_ROUTES) == {
        "/api/ask", "/api/session", "/api/persona", "/api/multiagent/jobs",
        "/api/multiagent/cancel", "/api/files/upload", "/api/files/delete",
        "/api/tasks/submit", "/api/tasks/cancel", "/api/tasks/resume",
        "/api/presence/report", "/api/approvals/answer", "/api/notices/read",
        "/api/auth/login", "/api/auth/logout", "/api/core/add", "/api/core/page",
        "/api/study/attempt", "/api/study/explain", "/api/study/chat", "/api/auth/password",
        "/api/models/benchmark", "/api/agenda/add", "/api/agenda/status", "/api/agenda/delete",
        "/api/prefs"}
    assert set(web.GET_ROUTES) == {
        "/calis", "/api/core", "/api/study", "/api/tasks", "/api/files", "/api/presence",
        "/api/calendar", "/api/agenda", "/api/agenda/month", "/api/prefs", "/api/inbox",
        "/api/approvals", "/api/notices", "/api/auth/me", "/api/admin/users", "/api/models",
        "/api/state", "/api/history", "/api/roadmap"}
    assert [p for p, _ in web.GET_PREFIXES] == [
        "/api/core/", "/api/study/", "/api/tasks/", "/api/files/", "/api/multiagent/", "/static/"]


# --- total read deadline, with a real socket on 127.0.0.1 ---------------------------

class DeadlineHandler(BaseHandler):
    request_deadline = 0.4
    min_body_rate = 1000  # bytes per extra second of body deadline
    timeout = 5  # the per-read timeout alone would let a trickle go on

    def do_GET(self):
        if self.path == "/stream":
            self.send_response(200)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.end_request_deadline()
            for _ in range(6):  # stays open well past the read deadline
                self.wfile.write(b"tick\n")
                self.wfile.flush()
                time.sleep(0.15)
            return
        self._send(200, b"ok", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.allow_body(length)
        body = self.rfile.read(length)
        self._send(200, body, "text/plain")


@pytest.fixture
def live():
    server = BoundedHTTPServer(("127.0.0.1", 0), DeadlineHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address
    server.shutdown()
    server.server_close()


def trickle(address, chunks, pause):
    """Send ``chunks`` one by one; return (seconds until the server closed, bytes read)."""
    sock = socket.create_connection(address, timeout=5)
    start = time.monotonic()
    try:
        for chunk in chunks:
            try:
                sock.sendall(chunk)
            except OSError:
                break
            time.sleep(pause)
        received = b""
        try:
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                received += data
        except OSError:
            pass
        return time.monotonic() - start, received
    finally:
        sock.close()


def test_trickled_headers_are_cut_off_at_the_request_deadline(live):
    head = b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\n" + b"X-Slow: " + b"a" * 40
    elapsed, received = trickle(live, [bytes([b]) for b in head], 0.05)
    assert received == b""  # closed without an answer
    assert elapsed < 40 * 0.05  # long before the trickle could finish


def test_trickled_body_is_cut_off_at_its_deadline(live):
    head = b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 40\r\n\r\n"
    # 40 bytes allow 0.4 s + 0.04 s; one byte every 0.05 s needs 2 s
    elapsed, received = trickle(live, [head] + [b"x"] * 40, 0.05)
    assert received == b""
    assert elapsed < 40 * 0.05


def test_a_prompt_request_is_answered(live):
    head = b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
    _, received = trickle(live, [head], 0)
    assert received.startswith(b"HTTP/1.0 200") and received.endswith(b"hello")


def test_a_started_stream_outlives_the_read_deadline(live):
    head = b"GET /stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
    elapsed, received = trickle(live, [head], 0)
    assert received.count(b"tick") == 6
    assert elapsed > DeadlineHandler.request_deadline
