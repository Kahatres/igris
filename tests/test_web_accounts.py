"""Exercise the real HTTP handlers with in-memory connections, never sockets."""

from http.client import HTTPResponse
from http.cookies import SimpleCookie
import io
import base64
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from igris.accounts import AccountStore
from igris.assistant import build_assistant
from igris.config import Settings
from igris import web


class Connection:
    def __init__(self, data):
        self.input = io.BytesIO(data)
        self.output = bytearray()

    def settimeout(self, timeout):
        self.timeout = timeout

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class Server:
    def __init__(self, address, handler):
        self.server_address = (address[0], address[1] or 8770)
        self.RequestHandlerClass = handler


class Browser:
    def __init__(self, server):
        self.server = server
        self.cookie = ""
        self.csrf = ""

    def request(self, method, path, data=None, headers=None):
        body = json.dumps(data).encode() if data is not None else b""
        fields = {"Host": "127.0.0.1:8770", "Content-Type": "application/json",
                  "Content-Length": str(len(body)), "Cookie": self.cookie,
                  "X-CSRF-Token": self.csrf}
        fields.update(headers or {})
        raw = f"{method} {path} HTTP/1.0\r\n".encode()
        raw += "".join(f"{k}: {v}\r\n" for k, v in fields.items()).encode()
        connection = Connection(raw + b"\r\n" + body)
        self.server.RequestHandlerClass(connection, ("127.0.0.1", 1234), self.server)
        response = HTTPResponse(Connection(bytes(connection.output)))
        response.begin()
        payload = response.read()
        if response.getheader("Set-Cookie"):
            cookie = SimpleCookie(response.getheader("Set-Cookie"))
            self.cookie = "igris_session=" + cookie["igris_session"].value
        if response.getheader("Content-Type", "").startswith("application/json"):
            payload = json.loads(payload)
        return response.status, payload, response

    def login(self, name, password):
        assert self.request("POST", "/api/auth/login", {"username": name, "password": password})[0] == 200
        status, identity, _ = self.request("GET", "/api/auth/me")
        assert status == 200
        self.csrf = identity["csrf"]
        return identity


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    settings = Settings(backends=("echo",), slow_backends=("echo",), user_backends=("echo",),
                        data_dir=tmp_path / "server", sessions_dir=tmp_path / "owner-sessions",
                        personas_dir=tmp_path / "owner-personas")
    owner = build_assistant(settings)
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    accounts.bootstrap("admin-test-password", "guest-test-password")
    server = web.create_server(owner, port=0, accounts=accounts)
    return SimpleNamespace(server=server, owner=owner, accounts=accounts)


def test_anonymous_cannot_read_private_apis(env):
    browser = Browser(env.server)
    assert b"login-form" in browser.request("GET", "/")[1]
    for path in ("/api/state", "/api/history", "/api/roadmap", "/api/models", "/api/admin/users",
                 "/api/multiagent/runs", "/api/multiagent/live", "/api/auth/me"):
        assert browser.request("GET", path)[0] == 401
    assert browser.request("POST", "/api/ask", {"text": "private"})[0] == 401


def test_models_are_readable_by_user_but_benchmark_action_is_owner_only(env):
    user = Browser(env.server)
    user.login("Guest", "guest-test-password")
    status, payload, _ = user.request("GET", "/api/models")
    assert status == 200 and "providers" in payload and "domains" in payload
    assert user.request("POST", "/api/models/benchmark", {"domain": "code"})[0] == 403
    owner = Browser(env.server)
    owner.login("admin", "admin-test-password")
    assert owner.request("POST", "/api/models/benchmark", {"domain": "code"})[0] == 503


def test_owner_benchmark_runner_is_explicit_and_csrf_protected(env, monkeypatch):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    calls = []
    server = web.create_server(env.owner, port=0, accounts=env.accounts,
                               benchmark_runner=lambda domain: calls.append(domain) or {"ok": True})
    owner = Browser(server)
    owner.login("admin", "admin-test-password")
    assert owner.request("POST", "/api/models/benchmark", {"domain": "code"},
                         {"X-CSRF-Token": "wrong"})[0] == 403
    assert owner.request("POST", "/api/models/benchmark", {"domain": "unknown"})[0] == 400
    assert owner.request("POST", "/api/models/benchmark", {"domain": "code"})[0] == 200
    assert calls == ["code"]


def test_cookie_flags_login_logout_and_replay(env):
    browser = Browser(env.server)
    _, _, response = browser.request("POST", "/api/auth/login",
        {"username": "admin", "password": "admin-test-password"})
    cookie = response.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie
    old_cookie = browser.cookie
    browser.csrf = browser.request("GET", "/api/auth/me")[1]["csrf"]
    assert browser.request("POST", "/api/auth/logout", {})[0] == 200
    assert browser.request("GET", "/api/history", headers={"Cookie": old_cookie})[0] == 401


def test_csrf_and_origin_checks_do_not_write_memory(env):
    browser = Browser(env.server)
    browser.login("admin", "admin-test-password")
    assert browser.request("POST", "/api/ask", {"text": "bad"},
                           {"X-CSRF-Token": ""})[0] == 403
    assert browser.request("POST", "/api/ask", {"text": "bad"},
                           {"Origin": "http://attacker.example"})[0] == 403
    assert len(env.owner.memory) == 0


def test_owner_and_guest_have_independent_history_and_roles(env):
    admin, user = Browser(env.server), Browser(env.server)
    assert admin.login("admin", "admin-test-password")["user"]["role"] == "admin"
    assert user.login("Guest", "guest-test-password")["user"]["display_name"] == "Guest"
    admin.request("POST", "/api/ask", {"text": "owner-only-message"})
    user.request("POST", "/api/ask", {"text": "user-only-message"})
    assert "owner-only-message" not in str(user.request("GET", "/api/history")[1])
    assert "user-only-message" not in str(admin.request("GET", "/api/history")[1])
    assert "password_hash" not in str(admin.request("GET", "/api/admin/users")[1])
    for method, path, body in (
        ("GET", "/api/admin/users", None), ("GET", "/api/multiagent/runs", None),
        ("GET", "/api/multiagent/run/20260101_101010", None),
        ("POST", "/api/multiagent/jobs", {"goal": "change host files", "implement": True}),
        ("POST", "/api/multiagent/cancel", {"id": "owner-job"}),
    ):
        assert user.request(method, path, body)[0] == 403


def test_two_browsers_same_account_keep_separate_conversation_and_persona(env):
    one, two = Browser(env.server), Browser(env.server)
    one.login("guest", "guest-test-password")
    two.login("guest", "guest-test-password")
    assert one.request("POST", "/api/session", {"name": "project"})[0] == 200
    one.request("POST", "/api/persona", {"name": "coder"})
    assert one.request("GET", "/api/state")[1]["session"] == "project"
    assert two.request("GET", "/api/state")[1]["session"] == "default"
    assert two.request("GET", "/api/state")[1]["persona"] == "default"
    assert one.request("GET", "/api/state")[1]["persona"] == "coder"


def test_password_change_logs_out_all_browsers(env):
    browser = Browser(env.server)
    browser.login("admin", "admin-test-password")
    assert browser.request("POST", "/api/auth/password", {
        "current_password": "admin-test-password", "password": "new-test-password"})[0] == 200
    assert browser.request("GET", "/api/state")[0] == 401
    browser.login("admin", "new-test-password")


def test_setup_required_and_unauthenticated_remote_bind_rejected(env, tmp_path):
    with pytest.raises(ValueError, match="setup-users"):
        web.create_server(env.owner, accounts=AccountStore(tmp_path / "empty.sqlite3"))
    with pytest.raises(ValueError, match="requires accounts"):
        web.create_server(env.owner, host="100.64.0.7")


def test_secure_cookie_option_and_host_rejection(env):
    secure = web.create_server(env.owner, accounts=env.accounts, cookie_secure=True)
    browser = Browser(secure)
    status, _, response = browser.request("POST", "/api/auth/login", {
        "username": "admin", "password": "admin-test-password"},
        {"Origin": "https://127.0.0.1:8770"})
    assert status == 200 and "Secure" in response.getheader("Set-Cookie")
    assert browser.request("GET", "/api/state", headers={"Host": "attacker.example"})[0] == 403


def test_public_origin_requires_exact_host_and_origin(env):
    server = web.create_server(env.owner, accounts=env.accounts,
                               public_origin="https://igris.example.com")
    browser = Browser(server)
    assert browser.request("GET", "/")[0] == 403
    headers = {"Host": "igris.example.com", "Origin": "https://igris.example.com"}
    status, _, response = browser.request("POST", "/api/auth/login",
        {"username": "admin", "password": "admin-test-password"}, headers)
    assert status == 200 and "Secure" in response.getheader("Set-Cookie")
    headers["Origin"] = "https://attacker.example"
    assert browser.request("POST", "/api/auth/login", {}, headers)[0] == 403


def test_file_upload_download_delete_and_cross_user_access(env):
    admin, user = Browser(env.server), Browser(env.server)
    admin.login("admin", "admin-test-password")
    user.login("guest", "guest-test-password")
    payload = {"name": "notlar.html", "content": base64.b64encode(b"<script>bad()</script>").decode()}
    status, uploaded, _ = user.request("POST", "/api/files/upload", payload)
    assert status == 201
    path = "/api/files/" + uploaded["id"] + "/download"
    status, content, response = user.request("GET", path)
    assert status == 200 and content == b"<script>bad()</script>"
    assert response.getheader("Content-Type") == "application/octet-stream"
    assert response.getheader("Content-Disposition").startswith("attachment;")
    assert admin.request("GET", path)[0] == 404
    assert admin.request("POST", "/api/files/delete", {"id": uploaded["id"]})[0] == 404
    assert admin.request("GET", "/api/files")[1]["files"] == []
    assert user.request("POST", "/api/files/delete", {"id": uploaded["id"]})[0] == 200
    assert user.request("GET", path)[0] == 404
    assert user.request("GET", "/api/files/events")[1]["events"][0]["action"] == "delete"


def test_file_upload_auth_csrf_and_validation(env):
    browser = Browser(env.server)
    assert browser.request("GET", "/api/files")[0] == 401
    assert browser.request("POST", "/api/files/upload", {})[0] == 401
    browser.login("guest", "guest-test-password")
    assert browser.request("POST", "/api/files/upload", {}, {"X-CSRF-Token": ""})[0] == 403
    assert browser.request("POST", "/api/files/upload", {"name": "x", "content": "!"})[0] == 400
    assert browser.request("POST", "/api/files/upload", {"name": "../x", "content": "eA=="})[0] == 400
    assert browser.request("GET", "/api/files")[1]["files"] == []


def test_user_tasks_auth_ownership_and_cancel(env):
    admin, user = Browser(env.server), Browser(env.server)
    assert user.request("GET", "/api/tasks")[0] == 401
    admin.login("admin", "admin-test-password")
    user.login("guest", "guest-test-password")
    assert user.request("POST", "/api/tasks/submit", {"prompt": "x"}, {"X-CSRF-Token": ""})[0] == 403
    status, task, _ = user.request("POST", "/api/tasks/submit", {"prompt": "private work"})
    assert status == 202
    assert admin.request("GET", "/api/tasks/" + task["id"])[0] == 404
    assert admin.request("POST", "/api/tasks/cancel", {"id": task["id"]})[0] == 404
    assert admin.request("GET", "/api/tasks")[1]["tasks"] == []
    assert user.request("POST", "/api/tasks/cancel", {"id": task["id"]})[1]["status"] == "cancelled"


def test_background_task_completes_without_writing_chat(env):
    browser = Browser(env.server)
    browser.login("guest", "guest-test-password")
    task = browser.request("POST", "/api/tasks/submit", {"prompt": "background result"})[1]
    env.server.poll_jobs()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        detail = browser.request("GET", "/api/tasks/" + task["id"])[1]
        if detail["status"] == "done":
            break
        threading.Event().wait(0.01)
    assert detail["status"] == "done" and "background result" in detail["result"]
    assert browser.request("GET", "/api/history")[1]["messages"] == []


def test_login_lockout_returns_429_even_for_correct_password(env):
    browser = Browser(env.server)
    for _ in range(5):
        assert browser.request("POST", "/api/auth/login",
                               {"username": "admin", "password": "wrong-password"})[0] == 401
    status, payload, response = browser.request("POST", "/api/auth/login",
        {"username": "admin", "password": "admin-test-password"})
    assert status == 429 and "attempts" in payload["error"]
    assert response.getheader("Set-Cookie") is None
    # the account limit is per account: the other account still logs in
    browser.login("guest", "guest-test-password")


def test_expired_session_is_rejected_by_every_api(env):
    from igris.accounts import SESSION_SECONDS

    browser = Browser(env.server)
    browser.login("guest", "guest-test-password")
    start = time.time()
    env.accounts.clock = lambda: start + SESSION_SECONDS + 1
    assert browser.request("GET", "/api/state")[0] == 401
    assert browser.request("GET", "/api/files")[0] == 401
    assert browser.request("GET", "/api/tasks")[0] == 401
    assert browser.request("POST", "/api/ask", {"text": "after expiry"})[0] == 401
    assert b"login-form" in browser.request("GET", "/")[1]


@pytest.mark.parametrize("path, body", [
    ("/api/ask", {"text": "x"}),
    ("/api/session", {"name": "forged"}),
    ("/api/persona", {"name": "coder"}),
    ("/api/auth/logout", {}),
    ("/api/auth/password", {"current_password": "guest-test-password",
                            "password": "attacker-chosen-password"}),
    ("/api/files/upload", {"name": "x.txt", "content": "eA=="}),
    ("/api/files/delete", {"id": "0" * 32}),
    ("/api/tasks/submit", {"prompt": "x"}),
    ("/api/tasks/cancel", {"id": "0" * 32}),
])
def test_csrf_failure_rejects_every_post_without_side_effects(env, path, body):
    browser = Browser(env.server)
    browser.login("guest", "guest-test-password")
    good = browser.csrf
    for token in ("", "wrong-token", good + "x"):
        browser.csrf = token
        assert browser.request("POST", path, body)[0] == 403
    browser.csrf = good
    state = browser.request("GET", "/api/state")[1]
    assert state["session"] == "default" and state["persona"] == "default"
    assert browser.request("GET", "/api/tasks")[1]["tasks"] == []
    assert browser.request("GET", "/api/files")[1]["files"] == []
    assert browser.request("GET", "/api/history")[1]["messages"] == []
    assert browser.request("POST", "/api/auth/logout", {})[0] == 200
    browser.login("guest", "guest-test-password")


def test_csrf_token_of_other_session_is_rejected(env):
    one, two = Browser(env.server), Browser(env.server)
    one.login("guest", "guest-test-password")
    two.login("guest", "guest-test-password")
    assert one.csrf != two.csrf
    one.csrf = two.csrf
    assert one.request("POST", "/api/session", {"name": "x"})[0] == 403


def test_logout_invalidates_only_that_session(env):
    one, two = Browser(env.server), Browser(env.server)
    one.login("admin", "admin-test-password")
    two.login("admin", "admin-test-password")
    old_cookie, old_csrf = one.cookie, one.csrf
    _, _, response = one.request("POST", "/api/auth/logout", {})
    assert "Max-Age=0" in response.getheader("Set-Cookie")
    replay = {"Cookie": old_cookie, "X-CSRF-Token": old_csrf}
    assert one.request("GET", "/api/state", headers=replay)[0] == 401
    assert one.request("POST", "/api/ask", {"text": "x"}, replay)[0] == 401
    assert two.request("GET", "/api/state")[0] == 200


def test_password_change_invalidates_other_browsers_and_rejects_bad_input(env):
    one, two, admin = Browser(env.server), Browser(env.server), Browser(env.server)
    one.login("guest", "guest-test-password")
    two.login("guest", "guest-test-password")
    admin.login("admin", "admin-test-password")
    assert one.request("POST", "/api/auth/password", {
        "current_password": "wrong-password", "password": "new-test-password"})[0] == 400
    assert one.request("POST", "/api/auth/password", {
        "current_password": "guest-test-password", "password": "short"})[0] == 400
    assert two.request("GET", "/api/state")[0] == 200
    assert one.request("POST", "/api/auth/password", {
        "current_password": "guest-test-password", "password": "new-test-password"})[0] == 200
    assert one.request("GET", "/api/state")[0] == 401
    assert two.request("GET", "/api/state")[0] == 401
    assert admin.request("GET", "/api/state")[0] == 200
    assert Browser(env.server).request("POST", "/api/auth/login", {
        "username": "guest", "password": "guest-test-password"})[0] == 401


def test_public_origin_rejects_wrong_host_and_origin_on_every_route(env):
    server = web.create_server(env.owner, accounts=env.accounts,
                               public_origin="https://igris.example.com")
    browser = Browser(server)
    good = {"Host": "igris.example.com", "Origin": "https://igris.example.com"}
    assert browser.request("POST", "/api/auth/login",
        {"username": "admin", "password": "admin-test-password"}, good)[0] == 200
    browser.csrf = browser.request("GET", "/api/auth/me", headers=good)[1]["csrf"]
    for host in ("127.0.0.1:8770", "localhost:8770", "igris.example.com.attacker.example",
                 "igris.example.com:8443", ""):
        assert browser.request("GET", "/api/state", headers={"Host": host})[0] == 403
        assert browser.request("POST", "/api/ask", {"text": "x"},
                               {"Host": host, "Origin": good["Origin"]})[0] == 403
    for origin in ("http://igris.example.com", "https://igris.example.com:8443",
                   "https://attacker.example", "null"):
        assert browser.request("POST", "/api/session", {"name": "x"},
                               {"Host": good["Host"], "Origin": origin})[0] == 403
    state = browser.request("GET", "/api/state", headers={"Host": good["Host"]})[1]
    assert state["session"] == "default" and len(env.owner.memory) == 0
    assert browser.request("POST", "/api/session", {"name": "ok"}, good)[0] == 200


@pytest.mark.parametrize("method, path, body", [
    ("GET", "/api/admin/users", None),
    ("GET", "/api/multiagent/jobs", None),
    ("GET", "/api/multiagent/runs", None),
    ("GET", "/api/multiagent/live", None),
    ("GET", "/api/multiagent/run/20260101_101010", None),
    ("POST", "/api/multiagent/jobs", {"goal": "change host files", "implement": True,
                                      "test": "pytest -q"}),
    ("POST", "/api/multiagent/cancel", {"id": "owner-job"}),
])
def test_owner_only_endpoint_returns_403_for_regular_user(env, method, path, body):
    user = Browser(env.server)
    user.login("guest", "guest-test-password")
    status, payload, _ = user.request(method, path, body)
    assert status == 403 and payload == {"error": "Administrator access required"}
    admin = Browser(env.server)
    admin.login("admin", "admin-test-password")
    assert admin.request(method, path, body)[0] != 403


def test_regular_user_never_gets_host_cli_backends(tmp_path, monkeypatch):
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    settings = Settings(backends=("claude",), slow_backends=("claude", "codex"),
                        user_backends=("echo",), data_dir=tmp_path / "server",
                        sessions_dir=tmp_path / "s", personas_dir=tmp_path / "p")
    owner = build_assistant(settings)
    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    accounts.bootstrap("admin-test-password", "guest-test-password")
    user = Browser(web.create_server(owner, accounts=accounts))
    user.login("guest", "guest-test-password")
    assert user.request("GET", "/api/state")[1]["backends"] == ["echo"]
    status, reply, _ = user.request("POST", "/api/ask", {"text": "long job", "slow": True})
    assert status == 200 and reply["backends"] == ["echo"] and reply["job_id"] is None


EDGE_TOKEN = "edge-test-token"


def edge_server(env, monkeypatch, **options):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    return web.create_server(env.owner, port=0, accounts=env.accounts, edge_token=EDGE_TOKEN, **options)


def edge_post(server, data, token=EDGE_TOKEN):
    return Browser(server).request("POST", "/api/presence/report", data,
                                   {"Authorization": f"Bearer {token}"})


def test_device_report_needs_the_device_token_and_reaches_the_owner_only(env, monkeypatch):
    server = edge_server(env, monkeypatch)
    report = {"device": "desktop", "category": "coding", "idle_seconds": 3}
    assert edge_post(server, report, "wrong")[0] == 401
    assert edge_post(server, report)[0] == 200

    owner = Browser(server)
    owner.login("admin", "admin-test-password")
    status, payload, _ = owner.request("GET", "/api/presence")
    assert status == 200 and payload["state"] == "deep_work" and payload["device"] == "desktop"

    user = Browser(server)
    user.login("Guest", "guest-test-password")
    assert user.request("GET", "/api/presence")[0] == 403
    assert Browser(server).request("GET", "/api/presence")[0] == 401


def test_device_report_is_closed_without_a_configured_token(env):
    assert edge_post(env.server, {"device": "d", "category": "coding", "idle_seconds": 0})[0] == 404


@pytest.mark.parametrize("report", [
    {"device": "d", "category": "games", "idle_seconds": 0},
    {"device": "", "category": "coding", "idle_seconds": 0},
    {"device": "d" * 65, "category": "coding", "idle_seconds": 0},
    {"device": "d", "category": "coding", "idle_seconds": "0"},
    {"device": "d", "category": "coding", "idle_seconds": -1},
    {"device": 5, "category": "coding", "idle_seconds": 0},
    {"device": "d", "category": ["coding"], "idle_seconds": 0},
])
def test_device_report_rejects_bad_values(env, monkeypatch, report):
    assert edge_post(edge_server(env, monkeypatch), report)[0] == 400


def test_device_clock_is_not_trusted(env, monkeypatch):
    server = edge_server(env, monkeypatch)
    edge_post(server, {"device": "d", "category": "coding", "idle_seconds": 0, "at": 1})
    owner = Browser(server)
    owner.login("admin", "admin-test-password")
    assert owner.request("GET", "/api/presence")[1]["state"] == "deep_work"


def test_approval_answer_is_owner_scoped_and_wakes_the_waiting_task(env):
    owner, user = Browser(env.server), Browser(env.server)
    owner_id = owner.login("admin", "admin-test-password")["user"]["id"]
    user.login("Guest", "guest-test-password")
    flow = env.server.approvals
    request_id = flow.request(owner_id, "mail", "Send it?", payload={"to": "x"})

    assert [a["summary"] for a in owner.request("GET", "/api/approvals")[1]["approvals"]] == ["Send it?"]
    assert user.request("GET", "/api/approvals")[1]["approvals"] == []
    assert user.request("POST", "/api/approvals/answer", {"id": request_id, "approved": True})[0] == 400
    assert owner.request("POST", "/api/approvals/answer", {"id": request_id, "approved": "true"})[0] == 400
    assert flow.store.status(request_id) == "pending"

    assert owner.request("POST", "/api/approvals/answer", {"id": request_id, "approved": True})[0] == 200
    assert flow.store.status(request_id) == "approved"
    assert owner.request("POST", "/api/approvals/answer", {"id": request_id, "approved": False})[0] == 400


def test_approval_answer_needs_a_csrf_token(env):
    owner = Browser(env.server)
    owner_id = owner.login("admin", "admin-test-password")["user"]["id"]
    request_id = env.server.approvals.request(owner_id, "mail", "Send it?", payload={})
    owner.csrf = "wrong"
    assert owner.request("POST", "/api/approvals/answer", {"id": request_id, "approved": True})[0] == 403


def test_answering_an_approval_resumes_the_task_waiting_for_it(env, monkeypatch):
    from igris.tasks import TaskQueue, WaitForEvent

    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    seen, finished = [], threading.Event()

    def runner(task):
        if task["resume"] is None:
            return WaitForEvent("approval:" + holder["id"])
        seen.append(task["resume"])
        finished.set()
        return "sent"

    holder = {}
    queue = TaskQueue(env.owner.settings.data_dir / "flow.sqlite3", runner)
    server = web.create_server(env.owner, port=0, accounts=env.accounts, tasks=queue)
    owner = Browser(server)
    owner_id = owner.login("admin", "admin-test-password")["user"]["id"]
    holder["id"] = server.approvals.request(owner_id, "mail", "Send it?", payload={})
    task = queue.submit(owner_id, "send the mail")
    queue.poll()
    deadline = time.time() + 5
    while queue.get(owner_id, task["id"])["status"] != "waiting_for_event" and time.time() < deadline:
        time.sleep(0.01)

    assert owner.request("POST", "/api/approvals/answer", {"id": holder["id"], "approved": False})[0] == 200
    queue.poll()
    assert finished.wait(5)
    assert seen == [{"answer": None, "payload": "denied", "wake_reason": "event"}]


def test_task_resume_endpoint_is_owner_scoped(env, monkeypatch):
    from igris.tasks import TaskQueue, WaitForUser

    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    queue = TaskQueue(env.owner.settings.data_dir / "ask.sqlite3", lambda task: WaitForUser("Which file?"))
    server = web.create_server(env.owner, port=0, accounts=env.accounts, tasks=queue)
    owner, user = Browser(server), Browser(server)
    owner_id = owner.login("admin", "admin-test-password")["user"]["id"]
    user.login("Guest", "guest-test-password")
    task = queue.submit(owner_id, "work")
    queue.poll()
    deadline = time.time() + 5
    while queue.get(owner_id, task["id"])["status"] != "waiting_for_user" and time.time() < deadline:
        time.sleep(0.01)

    assert user.request("POST", "/api/tasks/resume", {"id": task["id"], "answer": "a"})[0] == 404
    assert owner.request("POST", "/api/tasks/resume", {"id": task["id"], "answer": ""})[0] == 400
    status, payload, _ = owner.request("POST", "/api/tasks/resume", {"id": task["id"], "answer": "a.txt"})
    assert status == 200 and payload["status"] == "queued" and payload["resume_answer"] == "a.txt"


def test_channel_settings_use_the_names_notify_reads():
    from igris.notify import TelegramNotifier, from_env

    stored = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "42"}
    settings = web._channel_settings(stored.get)
    assert isinstance(from_env(settings), TelegramNotifier)
    assert web._channel_settings({}.get) == {} and from_env({}) is None


def test_answer_pump_survives_a_failure_and_reports_it_once(capsys):
    calls = []
    stop = threading.Event()

    class Flow:
        def pump(self):
            calls.append(1)
            if len(calls) == 3:
                stop.set()
            raise RuntimeError("channel is down")

    web._pump_answers(Flow(), stop, interval=0)
    assert len(calls) == 3
    assert capsys.readouterr().err.count("channel is down") == 1


class FakeInbox:
    def __init__(self, folder):
        from igris.inbox import InboxStore
        self.store = InboxStore(folder / "inbox.sqlite3")
        self.store.add({"id": "m1", "seen_at": "2026-11-07T10:00:00", "sender": "A <a@x.com>",
                        "subject": "Merhaba", "label": "routine", "risk": "low", "flags": "",
                        "spf": "pass", "dkim": "pass", "dmarc": "pass"}, notify=False)

    def status(self):
        return {"state": "ok", "last_poll": "2026-11-07T10:00:00", "error": None}


def test_inbox_is_owner_only(env, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    server = web.create_server(env.owner, port=0, accounts=env.accounts, inbox=FakeInbox(tmp_path))
    user = Browser(server)
    user.login("guest", "guest-test-password")
    assert user.request("GET", "/api/inbox")[0] == 403
    owner = Browser(server)
    owner.login("admin", "admin-test-password")
    status, payload, _ = owner.request("GET", "/api/inbox")
    assert status == 200 and payload["status"]["state"] == "ok"
    assert payload["mails"][0]["subject"] == "Merhaba"
    assert {"category", "reason", "task", "origin"} <= set(payload["mails"][0])


def test_inbox_without_watcher_reports_disabled(env):
    owner = Browser(env.server)
    owner.login("admin", "admin-test-password")
    status, payload, _ = owner.request("GET", "/api/inbox")
    assert status == 200 and payload == {"status": {"state": "disabled"}, "mails": []}


def test_build_inbox_waits_for_a_sign_in_and_survives_bad_rules(tmp_path):
    settings = Settings(data_dir=tmp_path)
    (tmp_path / "mail_rules.json").write_text("{not json", encoding="utf-8")
    secrets = {"GMAIL_CLIENT_ID": "cid", "GMAIL_CLIENT_SECRET": "sec"}
    watcher = web.build_inbox(settings, secrets.get, None)
    assert watcher.poll() == 0 and watcher.status()["state"] == "not_configured"


def test_watch_inbox_polls_until_stopped():
    import threading

    class Watcher:
        polls = 0

        def poll(self):
            self.polls += 1
            if self.polls == 2:
                stop.set()

        def status(self):
            return {"error": None}

    stop = threading.Event()
    watcher = Watcher()
    web._watch_inbox(watcher, stop, 0)
    assert watcher.polls == 2


def test_v2_page_needs_a_login(env):
    browser = Browser(env.server)
    assert b"login-form" in browser.request("GET", "/")[1]
    for old in ("/v2", "/klasik-eski"):
        status, _, resp = browser.request("GET", old)
        assert status == 302 and resp.getheader("Location") == "/", old
    browser.login("Guest", "guest-test-password")
    page = browser.request("GET", "/")[1]
    assert b"/static/v2.js" in page and b"login-form" not in page
    status, _, resp = browser.request("GET", "/klasik-eski")
    assert status == 302 and resp.getheader("Location") == "/"


def test_login_page_has_its_own_stylesheet():
    static = Path(web.__file__).parent / "static"
    page = (static / "login.html").read_text(encoding="utf-8")
    assert "/static/login.css" in page and "app.css" not in page
    css = (static / "login.css").read_text(encoding="utf-8")
    assert css.strip()
    assert "url(" not in css and "@import" not in css


def test_view_preference_is_stored_per_user(env):
    owner, user = Browser(env.server), Browser(env.server)
    assert owner.request("GET", "/api/prefs")[0] == 401
    owner.login("admin", "admin-test-password")
    user.login("Guest", "guest-test-password")
    assert owner.request("GET", "/api/prefs")[:2] == (200, {"view": "odak"})
    assert owner.request("POST", "/api/prefs", {"view": "klasik"})[:2] == (200, {"view": "klasik"})
    assert owner.request("GET", "/api/prefs")[1] == {"view": "klasik"}
    assert user.request("GET", "/api/prefs")[1] == {"view": "odak"}
    assert user.request("POST", "/api/prefs", {"view": "odak"})[0] == 200
    assert owner.request("GET", "/api/prefs")[1] == {"view": "klasik"}
    files = sorted(p.name for p in (env.owner.settings.data_dir / "prefs").iterdir())
    assert len(files) == 2 and all(f.endswith(".json") for f in files)


def test_view_preference_rejects_bad_values_and_needs_csrf(env):
    owner = Browser(env.server)
    owner.login("admin", "admin-test-password")
    for bad in ({"view": "wheel"}, {"view": 1}, {}):
        assert owner.request("POST", "/api/prefs", bad)[0] == 400, bad
    assert owner.request("POST", "/api/prefs", {"view": "klasik"}, {"X-CSRF-Token": "wrong"})[0] == 403
    assert owner.request("GET", "/api/prefs")[1] == {"view": "odak"}


def test_broken_preference_file_falls_back_to_odak(env):
    owner = Browser(env.server)
    identity = owner.login("admin", "admin-test-password")
    folder = env.owner.settings.data_dir / "prefs"
    folder.mkdir(parents=True)
    (folder / (identity["user"]["id"] + ".json")).write_text("{oops", encoding="utf-8")
    assert owner.request("GET", "/api/prefs")[1] == {"view": "odak"}


def test_event_stream_and_retired_endpoints_need_a_login(env):
    browser = Browser(env.server)
    assert browser.request("GET", "/api/events")[0] == 401
    assert browser.request("GET", "/api/forge")[0] == 401


def test_calendar_is_for_the_owner_only(env):
    user = Browser(env.server)
    user.login("Guest", "guest-test-password")
    assert user.request("GET", "/api/calendar")[0] == 403
    owner = Browser(env.server)
    owner.login("admin", "admin-test-password")
    status, payload, _ = owner.request("GET", "/api/calendar")
    assert status == 200 and payload["status"] == "disabled"


def test_notices_are_owner_only_and_marking_them_read_needs_csrf(env):
    owner, user = Browser(env.server), Browser(env.server)
    owner_id = owner.login("admin", "admin-test-password")["user"]["id"]
    user.login("Guest", "guest-test-password")
    store = env.server.notices
    first = store.add(owner_id, "reminder", "Bir")
    store.add(owner_id, "job_done", "İki", ref="j1")

    status, payload, _ = owner.request("GET", "/api/notices")
    assert status == 200 and payload["unread"] == 2
    assert [n["title"] for n in payload["notices"]] == ["İki", "Bir"]
    assert user.request("GET", "/api/notices")[0] == 403
    assert user.request("POST", "/api/notices/read", {"all": True})[0] == 403

    owner.csrf, csrf = "wrong", owner.csrf
    assert owner.request("POST", "/api/notices/read", {"all": True})[0] == 403
    owner.csrf = csrf
    assert owner.request("POST", "/api/notices/read", {"ids": "x"})[0] == 400
    assert owner.request("POST", "/api/notices/read", {"ids": []})[0] == 400
    assert owner.request("POST", "/api/notices/read", {"ids": [first["id"]]})[1] == {"marked": 1}
    status, payload, _ = owner.request("GET", "/api/notices?unread=1")
    assert payload["unread"] == 1 and [n["title"] for n in payload["notices"]] == ["İki"]
    assert owner.request("POST", "/api/notices/read", {"all": True})[1] == {"marked": 1}
    assert owner.request("GET", "/api/notices")[1]["unread"] == 0


def test_server_approvals_write_owner_notices(env):
    owner = Browser(env.server)
    owner_id = owner.login("admin", "admin-test-password")["user"]["id"]
    request_id = env.server.approvals.request(owner_id, "mail", "Send it?", payload={})
    [item] = owner.request("GET", "/api/notices")[1]["notices"]
    assert item["kind"] == "approval" and item["action"] == {"approval": request_id}


def test_inbox_and_jobs_passed_in_get_the_store_and_the_admin_as_owner(env, monkeypatch, tmp_path):
    from igris.multiagent_jobs import JobManager

    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    jobs = JobManager(tmp_path / "ma", popen=lambda *a, **k: None, python="py")
    watcher = SimpleNamespace(notices=None, notice_owner=None)
    server = web.create_server(env.owner, port=0, accounts=env.accounts, jobs=jobs, inbox=watcher)
    admin = next(u["id"] for u in env.accounts.users() if u["role"] == "admin")
    assert jobs.notices is server.notices and watcher.notices is server.notices
    assert jobs.notice_owner() == admin and watcher.notice_owner() == admin


def test_agenda_is_per_user_and_csrf_checked(env):
    admin, user = Browser(env.server), Browser(env.server)
    assert user.request("GET", "/api/agenda")[0] == 401
    admin.login("admin", "admin-test-password")
    user.login("guest", "guest-test-password")
    entry = {"branch": "esn", "kind": "meeting", "title": "ESN toplantı",
             "start": "2099-01-02T18:00:00+03:00"}
    assert user.request("POST", "/api/agenda/add", entry, {"X-CSRF-Token": "wrong"})[0] == 403
    status, item, _ = user.request("POST", "/api/agenda/add", entry)
    assert status == 201 and item["source"] == "manual" and item["source_ref"] == item["id"]
    assert item["status"] == "added" and item["all_day"] is False
    status, payload, _ = user.request("GET", "/api/agenda")
    assert status == 200 and set(payload["branches"]) == {"kisisel", "okul", "esn"}
    assert [x["title"] for x in payload["branches"]["esn"]["later"]] == ["ESN toplantı"]
    assert set(payload["branches"]["okul"]) == {"overdue", "today", "week", "later"}
    assert list(user.request("GET", "/api/agenda?branch=esn")[1]["branches"]) == ["esn"]
    assert user.request("GET", "/api/agenda?branch=nope")[0] == 400
    # the owner (admin) neither sees nor changes another user's agenda
    assert all(not v for s in admin.request("GET", "/api/agenda")[1]["branches"].values()
               for v in s.values())
    assert admin.request("POST", "/api/agenda/status", {"id": item["id"], "status": "done"})[0] == 404
    assert admin.request("POST", "/api/agenda/delete", {"id": item["id"]})[0] == 404
    assert user.request("POST", "/api/agenda/status", {"id": item["id"], "status": "gone"})[0] == 400
    status, done, _ = user.request("POST", "/api/agenda/status", {"id": item["id"], "status": "done"})
    assert status == 200 and done["status"] == "done"
    assert not user.request("GET", "/api/agenda?branch=esn")[1]["branches"]["esn"]["later"]
    assert user.request("POST", "/api/agenda/delete", {"id": item["id"]}, {"X-CSRF-Token": ""})[0] == 403
    assert user.request("POST", "/api/agenda/delete", {"id": item["id"]})[0] == 200
    assert user.request("POST", "/api/agenda/delete", {"id": item["id"]})[0] == 404


def test_agenda_add_validates_and_cannot_forge_a_source(env):
    user = Browser(env.server)
    user.login("guest", "guest-test-password")
    bad = [{"branch": "okul", "kind": "exam", "title": "x"},  # no date
           {"branch": "is", "kind": "exam", "title": "x", "due": "2099-01-01"},
           {"branch": "okul", "kind": "party", "title": "x", "due": "2099-01-01"},
           {"branch": "okul", "kind": "exam", "title": "", "due": "2099-01-01"},
           {"branch": "okul", "kind": "exam", "title": "x", "due": "2099-01-01T10:00:00"}]
    for entry in bad:
        assert user.request("POST", "/api/agenda/add", entry)[0] == 400, entry
    status, item, _ = user.request("POST", "/api/agenda/add", {
        "branch": "okul", "kind": "exam", "title": "Vize", "due": "2099-01-01",
        "source": "moodle", "source_ref": "uid", "status": "done"})
    assert status == 201 and item["source"] == "manual" and item["status"] == "added"
    assert item["source_ref"] == item["id"] and item["all_day"] is True


def test_synced_agenda_items_cannot_be_deleted_over_the_api(env, tmp_path):
    from igris.agenda import AgendaStore

    store = AgendaStore(tmp_path / "agenda.sqlite3")
    server = web.create_server(env.owner, port=0, accounts=env.accounts, agenda=store)
    user = Browser(server)
    identity = user.login("guest", "guest-test-password")
    synced, _ = store.upsert(identity["user"]["id"], {
        "branch": "okul", "kind": "assignment", "title": "Ödev", "due": "2099-01-01",
        "source": "moodle", "source_ref": "uid-1"})
    assert user.request("POST", "/api/agenda/delete", {"id": synced["id"]})[0] == 400
    assert user.request("POST", "/api/agenda/status", {"id": synced["id"], "status": "dismissed"})[0] == 200


def test_agenda_is_off_without_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    settings = Settings(backends=("echo",), slow_backends=("echo",), user_backends=("echo",),
                        data_dir=tmp_path / "local")
    server = web.create_server(build_assistant(settings), port=0)
    assert Browser(server).request("GET", "/api/agenda")[0] == 404


def test_calendar_source_status_reaches_only_the_owner(env):
    from igris.agenda_sources import AgendaSync, moodle_source

    owner_id = web._first_admin(env.accounts)()
    env.server.agenda_sync = AgendaSync(env.server.agenda, lambda: owner_id,
                                        {"moodle": moodle_source(lambda: None)})
    env.server.agenda_sync.run()
    admin, user = Browser(env.server), Browser(env.server)
    assert admin.login("admin", "admin-test-password")["user"]["id"] == owner_id
    user.login("guest", "guest-test-password")
    sources = admin.request("GET", "/api/agenda")[1]["sources"]
    assert sources["moodle"]["state"] == "not_configured" and sources["moodle"]["reason"] == "no_link"
    assert "sources" not in user.request("GET", "/api/agenda")[1]


def test_build_agenda_sync_reads_secrets_and_esn_ids_on_every_pass(env):
    asked = []

    def get_secret(name):
        asked.append(name)
        return None

    settings = Settings(esn_calendar_ids=("esn@group.calendar.google.com",))
    sync = web.build_agenda_sync(settings, get_secret, env.server.agenda, lambda: "o")
    status = sync.run()
    assert status["moodle"]["reason"] == "no_link" and status["esn"]["reason"] == "no_google"
    assert "MOODLE_ICAL_URL" in asked and "GMAIL_REFRESH_TOKEN" in asked
    none = web.build_agenda_sync(Settings(), get_secret, env.server.agenda, lambda: "o").run()
    assert none["esn"]["reason"] == "no_calendar_id"


def test_sync_thread_runs_until_stopped(env):
    class Once:
        runs = 0

        def run(self):
            self.runs += 1
            return {"moodle": {"state": "error", "reason": "error", "error": "boom"}}

    stop, sync = threading.Event(), Once()
    stop.set()
    web._sync_agenda(sync, stop, interval=60)
    assert sync.runs == 1


def test_owner_agenda_changes_wake_the_google_calendar_sync(env, monkeypatch):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    assert env.server.calendar_sync is None  # no Google client, no sync
    server = web.create_server(env.owner, port=0, accounts=env.accounts,
                               calendar_client=lambda: None)
    sync = server.calendar_sync
    admin, user = Browser(server), Browser(server)
    admin.login("admin", "admin-test-password")
    user.login("guest", "guest-test-password")
    entry = {"branch": "okul", "kind": "exam", "title": "Vize", "due": "2099-01-01"}

    assert user.request("POST", "/api/agenda/add", entry)[0] == 201
    assert not sync._wake.is_set()  # another user's agenda never reaches the owner's Google
    assert "sync" not in user.request("GET", "/api/agenda")[1]

    status, item, _ = admin.request("POST", "/api/agenda/add", entry)
    assert status == 201 and sync._wake.is_set()
    sync._wake.clear()
    admin.request("POST", "/api/agenda/status", {"id": item["id"], "status": "done"})
    assert sync._wake.is_set()

    sync.sync()
    payload = admin.request("GET", "/api/agenda")[1]
    assert payload["sync"]["state"] == "not_configured"


class TelegramChannel:
    """A Telegram notifier stand-in with the one poll; nothing reaches the network."""

    def __init__(self):
        self.sent, self.updates = [], []

    def send(self, text):
        self.sent.append(text)

    def ask(self, text, request_id):
        self.sent.append(text)

    def send_chat_action(self, action="typing"):
        pass

    def poll_updates(self, offset=None):
        answers, messages = self.updates.pop(0) if self.updates else ([], [])
        return answers, messages, 1


def test_telegram_text_adds_to_the_owners_agenda_and_wakes_the_google_sync(env, monkeypatch):
    """The done-when path with fakes: a phone message becomes an added agenda item in the
    telegram session, the owner's Google Calendar sync is woken, the browser keeps its
    session."""
    from igris.notify import TextMessage

    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    channel = TelegramChannel()
    server = web.create_server(env.owner, port=0, accounts=env.accounts, notifier=channel,
                               calendar_client=lambda: None)
    chat = server.telegram
    assert chat is not None and server.approvals.on_message == chat.handle
    admin = Browser(server)
    admin.login("admin", "admin-test-password")
    assert admin.request("POST", "/api/session", {"name": "web"})[0] == 200

    replies = iter(['TOOL: agenda_add {"items": [{"title": "Spor", "branch": "kisisel", '
                    '"start": "2099-01-02T18:00", "due": "2099-01-02T19:00"}]}',
                    "Ekledim: 2 Ocak 18:00 Spor."])
    systems = []

    class Router:
        def complete(self, system, messages):
            systems.append(system)
            return next(replies), "fake"

    monkeypatch.setattr(env.owner, "router", Router())
    monkeypatch.setattr(env.owner, "jobs", None)
    channel.updates = [([], [TextMessage(text="2 Ocak 2099 18:00 spor ekle")])]
    server.approvals.pump()
    chat.join()

    assert channel.sent == ["Ekledim: 2 Ocak 18:00 Spor."]
    # no inbox here: the mail_recent tool is absent (mail_draft's description may name it)
    assert "Telegram" in systems[0] and "- mail_recent:" not in systems[0]
    owner_id = server.calendar_sync.owner()
    [item] = server.agenda.list(owner_id)
    assert (item["title"], item["status"], item["source"]) == ("Spor", "added", "manual")
    assert server.calendar_sync._wake.is_set()  # Google Calendar follows like web chat
    assert env.owner.session_name() == "web"
    names = {s["name"] for s in admin.request("GET", "/api/state")[1]["sessions"]}
    assert "telegram" in names


def test_answer_pump_runs_the_morning_tick_after_each_poll():
    stop = threading.Event()
    ticks = []

    class Flow:
        def pump(self):
            raise RuntimeError("channel is down")

    def tick():
        ticks.append(1)
        if len(ticks) == 2:
            stop.set()
        raise OSError("disk")

    web._pump_answers(Flow(), stop, interval=0, tick=tick)
    assert len(ticks) == 2


def test_app_install_files_are_public_with_their_types(env):
    browser = Browser(env.server)
    status, body, response = browser.request("GET", "/sw.js")
    assert status == 200 and b"serviceWorker" not in body and b'"navigate"' in body
    assert response.getheader("Content-Type").startswith("text/javascript")
    status, manifest, response = browser.request("GET", "/static/manifest.webmanifest")
    assert status == 200 and response.getheader("Content-Type").startswith("application/manifest+json")
    manifest = json.loads(manifest)
    assert manifest["start_url"] == "/" and manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        status, data, response = browser.request("GET", icon["src"])
        assert status == 200 and data[:4] == b"\x89PNG"
        assert response.getheader("Content-Type") == "image/png"
    assert b"/static/manifest.webmanifest" in browser.request("GET", "/")[1]  # the login page
    assert browser.request("GET", "/static/../accounts.sqlite3")[0] == 404


def test_inbox_gets_the_agenda_for_mail_tasks(env, monkeypatch):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    watcher = SimpleNamespace(notices=None, notice_owner=None, agenda=None)
    web.create_server(env.owner, port=0, accounts=env.accounts, inbox=watcher)
    from igris.agenda import AgendaStore

    assert isinstance(watcher.agenda, AgendaStore)


def test_build_inbox_reads_mail_settings_and_uses_only_claude(tmp_path):
    settings = Settings(data_dir=tmp_path, backends=("claude", "codex"), mail_digest=True,
                        school_domains=("uni.example",), owner_context="The owner is a nurse.")
    watcher = web.build_inbox(settings, {}.get, None)
    assert watcher.digest is True and watcher.school_domains == ("uni.example",)
    assert watcher.owner_context == "The owner is a nurse."
    assert callable(watcher.model)
    assert web.mail_model(Settings(data_dir=tmp_path, backends=("codex", "echo"))) is None


def test_mail_model_sends_system_and_mail_as_one_user_turn(tmp_path):
    seen = []

    class Backend:
        def complete(self, system, messages):
            seen.append((system, [(m.role, m.content) for m in messages]))
            return "{}"

    read = web.mail_model(Settings(data_dir=tmp_path, backends=("claude",)),
                          backend_factory=Backend)
    assert read("SYS", "mail text") == "{}"
    assert seen == [("SYS", [("user", "mail text")])]


def test_month_view_gives_six_weeks_of_own_items_and_google_only_to_the_owner(env, monkeypatch):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    windows = []

    def month_loader(time_min, time_max):
        windows.append((time_min, time_max))
        return {"status": "ok", "events": [{"id": "g1", "title": "Doktor", "start": "2026-11-20",
                                            "end": "2026-11-21", "all_day": True, "location": ""}]}

    server = web.create_server(env.owner, port=0, accounts=env.accounts, month_loader=month_loader)
    admin, user = Browser(server), Browser(server)
    admin.login("admin", "admin-test-password")
    user.login("guest", "guest-test-password")
    for who, title in ((admin, "Vize"), (user, "Kendi işim")):
        assert who.request("POST", "/api/agenda/add", {"branch": "okul", "kind": "exam",
                                                        "title": title, "due": "2026-11-12"})[0] == 201
    assert admin.request("POST", "/api/agenda/add", {"branch": "okul", "kind": "exam",
                                                     "title": "Sonra", "due": "2027-01-20"})[0] == 201

    status, month, _ = admin.request("GET", "/api/agenda/month?month=2026-11")
    assert status == 200 and month["from"] == "2026-10-26" and month["to"] == "2026-12-07"
    assert [i["title"] for i in month["items"]] == ["Vize"]
    assert month["google"]["events"][0]["title"] == "Doktor"
    assert windows and windows[0][1] - windows[0][0] == 42 * 86400

    # an item from the window's last day into the next one is in the window; one that starts
    # after the window is not
    for title, start, due in (("Vardiya", "2026-12-06T22:00:00+02:00", "2026-12-07T08:00:00+02:00"),
                              ("Ertesi", "2026-12-07T09:00:00+02:00", "2026-12-07T10:00:00+02:00")):
        assert admin.request("POST", "/api/agenda/add", {"branch": "kisisel", "kind": "other", "title": title,
                                                         "start": start, "due": due})[0] == 201
    month = admin.request("GET", "/api/agenda/month?month=2026-11")[1]
    assert [i["title"] for i in month["items"]] == ["Vize", "Vardiya"]

    status, theirs, _ = user.request("GET", "/api/agenda/month?month=2026-11")
    assert [i["title"] for i in theirs["items"]] == ["Kendi işim"] and "google" not in theirs
    for bad in ("2026-13", "2026-1", "x", ""):
        assert admin.request("GET", "/api/agenda/month?month=" + bad)[0] == 400


def test_month_loader_caches_each_window_and_maps_google_errors():
    from igris import gcal

    class Client:
        calls = 0

        def between(self, time_min, time_max):
            Client.calls += 1
            if time_min < 0:
                raise gcal.CalendarScopeError("no scope")
            return [{"id": "e"}]

    now = [0.0]
    load = web._calendar_month_loader(client_factory=Client, clock=lambda: now[0])
    assert load(100, 200) == {"status": "ok", "events": [{"id": "e"}]}
    load(100, 200)
    assert Client.calls == 1
    now[0] += web.CALENDAR_REFRESH
    load(100, 200)
    assert Client.calls == 2
    assert load(-5, 200)["status"] == "needs_consent"
    assert web._calendar_month_loader(client_factory=lambda: None)(1, 2)["status"] == "not_configured"


def test_chat_tools_reach_the_agenda_through_the_server(env, monkeypatch):
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    from igris.user_context import UserContexts

    contexts = UserContexts(env.owner)
    web.create_server(env.owner, port=0, accounts=env.accounts, contexts=contexts)
    assert contexts.agenda is not None and callable(contexts.busy)
    assert contexts.busy(0, 1)["status"] == "disabled"
