import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from igris import __version__
from igris.llm import BackendError
from web_serving import start, stop
from igris.web import (BIND_ATTEMPTS, BIND_RETRY_DELAY, WSAEADDRNOTAVAIL, create_server,
                       serve, wait_for_address)


class FakeMemory:
    def __init__(self, n=0):
        self.n = n
        self.limits = []

    def __len__(self):
        return self.n

    def history(self, limit):
        assert limit >= 1
        self.limits.append(limit)
        return [{"role": "user", "content": f"m{i}", "ts": "t"}
                for i in range(limit)]


class FakeAssistant:
    def __init__(self):
        self.settings = SimpleNamespace(name="Igris", backends=("fake", "other"))
        self.memory = FakeMemory(3)
        self.tools = SimpleNamespace(names=lambda: ["clock", "calc"])
        self.session = "default"
        self.persona = "plain"
        self.error = None
        self.asked = []

    def ask(self, text, slow=False):
        self.asked.append(text)
        self.slows = getattr(self, "slows", []) + [slow]
        if self.error:
            raise self.error
        return SimpleNamespace(text=f"echo {text}", backends=("fake",),
                               tool_calls=("clock",))

    def use_session(self, name):
        if name == "bad":
            raise ValueError("invalid session name")
        self.session = name

    def session_name(self):
        return self.session

    def list_sessions(self):
        return [{"name": self.session, "messages": 3, "updated": "x"}]

    def set_persona(self, name):
        if name not in ("plain", "pirate"):
            raise ValueError("unknown persona")
        self.persona = name

    def persona_name(self):
        return self.persona

    def list_personas(self):
        return ["plain", "pirate"]


ROADMAP = {"updated": "2026-01-01", "releases": [], "plans": []}


class Client:
    def __init__(self, server):
        self.port = server.server_address[1]

    def request(self, method, path, body=None, headers=None, raw=None):
        h = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None or raw is not None:
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        h = {k: v for k, v in h.items() if v is not None}
        data = raw if raw is not None else (
            json.dumps(body).encode("utf-8") if body is not None else None)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=data, headers=h)
            resp = conn.getresponse()
            payload = resp.read()
            return resp.status, resp, payload
        finally:
            conn.close()

    def json(self, method, path, body=None, **kw):
        status, resp, payload = self.request(method, path, body, **kw)
        return status, json.loads(payload.decode("utf-8"))


@pytest.fixture
def env(tmp_path):
    assistant = FakeAssistant()
    (tmp_path / "v2.html").write_text("<html>v2</html>", encoding="utf-8")
    server = create_server(assistant, port=0, roadmap_loader=lambda: ROADMAP,
                           static_dir=tmp_path)
    thread = start(server)
    yield assistant, Client(server), server, tmp_path
    stop(server, thread)


def test_default_port_is_8770():
    import inspect

    from igris.web import serve

    assert inspect.signature(create_server).parameters["port"].default == 8770
    assert inspect.signature(serve).parameters["port"].default == 8770


def test_index(env):
    _, c, _, _ = env
    status, resp, body = c.request("GET", "/")
    assert status == 200
    assert resp.getheader("Content-Type") == "text/html; charset=utf-8"
    assert body == b"<html>v2</html>"


def test_index_missing(env):
    _, c, _, tmp = env
    (tmp / "v2.html").unlink()
    status, body = c.json("GET", "/")
    assert status == 404
    assert "error" in body


def test_state(env):
    _, c, _, _ = env
    status, body = c.json("GET", "/api/state")
    assert status == 200
    assert body == {
        "name": "Igris", "version": __version__, "session": "default",
        "persona": "plain", "backends": ["fake", "other"],
        "sessions": [{"name": "default", "messages": 3, "updated": "x"}],
        "personas": ["plain", "pirate"], "tools": ["clock", "calc"]}


def test_response_headers(env):
    _, c, _, _ = env
    _, resp, _ = c.request("GET", "/api/state")
    assert resp.getheader("Content-Type") == "application/json; charset=utf-8"
    assert resp.getheader("Cache-Control") == "no-store"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"


def test_history(env):
    a, c, _, _ = env
    status, body = c.json("GET", "/api/history")
    assert status == 200
    assert len(body["messages"]) == 3
    assert a.memory.limits == [3]


def test_history_capped(env):
    a, c, _, _ = env
    a.memory.n = 500
    _, body = c.json("GET", "/api/history")
    assert len(body["messages"]) == 200


def test_history_empty_skips_call(env):
    a, c, _, _ = env
    a.memory.n = 0
    status, body = c.json("GET", "/api/history")
    assert status == 200
    assert body == {"messages": []}
    assert a.memory.limits == []


def test_roadmap(env):
    _, c, _, _ = env
    assert c.json("GET", "/api/roadmap") == (200, ROADMAP)


def test_roadmap_loader_failure(env):
    _, _, server, _ = env
    # a separate server whose loader fails
    bad = create_server(FakeAssistant(), port=0,
                        roadmap_loader=lambda: 1 / 0)
    t = start(bad)
    try:
        status, body = Client(bad).json("GET", "/api/roadmap")
        assert status == 500 and "error" in body
    finally:
        stop(bad, t)


def test_unknown_paths_404(env):
    _, c, _, _ = env
    assert c.json("GET", "/nope")[0] == 404
    assert c.json("POST", "/api/nope", {})[0] == 404


def test_query_string_ignored(env):
    _, c, _, _ = env
    assert c.json("GET", "/api/state?x=1")[0] == 200


def test_ask(env):
    a, c, _, _ = env
    status, body = c.json("POST", "/api/ask", {"text": "héllo"})
    assert status == 200
    assert body == {"text": "echo héllo", "backends": ["fake"],
                    "tool_calls": ["clock"], "job_id": None}
    assert a.asked == ["héllo"]


@pytest.mark.parametrize("payload", [{}, {"text": ""}, {"text": "   "},
                                     {"text": 5}, {"text": "x" * 8001}])
def test_ask_bad_text(env, payload):
    a, c, _, _ = env
    status, body = c.json("POST", "/api/ask", payload)
    assert status == 400 and "error" in body
    assert a.asked == []


def test_ask_max_length_ok(env):
    _, c, _, _ = env
    assert c.json("POST", "/api/ask", {"text": "x" * 8000})[0] == 200


def test_ask_backend_error(env):
    a, c, _, _ = env
    a.error = BackendError("all failed")
    status, body = c.json("POST", "/api/ask", {"text": "hi"})
    assert status == 502
    assert "all failed" in body["error"]


def test_ask_other_error(env):
    a, c, _, _ = env
    a.error = RuntimeError("boom")
    status, body = c.json("POST", "/api/ask", {"text": "hi"})
    assert status == 500
    assert body == {"error": "boom"}


def test_ask_slow_flag_passed(env):
    a, c, _, _ = env
    assert c.json("POST", "/api/ask", {"text": "hi", "slow": True})[0] == 200
    assert c.json("POST", "/api/ask", {"text": "hi"})[0] == 200
    assert a.slows == [True, False]


@pytest.mark.parametrize("bad", ["yes", 1, None, [], {}])
def test_ask_slow_must_be_boolean(env, bad):
    a, c, _, _ = env
    status, body = c.json("POST", "/api/ask", {"text": "hi", "slow": bad})
    assert status == 400 and "error" in body
    assert a.asked == []


def test_session_switch(env):
    a, c, _, _ = env
    status, body = c.json("POST", "/api/session", {"name": "work"})
    assert status == 200
    assert body["session"] == "work"
    assert a.session == "work"


def test_session_invalid(env):
    _, c, _, _ = env
    status, body = c.json("POST", "/api/session", {"name": "bad"})
    assert status == 400
    assert "invalid session name" in body["error"]
    assert c.json("POST", "/api/session", {})[0] == 400
    assert c.json("POST", "/api/session", {"name": 3})[0] == 400


def test_persona_switch(env):
    a, c, _, _ = env
    status, body = c.json("POST", "/api/persona", {"name": "pirate"})
    assert status == 200
    assert body["persona"] == "pirate"
    assert a.persona == "pirate"


def test_persona_invalid(env):
    _, c, _, _ = env
    status, body = c.json("POST", "/api/persona", {"name": "zzz"})
    assert status == 400
    assert "unknown persona" in body["error"]
    assert c.json("POST", "/api/persona", {"name": ""})[0] == 400


def test_invalid_json(env):
    _, c, _, _ = env
    assert c.json("POST", "/api/ask", raw=b"{not json")[0] == 400
    assert c.json("POST", "/api/ask", raw=b"\xff\xfe")[0] == 400


@pytest.mark.parametrize("raw", [b"[1]", b'"s"', b"3", b"null"])
def test_non_object_body(env, raw):
    _, c, _, _ = env
    assert c.json("POST", "/api/ask", raw=raw)[0] == 400


def test_body_too_large(env):
    _, c, _, _ = env
    status, body = c.json("POST", "/api/ask", raw=b'{"text": "a"}',
                          headers={"Content-Length": str(64 * 1024 + 1)})
    assert status == 413 and "error" in body


def test_body_at_limit_not_413(env):
    _, c, _, _ = env
    pad = " " * (64 * 1024 - len(b'{"text":"a"}'))
    raw = b'{"text":"a"}' + pad.encode()
    assert len(raw) == 64 * 1024
    assert c.json("POST", "/api/ask", raw=raw)[0] == 200


def test_content_type_required(env):
    a, c, _, _ = env
    status, _ = c.json("POST", "/api/ask", {"text": "hi"},
                       headers={"Content-Type": "text/plain"})
    assert status == 400
    assert a.asked == []


def test_content_type_with_charset(env):
    _, c, _, _ = env
    status, _ = c.json("POST", "/api/ask", {"text": "hi"},
                       headers={"Content-Type": "application/json; charset=utf-8"})
    assert status == 200


def test_missing_content_type(env):
    _, c, _, _ = env
    status, _ = c.json("POST", "/api/ask", {"text": "hi"},
                       headers={"Content-Type": None})
    assert status == 400


@pytest.mark.parametrize("host", ["evil.example.com", "evil.example.com:80",
                                  "127.0.0.1", "localhost"])
def test_bad_host_rejected(env, host):
    _, c, _, _ = env
    assert c.json("GET", "/api/state", headers={"Host": host})[0] == 403
    assert c.json("GET", "/", headers={"Host": host})[0] == 403
    assert c.json("POST", "/api/ask", {"text": "x"},
                  headers={"Host": host})[0] == 403


def test_localhost_host_allowed(env):
    _, c, _, _ = env
    status, _ = c.json("GET", "/api/state",
                       headers={"Host": f"localhost:{c.port}"})
    assert status == 200


def test_origin_must_match_host(env):
    a, c, _, _ = env
    status, _ = c.json("POST", "/api/ask", {"text": "x"},
                       headers={"Origin": "http://evil.example.com"})
    assert status == 403
    assert a.asked == []
    status, _ = c.json("POST", "/api/ask", {"text": "x"},
                       headers={"Origin": f"https://127.0.0.1:{c.port}"})
    assert status == 403


def test_matching_origin_allowed(env):
    _, c, _, _ = env
    status, _ = c.json("POST", "/api/ask", {"text": "x"},
                       headers={"Origin": f"http://127.0.0.1:{c.port}"})
    assert status == 200


def test_ask_calls_are_serialized(env):
    a, c, _, _ = env
    active = {"n": 0, "max": 0}
    guard = threading.Lock()

    def slow_ask(text):
        with guard:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        threading.Event().wait(0.05)
        with guard:
            active["n"] -= 1
        return SimpleNamespace(text=text, backends=(), tool_calls=())

    a.ask = slow_ask
    threads = [threading.Thread(target=c.json,
                                args=("POST", "/api/ask", {"text": "x"}))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert active["max"] == 1


# ---- jobs, static, colors ----
_LAST = {}


class FakePaid(Exception):
    pass


class FakeJobs:
    max_parallel = 2

    def __init__(self):
        self.submitted = []
        self.test_cmds = []
        self.cancelled = []
        self.polls = 0
        self.error = None

    def submit(self, goal, *, context=None, implement=False, test_cmd=None,
               parallel=False):
        if self.error:
            raise self.error
        self.submitted.append((goal, context, implement, parallel))
        self.test_cmds.append(test_cmd)
        return SimpleNamespace(to_dict=lambda: {"id": "j1", "goal": goal})

    def jobs(self):
        return [SimpleNamespace(to_dict=lambda: {"id": "j1"})]

    def cancel(self, job_id):
        self.cancelled.append(job_id)
        return job_id == "j1"

    def poll(self):
        self.polls += 1

    worker_calls = 0
    worker_error = None

    def worker_models(self):
        self.worker_calls += 1
        if self.worker_error:
            raise self.worker_error
        return {"zincir": ["evren-glm"], "isciler": [{"ad": "evren-glm", "durum": "aktif"}]}


@pytest.fixture
def jenv(tmp_path, monkeypatch):
    jobs = FakeJobs()
    assistant = FakeAssistant()
    _LAST["assistant"] = assistant
    loaded = []

    def loader(path):
        loaded.append(str(path))
        return [{"path": "a.py", "status": "M"}]

    (tmp_path / "sample.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "x.py").write_text("x", encoding="utf-8")
    server = create_server(assistant, port=0, static_dir=tmp_path,
                           multiagent_dir=tmp_path, jobs=jobs,
                           files_loader=loader)
    thread = start(server)
    yield Client(server), jobs, loaded, server
    stop(server, thread)


def test_jobs_list(jenv):
    c, _, _, _ = jenv
    status, body = c.json("GET", "/api/multiagent/jobs")
    assert status == 200
    checks = body.pop("checks")
    assert body == {"enabled": True, "max_parallel": 2, "implement_ready": False,
                    "jobs": [{"id": "j1"}]}
    assert [c["id"] for c in checks] == ["dir", "script", "claude", "codex", "python",
                                         "workspace"]
    assert checks[0]["ok"] and not checks[1]["ok"] and checks[1]["required"]


def test_jobs_disabled(env):
    _, c, _, _ = env
    assert c.json("GET", "/api/multiagent/jobs") == (
        200, {"enabled": False, "max_parallel": 1, "jobs": [],
              "checks": [{"id": "dir", "ok": False, "required": True,
                          "detail": "IGRIS_MULTIAGENT_DIR"}]})
    assert c.json("POST", "/api/multiagent/jobs", {"goal": "g"})[0] == 404
    assert c.json("POST", "/api/multiagent/cancel", {"id": "j1"})[0] == 404


def test_jobs_submit(jenv):
    c, jobs, _, _ = jenv
    status, body = c.json("POST", "/api/multiagent/jobs",
                          {"goal": "do it", "parallel": True})
    assert status == 200 and body["id"] == "j1"
    assert jobs.submitted == [("do it", None, False, True)]


@pytest.mark.parametrize("body", [{}, {"goal": 3}, {"goal": "g", "parallel": "y"},
                                  {"goal": "g", "implement": 1}])
def test_jobs_submit_bad_types(jenv, body):
    c, jobs, _, _ = jenv
    assert c.json("POST", "/api/multiagent/jobs", body)[0] == 400
    assert jobs.submitted == []


def test_jobs_submit_errors(jenv):
    from igris.llm import PaidUsageBlocked

    c, jobs, _, _ = jenv
    jobs.error = ValueError("empty goal")
    assert c.json("POST", "/api/multiagent/jobs", {"goal": "g"})[0] == 400
    jobs.error = PaidUsageBlocked("paid")
    assert c.json("POST", "/api/multiagent/jobs", {"goal": "g"})[0] == 409


def test_jobs_cancel(jenv):
    c, jobs, _, _ = jenv
    assert c.json("POST", "/api/multiagent/cancel", {"id": "j1"}) == (
        200, {"cancelled": True})
    assert c.json("POST", "/api/multiagent/cancel", {"id": "zz"}) == (
        200, {"cancelled": False})
    assert c.json("POST", "/api/multiagent/cancel", {"id": 3})[0] == 400


def test_jobs_post_origin_checked(jenv):
    c, jobs, _, _ = jenv
    status, _ = c.json("POST", "/api/multiagent/jobs", {"goal": "g"},
                       headers={"Origin": "http://evil.example"})
    assert status == 403 and jobs.submitted == []


def test_static_assets(jenv):
    c, _, _, _ = jenv
    status, resp, payload = c.request("GET", "/static/sample.js")
    assert status == 200 and payload == b"console.log(1)"
    assert resp.getheader("Content-Type").startswith("text/javascript")
    assert c.request("GET", "/static/x.py")[0] == 404
    assert c.request("GET", "/static/../igris/web.py")[0] == 404
    assert c.request("GET", "/static/%2e%2e/web.py")[0] == 404
    assert c.request("GET", "/static/missing.css")[0] == 404


def test_poll_jobs(jenv):
    _, jobs, _, server = jenv
    server.poll_jobs()
    assert jobs.polls == 1


def test_ask_includes_job_id(env):
    _, c, _, _ = env
    _, body = c.json("POST", "/api/ask", {"text": "hi"})
    assert body["job_id"] is None


def test_jobs_endpoints_do_not_wait_for_the_chat_lock(jenv):
    c, jobs, _, server = jenv
    gate = threading.Event()
    entered = threading.Event()

    def slow_ask(text, slow=False):
        entered.set()
        gate.wait(10)
        return SimpleNamespace(text="x", backends=(), tool_calls=())

    t = threading.Thread(target=lambda: c.request(
        "POST", "/api/ask", {"text": "hi"}), daemon=True)
    # hold the chat lock by blocking a real ask
    jobs_assistant = _LAST["assistant"]
    jobs_assistant.ask = slow_ask
    t.start()
    assert entered.wait(5)
    try:
        results = {}

        def work():
            results["list"] = c.json("GET", "/api/multiagent/jobs")[0]
            results["cancel"] = c.json("POST", "/api/multiagent/cancel",
                                       {"id": "j1"})[0]
            results["submit"] = c.json("POST", "/api/multiagent/jobs",
                                       {"goal": "g"})[0]
            server.poll_jobs()
            results["poll"] = jobs.polls

        w = threading.Thread(target=work, daemon=True)
        w.start()
        w.join(5)
        assert not w.is_alive()
        assert results == {"list": 200, "cancel": 200, "submit": 200, "poll": 1}
    finally:
        gate.set()
        t.join(5)


# ---- final review fixes ----

def test_jobs_submit_implement_passes_test_command(jenv, tmp_path):
    c, jobs, _, _ = jenv
    _LAST["assistant"].settings.workspace = tmp_path
    status, _ = c.json("POST", "/api/multiagent/jobs",
                       {"goal": "g", "implement": True, "test": "  pytest -q  "})
    assert status == 200
    assert jobs.submitted[-1] == ("g", tmp_path, True, False)
    assert jobs.test_cmds[-1] == "pytest -q"


def test_jobs_submit_test_ignored_without_implement(jenv):
    c, jobs, _, _ = jenv
    assert c.json("POST", "/api/multiagent/jobs", {"goal": "g", "test": "pytest"})[0] == 200
    assert jobs.test_cmds[-1] is None


@pytest.mark.parametrize("test", [None, "", "   ", 3])
def test_jobs_submit_implement_bad_test(jenv, tmp_path, test):
    c, jobs, _, _ = jenv
    _LAST["assistant"].settings.workspace = tmp_path
    body = {"goal": "g", "implement": True}
    if test is not None:
        body["test"] = test
    assert c.json("POST", "/api/multiagent/jobs", body)[0] == 400
    assert jobs.submitted == []


def test_jobs_submit_implement_needs_workspace(jenv):
    c, jobs, _, _ = jenv
    status, body = c.json("POST", "/api/multiagent/jobs",
                          {"goal": "g", "implement": True, "test": "pytest -q"})
    assert status == 400 and "IGRIS_WORKSPACE" in body["error"]
    assert jobs.submitted == []


def test_jobs_list_reports_implement_ready(jenv, tmp_path):
    c, _, _, _ = jenv
    assert c.json("GET", "/api/multiagent/jobs")[1]["implement_ready"] is False
    _LAST["assistant"].settings.workspace = tmp_path
    assert c.json("GET", "/api/multiagent/jobs")[1]["implement_ready"] is True


def test_jobs_submit_uses_assistant_workspace(jenv, tmp_path):
    c, jobs, _, _ = jenv
    _LAST["assistant"].settings.workspace = tmp_path
    assert c.json("POST", "/api/multiagent/jobs", {"goal": "g"})[0] == 200
    assert jobs.submitted[-1] == ("g", tmp_path, False, False)


def test_jobs_submit_without_workspace_setting(jenv):
    c, jobs, _, _ = jenv
    assert c.json("POST", "/api/multiagent/jobs", {"goal": "g"})[0] == 200
    assert jobs.submitted[-1] == ("g", None, False, False)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", " 0.0.0.0 "])
def test_every_interface_is_refused(host):
    with pytest.raises(ValueError, match="all network interfaces"):
        create_server(SimpleNamespace(), host, 0, accounts=SimpleNamespace())


class ProbeSocket:
    """Stands in for socket.socket: raises the queued errors, then binds."""

    def __init__(self, *errors):
        self.errors = list(errors)
        self.bound = []

    def __call__(self, *args):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def bind(self, address):
        self.bound.append(address)
        if self.errors:
            raise self.errors.pop(0)


def not_ready():
    return OSError(WSAEADDRNOTAVAIL, "address not available")


def test_wait_for_address_retries_until_the_interface_exists():
    probe, sleeps = ProbeSocket(not_ready(), not_ready()), []

    wait_for_address("100.64.0.7", sleep=sleeps.append, make_socket=probe)

    assert probe.bound == [("100.64.0.7", 0)] * 3
    assert sleeps == [BIND_RETRY_DELAY, BIND_RETRY_DELAY]


def test_wait_for_address_gives_up_after_the_last_attempt():
    probe = ProbeSocket(*[not_ready() for _ in range(BIND_ATTEMPTS)])
    sleeps = []

    with pytest.raises(OSError):
        wait_for_address("100.64.0.7", sleep=sleeps.append, make_socket=probe)

    assert len(probe.bound) == BIND_ATTEMPTS
    assert len(sleeps) == BIND_ATTEMPTS - 1


def test_wait_for_address_does_not_retry_other_errors():
    probe, sleeps = ProbeSocket(PermissionError(13, "denied")), []

    with pytest.raises(PermissionError):
        wait_for_address("100.64.0.7", sleep=sleeps.append, make_socket=probe)

    assert sleeps == []


def test_wait_for_address_skips_loopback():
    probe = ProbeSocket()

    wait_for_address("127.0.0.1", make_socket=probe)

    assert probe.bound == []


def test_serve_waits_for_the_address_before_building_anything(monkeypatch):
    def unavailable(host):
        raise OSError(WSAEADDRNOTAVAIL, f"{host} not available")

    monkeypatch.setattr("igris.web.wait_for_address", unavailable)

    # An empty assistant would fail with AttributeError if serve went any further.
    with pytest.raises(OSError, match="100.64.0.7 not available"):
        serve(SimpleNamespace(), "100.64.0.7", 0)


def test_v2_is_the_only_page_and_old_addresses_redirect(env):
    _, c, _, _ = env
    status, resp, body = c.request("GET", "/")
    assert status == 200
    assert resp.getheader("Content-Type") == "text/html; charset=utf-8"
    assert body == b"<html>v2</html>"
    for old in ("/v2", "/index.html", "/klasik-eski"):
        status, resp, body = c.request("GET", old)
        assert status == 302 and resp.getheader("Location") == "/" and body == b"", old


def test_old_page_is_never_served(env):
    _, c, _, tmp = env
    # even with an index.html in the static folder, no address serves it
    (tmp / "index.html").write_text("<html>old</html>", encoding="utf-8")
    for path in ("/", "/klasik-eski", "/index.html"):
        assert b"old" not in c.request("GET", path)[2], path


def test_prefs_need_accounts(env):
    _, c, _, _ = env
    assert c.json("GET", "/api/prefs")[0] == 404
    assert c.json("POST", "/api/prefs", {"view": "klasik"})[0] == 404


def _serve(**kwargs):
    assistant = FakeAssistant()
    server = create_server(assistant, port=0, roadmap_loader=lambda: ROADMAP, **kwargs)
    thread = start(server)
    return server, thread


def _stop(server, thread):
    stop(server, thread)


def test_retired_forge_endpoint_is_not_available(env):
    assert env[1].json("GET", "/api/forge")[0] == 404


def test_event_stream_says_hello_then_what_changed():
    server, thread = _serve(stream_settings={"interval": 0.01, "max_seconds": 0.05})
    try:
        status, resp, body = Client(server).request("GET", "/api/events")
        assert status == 200
        assert resp.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        text = body.decode("utf-8")
        assert text.index("event: hello") < text.index("event: change")
        assert 'data: {"keys":["calendar"],"errors":[]}' in text
        assert text.count("event: change") == 1  # unchanged data is not sent again
    finally:
        _stop(server, thread)


def test_event_stream_reports_a_failing_source_without_breaking():
    def broken():
        raise RuntimeError("calendar is unavailable")

    server, thread = _serve(calendar_loader=broken,
                            stream_settings={"interval": 0.01, "max_seconds": 0.05})
    try:
        text = Client(server).request("GET", "/api/events")[2].decode("utf-8")
        assert 'data: {"keys":["calendar"],"errors":["calendar"]}' in text
        assert Client(server).json("GET", "/api/calendar")[0] == 500
    finally:
        _stop(server, thread)


def test_static_font_is_served_as_ttf(tmp_path):
    (tmp_path / "hand.ttf").write_bytes(b"\x00\x01\x00\x00font")
    server, thread = _serve(static_dir=tmp_path)
    try:
        c = Client(server)
        status, resp, payload = c.request("GET", "/static/hand.ttf")
        assert status == 200 and payload == b"\x00\x01\x00\x00font"
        assert resp.getheader("Content-Type") == "font/ttf"
        assert c.request("GET", "/static/hand.otf")[0] == 404
    finally:
        _stop(server, thread)


def test_calendar_is_disabled_unless_a_loader_is_given():
    server, thread = _serve()
    try:
        assert Client(server).json("GET", "/api/calendar") == (200, {"status": "disabled", "events": []})
    finally:
        _stop(server, thread)


def test_calendar_comes_from_the_loader():
    events = {"status": "ok", "events": [{"id": "e1", "title": "Rapor teslimi"}]}
    server, thread = _serve(calendar_loader=lambda: events)
    try:
        assert Client(server).json("GET", "/api/calendar") == (200, events)
    finally:
        _stop(server, thread)


class FakeCalendar:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    def upcoming(self, *, days, max_results):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_calendar_loader_maps_outcomes_and_caches():
    from igris import gcal
    from igris.web import CALENDAR_REFRESH, _calendar_loader

    now = [0.0]
    cal = FakeCalendar([{"id": "e1"}])
    load = _calendar_loader(client_factory=lambda: cal, clock=lambda: now[0])
    assert load() == {"status": "ok", "events": [{"id": "e1"}]}
    assert load() and cal.calls == 1
    now[0] = CALENDAR_REFRESH
    load()
    assert cal.calls == 2
    for exc, status in ((gcal.CalendarScopeError("scope"), "needs_consent"),
                        (gcal.CalendarAuthError("refused"), "auth_down"),
                        (gcal.CalendarError("HTTP 500"), "error")):
        assert _calendar_loader(client_factory=lambda: FakeCalendar(exc))()["status"] == status
    assert _calendar_loader(client_factory=lambda: None)() == {"status": "not_configured", "events": []}
    assert _calendar_loader(get_secret=lambda name: None)()["status"] == "not_configured"


def test_models_lists_multiagent_workers_and_caches_them(jenv):
    c, jobs, _, _ = jenv
    status, body = c.json("GET", "/api/models")
    assert status == 200
    assert body["multiagent"]["isciler"] == [{"ad": "evren-glm", "durum": "aktif"}]
    assert "providers" in body
    c.json("GET", "/api/models")
    assert jobs.worker_calls == 1  # the subprocess runs at most once a minute


def test_models_reports_multiagent_worker_error(jenv):
    c, jobs, _, _ = jenv
    jobs.worker_error = RuntimeError("saglayici_durum.py is missing (git pull)")
    status, body = c.json("GET", "/api/models")
    assert status == 200 and "git pull" in body["multiagent"]["error"]


def test_due_followup_notifies_once(tmp_path):
    from igris.web import remind_followups
    from igris.followups import FollowupStore
    from igris.people import PeopleStore
    from igris.outbox import OutboxStore
    now = [1000.0]
    followups = FollowupStore(tmp_path / 'followups.db', clock=lambda: now[0])
    people = PeopleStore(str(tmp_path / 'people.db'))
    outbox = OutboxStore(str(tmp_path / 'outbox.db'), clock=lambda: now[0], day_start=lambda _: 0)
    people.add('owner', 'Ece', emails=['ece@example.lt'])
    draft = outbox.create('owner', identity='personal', to=['ece@example.lt'], subject='Meeting', body='Secret body')
    outbox.mark_awaiting('owner', draft.id, 'approval')
    outbox.begin_send('owner', draft.id, 'approval')
    outbox.mark_sent('owner', draft.id, thread_id='thread')
    followups.add('owner', 'thread', 3 * 86400, note='Meeting')
    notices = []
    notifier = SimpleNamespace(send=notices.append)
    remind_followups(followups, outbox, people, notifier, days=3)
    assert not notices
    now[0] += 3 * 86400
    remind_followups(followups, outbox, people, notifier, days=3)
    assert len(notices) == 1
    assert 'Ece 3 gündür cevap vermedi: Meeting' in notices[0]
    assert 'taslağı' in notices[0] and 'Secret body' not in notices[0]
    remind_followups(followups, outbox, people, notifier, days=3)
    assert len(notices) == 1


def test_mail_wiring_routes_answers_and_preserves_other_handlers(tmp_path, monkeypatch):
    from igris import web
    from igris.assistant import build_assistant
    from igris.config import Settings
    from igris.user_context import UserContexts
    from igris.approval_flow import ApprovalFlow
    from igris.approvals import ApprovalStore
    from igris.inbox import InboxStore
    monkeypatch.setattr(web, 'BoundedHTTPServer', lambda *args: SimpleNamespace())
    settings = Settings(data_dir=tmp_path, backends=('echo',), slow_backends=('echo',),
                        sessions_dir=tmp_path / 'sessions', personas_dir=tmp_path / 'personas',
                        memory_path=tmp_path / 'memory.jsonl', mail_max_recipients=2,
                        mail_daily_limit=7, mail_followup_days=4, school_address='me@school.example')
    assistant = build_assistant(settings)
    contexts = UserContexts(assistant)
    handled, sent, questions, calls = [], [], [], []
    notifier = SimpleNamespace(send=sent.append, ask=lambda text, rid: questions.append(rid))
    flow = ApprovalFlow(ApprovalStore(str(tmp_path / 'approvals.db')), notifier,
                        lambda rid, approved: handled.append((rid, approved)))
    client = SimpleNamespace(send=lambda raw, **kw: calls.append(raw) or {'id': 'sent', 'threadId': 'thread'})
    inbox = SimpleNamespace(store=InboxStore(tmp_path / 'inbox.db'), client_factory=lambda: client)
    accounts = SimpleNamespace(initialized=lambda: True, users=lambda: [{'id': 'owner', 'role': 'admin'}])
    server = web.create_server(assistant, accounts=accounts, contexts=contexts, approvals=flow,
                               notifier=notifier, inbox=inbox,
                               tasks=SimpleNamespace(close=lambda: None), agenda=SimpleNamespace(),
                               presence=SimpleNamespace(), notices=SimpleNamespace())
    assert contexts.outbox.max_recipients == 2 and contexts.outbox.daily_limit == 7
    assert contexts.mail_sender.followup_seconds == 4 * 86400
    assert contexts.mail_sender.school_address == 'me@school.example'
    assert inbox.followups is server.followups and inbox.people is contexts.people
    for filename in ['people.sqlite3', 'outbox.sqlite3', 'followups.sqlite3']:
        assert (tmp_path / filename).exists()
    draft = contexts.outbox.create('owner', identity='personal', to=['a@example.com'], subject='Hi', body='Body')
    rid = contexts.mail_sender.ask('owner', draft.id)
    assert not calls
    flow.answer('owner', rid, True)
    assert len(calls) == 1 and handled == []
    other = flow.request('owner', 'other.action', 'Other', payload={})
    flow.answer('owner', other, False)
    assert handled == [(other, False)]
    assert contexts.outbox.get('owner', draft.id).status == 'sent'
    assert inbox.owner_addresses == ('me@school.example',)
    server.tick()


def test_mail_answer_failure_is_finished_by_the_next_tick(tmp_path, monkeypatch, capsys):
    from igris import web
    from igris.assistant import build_assistant
    from igris.config import Settings
    from igris.user_context import UserContexts
    from igris.approval_flow import ApprovalFlow
    from igris.approvals import ApprovalStore
    from igris.inbox import InboxStore
    monkeypatch.setattr(web, 'BoundedHTTPServer', lambda *args: SimpleNamespace())
    settings = Settings(data_dir=tmp_path, backends=('echo',), slow_backends=('echo',),
                        sessions_dir=tmp_path / 'sessions', personas_dir=tmp_path / 'personas',
                        memory_path=tmp_path / 'memory.jsonl')
    assistant = build_assistant(settings)
    contexts = UserContexts(assistant)
    notifier = SimpleNamespace(send=lambda text: None, ask=lambda text, rid: None)
    flow = ApprovalFlow(ApprovalStore(str(tmp_path / 'approvals.db')), notifier)
    calls = []
    client = SimpleNamespace(send=lambda raw, **kw: calls.append(raw) or {'id': 'm', 'threadId': 't'})
    inbox = SimpleNamespace(store=InboxStore(tmp_path / 'inbox.db'), client_factory=lambda: client)
    accounts = SimpleNamespace(initialized=lambda: True, users=lambda: [{'id': 'owner', 'role': 'admin'}])
    server = web.create_server(assistant, accounts=accounts, contexts=contexts, approvals=flow,
                               notifier=notifier, inbox=inbox,
                               tasks=SimpleNamespace(close=lambda: None), agenda=SimpleNamespace(),
                               presence=SimpleNamespace(), notices=SimpleNamespace())
    draft = contexts.outbox.create('owner', identity='personal', to=['a@example.com'],
                                   subject='Hi', body='SECRET_BODY')
    rid = contexts.mail_sender.ask('owner', draft.id)
    real_begin = contexts.outbox.begin_send
    monkeypatch.setattr(contexts.outbox, 'begin_send',
                        lambda *a: (_ for _ in ()).throw(RuntimeError('SECRET_BODY')))
    flow.answer('owner', rid, True)  # the failure must not escape the answer handler
    assert not calls and 'SECRET_BODY' not in capsys.readouterr().err
    monkeypatch.setattr(contexts.outbox, 'begin_send', real_begin)
    server.tick()
    server.tick()
    assert len(calls) == 1
    assert contexts.outbox.get('owner', draft.id).status == 'sent'


@pytest.mark.parametrize("path", ["runs", "live", "run/20260101_101010", "run/bad"])
def test_retired_viewer_routes_are_missing(env, path):
    assert env[1].json("GET", "/api/multiagent/" + path)[0] == 404


def test_state_omits_legacy_colors(env):
    assert "agent_colors" not in env[1].json("GET", "/api/state")[1]
