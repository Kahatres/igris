"""Core/study HTTP contracts with in-memory HTTP and injected model/job fakes."""

from dataclasses import replace
import base64
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from igris import web
from igris.accounts import AccountStore
from igris.assistant import build_assistant
from igris.config import Settings
from igris.user_context import UserContexts
from test_web_accounts import Browser, Server


PHOTO = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("igris.accounts.PASSWORD_ROUNDS", 1000)
    monkeypatch.setattr(web, "BoundedHTTPServer", Server)
    settings = Settings(backends=("echo",), slow_backends=("echo",), user_backends=("echo",),
                        data_dir=tmp_path, memory_path=tmp_path / "memory.jsonl",
                        sessions_dir=tmp_path / "sessions", personas_dir=tmp_path / "personas")
    assistant = build_assistant(settings)
    contexts = UserContexts(assistant)
    jobs, calls, replies = [], [], []

    def model(system, prompt):
        calls.append((system, prompt))
        if replies:
            return replies.pop(0)
        return 'Topics\nVectors' if "document card" in system else json.dumps({
            "first_wrong_line": None, "reason": "Doğru", "should_be": "", "final_answer": "32"})

    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    accounts.bootstrap("admin-test-password", "guest-test-password")
    server = web.create_server(assistant, port=0, accounts=accounts, contexts=contexts,
                               study_model=model, start_ingest=jobs.append)
    admin, user, anonymous = Browser(server), Browser(server), Browser(server)
    owner = admin.login("admin", "admin-test-password")["user"]["id"]
    other = user.login("Guest", "guest-test-password")["user"]["id"]
    exam = contexts.study.create_exam(owner, "Vectors", "2026-12-01", [])
    contexts.study.set_steps(owner, exam, [{"topic": "dot", "goal": "Practice",
        "start": "2026-11-20T12:00:00+02:00", "end": "2026-11-20T13:00:00+02:00"}])
    step = contexts.study.steps(owner, exam)[0]["id"]
    problem = contexts.study.add_problem(owner, exam, "dot", "Calculate a dot product",
        answer={"op": "dot", "args": ["(1,2,3)", "(4,5,6)"]})
    return SimpleNamespace(**locals())


ROUTES = [
    ("GET", "/calis", None), ("GET", "/api/core", None),
    ("POST", "/api/core/add", {"file_id": "x"}), ("GET", "/api/core/doc?id=x", None),
    ("POST", "/api/core/page", {"id": "x", "n": 1, "text": "x"}),
    ("DELETE", "/api/core/doc?id=x", None), ("GET", "/api/study", None),
    ("GET", "/api/study/problem?step=x", None),
    ("GET", "/api/study/history", None),
    ("GET", "/api/study/progress", None),
    ("POST", "/api/study/attempt", {"problem_id": "x", "lines": ["32"]}),
    ("POST", "/api/study/explain", {"attempt_id": "x"}),
    ("POST", "/api/study/chat", {"text": "Explain", "selected": None, "step": None}),
]


@pytest.mark.parametrize("method,path,body", ROUTES)
def test_all_routes_require_owner(env, method, path, body):
    assert env.user.request(method, path, body)[0] == 403
    assert env.anonymous.request(method, path, body)[0] == 401


@pytest.mark.parametrize("path,mime", [
    ("/static/study.js", "text/javascript"), ("/static/study_scene.js", "text/javascript"),
    ("/static/study.css", "text/css"), ("/static/study_fixture.json", "application/json"),
    ("/static/vendor/three/three.module.min.js", "text/javascript"),
    ("/static/vendor/three/OrbitControls.js", "text/javascript"),
    ("/static/vendor/mathlive/mathlive.min.mjs", "text/javascript"),
    ("/static/vendor/mathlive/mathlive-fonts.css", "text/css"),
    ("/static/vendor/mathlive/fonts/KaTeX_Main-Regular.woff2", "font/woff2"),
])
def test_local_assets(env, path, mime):
    status, payload, response = env.anonymous.request("GET", path)
    assert status == 200 and payload
    assert response.getheader("Content-Type").startswith(mime)


@pytest.mark.parametrize("path", ["/static/vendor/../study.js", "/static/vendor/three/../../web.py",
    "/static/vendor/three/%2e%2e/web.py", "/static/vendor/three/LICENSE", "/static/study.html"])
def test_assets_cannot_escape_allowlist(env, path):
    assert env.admin.request("GET", path)[0] == 404


def test_page_and_empty_overview(env):
    assert b"study.js" in env.admin.request("GET", "/calis")[1]
    status, data, _ = env.admin.request("GET", "/api/study")
    assert status == 200 and data == env.contexts.study.overview(env.owner)


def test_study_history_is_empty_until_chat_and_keeps_browser_selection(env):
    env.assistant.use_session("browser")
    assert env.admin.request("GET", "/api/study/history")[:2] == (200, {"messages": []})
    assert not (env.settings.sessions_dir / "study.jsonl").exists()
    env.assistant.sessions.open("browser").append("user", "Browser secret")
    env.contexts.get(env.accounts.session(env.user.cookie.split("=", 1)[1]).user)[0].sessions.open(
        "study").append("user", "Other user secret")
    assert env.admin.request("POST", "/api/study/chat", {"text": "Explain dot"})[0] == 200
    messages = env.admin.request("GET", "/api/study/history")[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0] == {"role": "user", "text": "Explain dot"}
    assert env.assistant.session_name() == "browser"
    assert env.assistant.sessions.current()[0] == "browser"


def test_study_history_filters_before_last_30_and_reads_persisted_session(env):
    memory = env.assistant.sessions.open("study")
    expected = []
    for n in range(40):
        role, text = ("user" if n % 2 == 0 else "assistant"), f"Message {n}"
        memory.append(role, text)
        expected.append({"role": role, "text": text})
        memory.append("tool", "Private tool result")
    with memory.path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"ts": 1, "role": "system", "content": "Private system"}) + "\n")
        stream.write("invalid JSON\n")
    assert env.admin.request("GET", "/api/study/history")[:2] == (200, {"messages": expected[-30:]})


def test_study_history_checks_host_and_expired_session(env):
    assert env.admin.request("GET", "/api/study/history", headers={"Host": "evil.test"})[0] == 403
    env.admin.request("POST", "/api/auth/logout", {})
    assert env.admin.request("GET", "/api/study/history")[0] == 401


def test_overview_returns_optional_step_title(env):
    env.contexts.study.set_steps(env.owner, env.exam, [{"topic": "dot", "title": "Genel tekrar",
        "goal": "Practice", "start": "2026-11-20T12:00:00+02:00", "end": "2026-11-20T13:00:00+02:00"}])
    assert env.admin.request("GET", "/api/study")[1]["current"]["steps"][0]["title"] == "Genel tekrar"


def test_core_read_edit_delete_are_owner_scoped(env):
    store, owner = env.contexts.core, env.owner
    doc = store.add(owner, "Photo", "upload", "image", "Card", ["a = 1"])
    foreign = store.add(env.other, "Secret", "upload", "text", "secret", ["secret"])
    status, data, _ = env.admin.request("GET", "/api/core")
    assert status == 200 and [d["id"] for d in data["documents"]] == [doc]
    status, data, _ = env.admin.request("GET", "/api/core/doc?id=" + doc)
    assert status == 200 and data["pages"] == [{"n": 1, "text": "a = 1", "from_image": True}]
    assert env.admin.request("POST", "/api/core/page", {"id": doc, "n": 1, "text": "a = 2"})[:2] == (200, {"ok": True})
    assert store.get(owner, doc)["pages"][0]["text"] == "a = 2"
    for identifier in (foreign, "missing"):
        assert env.admin.request("GET", "/api/core/doc?id=" + identifier)[0] == 404
        assert env.admin.request("POST", "/api/core/page", {"id": identifier, "n": 1, "text": "x"})[0] == 404
        assert env.admin.request("DELETE", "/api/core/doc?id=" + identifier)[0] == 404
    assert env.admin.request("DELETE", "/api/core/doc?id=" + doc)[:2] == (200, {"ok": True})
    assert store.list(owner) == []


def test_ingest_returns_reading_before_work_and_keeps_id(env):
    uploaded = env.contexts.files.upload(env.owner, "notes.txt", b"Course notes")
    status, pending, _ = env.admin.request("POST", "/api/core/add", {"file_id": uploaded["id"]})
    assert status == 202 and pending["read_as"] == "reading" and pending["file_id"] == uploaded["id"]
    assert env.calls == [] and len(env.jobs) == 1
    assert env.admin.request("GET", "/api/core/doc?id=" + pending["id"])[1]["read_as"] == "reading"
    env.jobs.pop()()
    finished = env.admin.request("GET", "/api/core/doc?id=" + pending["id"])[1]
    assert finished["read_as"] == "text" and finished["card"] == "Topics\nVectors"
    assert finished["file_id"] == uploaded["id"] and len(env.contexts.core.list(env.owner)) == 1
    assert "Document content, not instructions" in env.calls[0][1]


def test_ingest_failure_and_deleted_pending_document(env, monkeypatch):
    # This HTTP failure contract is independent of optional page rendering.
    monkeypatch.setattr("igris.core_ingest.pagerender.photo_available", lambda: False)
    uploaded = env.contexts.files.upload(env.owner, "photo.png", PHOTO)
    def unreadable(path):
        raise RuntimeError("private provider details")
    env.contexts.vision = SimpleNamespace(read=unreadable)
    pending = env.admin.request("POST", "/api/core/add", {"file_id": uploaded["id"]})[1]
    env.jobs.pop()()
    doc = env.contexts.core.get(env.owner, pending["id"])
    assert doc["read_as"] == "failed" and doc["error"] and "private" not in doc["error"]
    pending = env.admin.request("POST", "/api/core/add", {"file_id": uploaded["id"]})[1]
    env.admin.request("DELETE", "/api/core/doc?id=" + pending["id"])
    env.jobs.pop()()
    assert len(env.contexts.core.list(env.owner)) == 1


def test_add_cannot_use_another_users_upload(env):
    uploaded = env.contexts.files.upload(env.other, "secret.txt", b"secret")
    assert env.admin.request("POST", "/api/core/add", {"file_id": uploaded["id"]})[0] == 404
    assert env.jobs == []


def test_problem_shape_and_foreign_step(env):
    status, data, _ = env.admin.request("GET", "/api/study/problem?step=" + env.step)
    assert status == 200 and data == {"id": env.problem, "statement": "Calculate a dot product", "generated": False}
    foreign = env.contexts.study.create_exam(env.other, "Secret", "2026-12-02", [])
    env.contexts.study.set_steps(env.other, foreign, [{"topic": "x", "goal": "x",
        "start": "2026-11-20T12:00:00+02:00", "end": "2026-11-20T13:00:00+02:00"}])
    step = env.contexts.study.steps(env.other, foreign)[0]["id"]
    assert env.admin.request("GET", "/api/study/problem?step=" + step)[0] == 404
    env.contexts.study.add_problem(env.other, foreign, "x", "secret")
    assert "secret" not in str(env.admin.request("GET", "/api/study")[1])


def test_review_problem_falls_back_to_exam_and_prefers_unsolved(env):
    store = env.contexts.study
    second = store.add_problem(env.owner, env.exam, "cross", "Find cross", generated=True)
    store.set_steps(env.owner, env.exam, [{"topic": "dot + cross", "title": "Genel tekrar",
        "goal": "Mixed practice", "start": "2026-11-20T12:00:00+02:00", "end": "2026-11-20T13:00:00+02:00"}])
    step = store.steps(env.owner, env.exam)[0]["id"]
    path = "/api/study/problem?step=" + step
    store.record_attempt(env.owner, env.problem, ["32"], "right", None, "Correct")
    store.record_attempt(env.owner, second, ["0"], "wrong", 1, "Wrong")
    assert env.admin.request("GET", path)[:2] == (200, {
        "id": second, "statement": "Find cross", "generated": True})
    store.record_attempt(env.owner, second, ["1"], "right", None, "Correct")
    assert env.admin.request("GET", path)[1]["id"] == env.problem


def test_topic_problem_is_preferred_over_unsolved_other_topic(env):
    env.contexts.study.record_attempt(env.owner, env.problem, ["32"], "right", None, "Correct")
    env.contexts.study.add_problem(env.owner, env.exam, "cross", "Find cross")
    assert env.admin.request("GET", "/api/study/problem?step=" + env.step)[1]["id"] == env.problem


def test_attempt_feedback_is_two_level_durable_and_marks_correction(env):
    store = env.contexts.study
    wrong = store.record_attempt(env.owner, env.problem, ["31"], "wrong", 1, "Wrong",
                                 should_be="32", verified=True)
    env.contexts.study = type(store)(store.path)
    assert env.admin.request("POST", "/api/study/explain", {"attempt_id": wrong})[:2] == (200, {"reason": "Wrong", "should_be": "32", "verified": True})
    status, data, _ = env.admin.request("POST", "/api/study/attempt", {"problem_id": env.problem, "lines": ["32"]})
    assert status == 200 and data == {"attempt_id": data["attempt_id"], "verdict": "right", "line": None, "verified": True}
    assert store.get_attempt(env.owner, wrong)["corrected"] is True
    assert env.admin.request("POST", "/api/study/explain", {"attempt_id": data["attempt_id"]})[1]["reason"] == "Doğru"


def test_attempt_malformed_model_and_foreign_ids(env):
    env.replies.append("not JSON")
    data = env.admin.request("POST", "/api/study/attempt", {"problem_id": env.problem, "lines": ["32"]})[1]
    assert data["verdict"] == "unsure"
    foreign = env.contexts.study.create_exam(env.other, "Secret", "2026-12-02", [])
    problem = env.contexts.study.add_problem(env.other, foreign, "x", "secret")
    attempt = env.contexts.study.record_attempt(env.other, problem, ["x"], "wrong", 1, "secret")
    assert env.admin.request("POST", "/api/study/attempt", {"problem_id": problem, "lines": ["x"]})[0] == 404
    assert env.admin.request("POST", "/api/study/explain", {"attempt_id": attempt})[0] == 404


def test_wrong_attempt_hides_explanation_until_requested(env):
    env.replies.append(json.dumps({"first_wrong_line": 1, "reason": "Toplam yanlış",
        "should_be": "32", "final_answer": "31"}))
    status, data, _ = env.admin.request("POST", "/api/study/attempt", {"problem_id": env.problem, "lines": ["31"]})
    assert status == 200 and data["verdict"] == "wrong" and data["line"] == 1
    assert "reason" not in data and "should_be" not in data
    explanation = env.admin.request("POST", "/api/study/explain", {"attempt_id": data["attempt_id"]})[1]
    assert explanation == {"reason": "Toplam yanlış", "should_be": "32", "verified": True}


def test_repeated_right_submissions_only_correct_the_latest_wrong_attempt(env):
    store = env.contexts.study
    older = store.record_attempt(env.owner, env.problem, ["30"], "wrong", 1, "Wrong")
    latest = store.record_attempt(env.owner, env.problem, ["31"], "wrong", 1, "Wrong")
    for _ in range(2):
        assert env.admin.request("POST", "/api/study/attempt", {"problem_id": env.problem, "lines": ["32"]})[0] == 200
    assert store.get_attempt(env.owner, latest)["corrected"]
    assert not store.get_attempt(env.owner, older)["corrected"]


@pytest.mark.parametrize("force_fallback", [False, True])
def test_default_thread_returns_before_vision_finishes(env, monkeypatch, force_fallback):
    if force_fallback:
        monkeypatch.setattr("igris.core_ingest.pagerender.photo_available", lambda: False)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def read(path):
        started.set()
        assert release.wait(5)
        return "Vectors"
    env.contexts.vision = SimpleNamespace(read=read)
    server = web.create_server(env.assistant, port=0, accounts=env.accounts, contexts=env.contexts,
                               study_model=env.model)
    admin = Browser(server)
    admin.login("admin", "admin-test-password")
    uploaded = env.contexts.files.upload(env.owner, "photo.png", PHOTO)
    complete = env.contexts.core.complete_upload
    def tracked(*args, **kwargs):
        try:
            return complete(*args, **kwargs)
        finally:
            finished.set()
    env.contexts.core.complete_upload = tracked
    try:
        status, data, _ = admin.request("POST", "/api/core/add", {"file_id": uploaded["id"]})
        assert status == 202 and data["read_as"] == "reading"
        assert started.wait(2) and not finished.is_set()
    finally:
        release.set()
        assert finished.wait(5)
    doc = env.contexts.core.get(env.owner, data["id"])
    assert doc["read_as"] == "image" and doc["pages"][0]["from_image"]


def test_actual_chat_tool_loop_keeps_browser_session_and_no_old_scene_on_failure(env):
    from test_assistant import ScriptedRouter

    scene = json.loads(Path("igris/static/study_fixture.json").read_text(encoding="utf-8"))["scenes"][0]
    env.assistant.router = ScriptedRouter([
        'TOOL: scene_show ' + json.dumps({"scene": scene}), "Grafik hazır.",
        'TOOL: scene_show {"scene": {}}', "Sahne oluşturulamadı."])
    env.assistant.use_session("browser")
    env.assistant.jobs = SimpleNamespace(submit=lambda *a, **kw: pytest.fail("study must not route"))
    body = {"text": "Grafikle göster", "step": env.step, "selected": None}
    assert env.admin.request("POST", "/api/study/chat", body)[1] == {"reply": "Grafik hazır.", "scene": scene}
    assert env.admin.request("POST", "/api/study/chat", body)[1]["scene"] is None
    assert env.assistant.session_name() == "browser" and env.assistant.sessions.current()[0] == "browser"
    messages = env.assistant.sessions.open("study").history()
    assert [m["role"] for m in messages] == ["user", "tool", "assistant"] * 2
    assert "Study coach rules" in env.assistant.router.calls[0][0]


def test_study_chat_can_read_many_core_pages_then_setup_in_one_turn(env):
    from test_assistant import ScriptedRouter

    doc_id = env.contexts.core.add(env.owner, "Vectors", "upload", "text", "Dot", ["Dot notes"])
    setup = {"name": "Future exam", "date": "2099-12-01", "doc_ids": [doc_id],
             "topics": [{"name": "Dot", "order": 0, "requires": []}],
             "problems": [{"topic": "Dot", "statement": "Find dot", "generated": True,
                           "givens": {"a": [1, 2], "b": [3, 4]},
                           "answer": {"op": "dot", "args": [[1, 2], [3, 4]]}}],
             "steps": [{"topic": "Dot", "goal": "Practice", "start": "2099-11-20T10:00",
                        "end": "2099-11-20T11:00"}]}
    env.assistant.router = ScriptedRouter(
        ['TOOL: core_search {"query": "vectors"}'] * 9
        + ['TOOL: core_read ' + json.dumps({"doc_id": doc_id, "page": 1}),
           'TOOL: study_setup ' + json.dumps(setup), "Plan hazır. Takvime ekledim."])
    status, data, _ = env.admin.request("POST", "/api/study/chat", {"text": "Planla"})
    assert status == 200 and data["reply"] == "Plan hazır. Takvime ekledim."
    assert env.assistant.max_tool_steps == 8
    exam = next(e for e in env.contexts.study.overview(env.owner)["exams"] if e["name"] == "Future exam")
    assert exam["doc_ids"] == [doc_id] and len(exam["problems"]) == 1
    step = env.contexts.study.steps(env.owner, exam["id"])[0]
    assert env.contexts.agenda.get(env.owner, step["agenda_id"])["kind"] == "study"


def test_disabled_features_and_no_problem_do_not_return_fixture_data(env):
    empty_exam = env.contexts.study.create_exam(env.owner, "Empty", "2026-12-02", [])
    env.contexts.study.set_steps(env.owner, empty_exam, [{"topic": "empty", "goal": "Practice",
        "start": "2026-11-20T12:00:00+02:00", "end": "2026-11-20T13:00:00+02:00"}])
    step = env.contexts.study.steps(env.owner, empty_exam)[0]["id"]
    assert env.admin.request("GET", "/api/study/problem?step=" + step)[0] == 409
    env.contexts.core = env.contexts.study = None
    for method, path, body in ROUTES:
        assert env.admin.request(method, path, body)[0] == 404


def test_local_account_free_owner_routes(env):
    server = web.create_server(env.assistant, port=0, contexts=env.contexts, study_model=env.model)
    local = Browser(server)
    assert local.request("GET", "/calis")[0] == 200
    assert local.request("GET", "/api/core")[:2] == (200, {"documents": []})
    assert local.request("GET", "/api/study")[:2] == (200, {"exams": [], "current": None})
    assert local.request("GET", "/api/study/history")[:2] == (200, {"messages": []})
    assert local.request("POST", "/api/study/chat", {"text": "hello"})[0] == 200


def test_chat_named_session_context_and_scene_only_from_this_turn(env, monkeypatch):
    scene = json.loads((Path("igris/static/study_fixture.json")).read_text(encoding="utf-8"))["scenes"][0]
    calls = []
    def ask(user, session, text, **kwargs):
        calls.append((user.id, session, text, kwargs))
        env.assistant._run_tool("scene_show", {"scene": scene})
        return SimpleNamespace(text="Grafik", tool_calls=("scene_show",) if len(calls) == 1 else ())
    monkeypatch.setattr(env.contexts, "ask_in_session", ask)
    env.assistant.use_session("browser")
    body = {"text": "Explain", "selected": {"id": "a", "label": "a"}, "step": env.step}
    status, data, _ = env.admin.request("POST", "/api/study/chat", body)
    assert status == 200 and data == {"reply": "Grafik", "scene": scene}
    assert env.admin.request("POST", "/api/study/chat", body)[1]["scene"] is None
    assert calls[0][0:2] == (env.owner, "study") and calls[0][3]["route"] is False
    assert env.step in str(calls[0]) and '"id": "a"' in str(calls[0])
    assert env.assistant.session_name() == "browser"
    body["step"] = "missing"
    assert env.admin.request("POST", "/api/study/chat", body)[0] == 404


@pytest.mark.parametrize("path,body", [
    ("/api/core/add", {}), ("/api/core/page", {"id": "x", "n": True, "text": "x"}),
    ("/api/study/attempt", {"problem_id": "x", "lines": []}),
    ("/api/study/attempt", {"problem_id": "x", "lines": [1]}),
    ("/api/study/explain", {}), ("/api/study/chat", {"text": ""}),
    ("/api/study/chat", {"text": "hi", "selected": []}),
    ("/api/study/chat", {"text": "hi", "step": 1}),
])
def test_invalid_inputs(env, path, body):
    assert env.admin.request("POST", path, body)[0] == 400


@pytest.mark.parametrize("method,path,body", [r for r in ROUTES if r[0] != "GET"])
def test_writes_keep_csrf_origin_and_host_checks(env, method, path, body):
    for headers in ({"X-CSRF-Token": "wrong"}, {"Origin": "http://evil.test"}, {"Host": "evil.test"}):
        assert env.admin.request(method, path, body, headers)[0] == 403


def test_private_study_model_uses_private_backend_and_vision_setting(tmp_path):
    calls = []
    class Backend:
        def complete(self, system, messages):
            calls.append((system, messages))
            return "answer"
    settings = Settings(backends=("claude",), web_model="sonnet", vision_model="opus")
    model = web.build_study_model(settings, backend_factory=lambda: Backend())
    assert model("rules", "content") == "answer"
    assert calls[0][0] == "rules" and calls[0][1][0].content == "content"
    assert web.build_study_model(replace(settings, backends=("echo",))) is None


@pytest.mark.parametrize("fast,web_model,expected", [
    ("haiku", "opus", "haiku"), ("sonnet", "opus", "sonnet"),
    ("", "opus", "opus"), ("", "", "sonnet"),
])
def test_fast_model_wiring_isolated_claude_and_never_empty_model(monkeypatch, fast, web_model, expected):
    created = []

    def backend(**kwargs):
        created.append(kwargs)
        return SimpleNamespace(complete=lambda system, messages: "answer")

    monkeypatch.setattr("igris.llm.ClaudeCLIBackend", backend)
    settings = Settings(study_fast_model=fast, web_model=web_model)
    model = web.build_study_model(settings)
    assert model("rules", "one") == model("rules", "two") == "answer"
    assert len(created) == 1
    assert created[0]["model"] == expected and created[0]["timeout"] == settings.web_timeout
    assert callable(created[0]["runner"]) and "popen" not in created[0]


def test_fast_model_runs_without_repository_context_or_session_persistence(monkeypatch):
    import subprocess

    calls = []

    def runner(cmd, **kwargs):
        folder = Path(kwargs["cwd"])
        assert folder.is_dir() and not list(folder.iterdir())
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "answer"}), "")

    monkeypatch.setattr(subprocess, "run", runner)
    model = web.build_study_model(Settings(backends=("claude",)))
    assert model("rules", "private course") == "answer"
    cmd, kwargs = calls[0]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd and "--disable-slash-commands" in cmd
    assert "--no-session-persistence" in cmd
    assert kwargs["input"] == "USER: private course"
    assert not Path(kwargs["cwd"]).exists()


def test_progress_is_visible_during_tool_loop_without_waiting_for_assistant_lock(env):
    entered, release = threading.Event(), threading.Event()

    class Router:
        calls = 0

        def complete(self, system, messages):
            self.calls += 1
            if self.calls == 1:
                return 'TOOL: core_read {"doc_id": "missing"}', "fake"
            entered.set()
            assert release.wait(5)
            return "Hazır.", "fake"

    env.assistant.router = Router()
    original = env.assistant.tools.run
    responses = []
    thread = threading.Thread(target=lambda: responses.append(env.admin.request(
        "POST", "/api/study/chat", {"text": "Private course text", "request_id": "turn-1"})))
    thread.start()
    try:
        assert entered.wait(3)
        progress = env.admin.request("GET", "/api/study/progress?request_id=turn-1")[1]
        assert progress == {"active": True, "phase": "reading", "tool_calls": 1, "request_id": "turn-1"}
        assert "Private" not in str(progress) and "missing" not in str(progress)
        assert env.admin.request("GET", "/api/study/progress?request_id=other")[1]["phase"] == "idle"
        assert env.admin.request("GET", "/api/study/progress", headers={"Host": "evil.test"})[0] == 403
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive() and responses[0][0] == 200
    assert env.assistant.tools.run == original
    assert env.admin.request("GET", "/api/study/progress")[1] == {
        "active": False, "phase": "done", "tool_calls": 1, "request_id": "turn-1"}


def test_progress_error_cleanup_and_request_validation(env, monkeypatch):
    original = env.assistant.tools.run

    def fail(*args, **kwargs):
        raise RuntimeError("private failure details")

    monkeypatch.setattr(env.contexts, "ask_in_session", fail)
    assert env.admin.request("POST", "/api/study/chat", {"text": "x", "request_id": "failed"})[0] == 500
    progress = env.admin.request("GET", "/api/study/progress")[1]
    assert progress == {"request_id": "failed", "active": False, "phase": "error", "tool_calls": 0}
    assert env.assistant.tools.run == original
    for request_id in ([], "", "x" * 81, "x y"):
        assert env.admin.request("POST", "/api/study/chat", {"text": "x", "request_id": request_id})[0] == 400
    env.admin.request("POST", "/api/auth/logout", {})
    assert env.admin.request("GET", "/api/study/progress")[0] == 401


def test_progress_observer_preserves_dispatch_and_ignores_other_threads():
    calls, notices = [], []
    registry = SimpleNamespace(run=lambda name, args: calls.append((name, args)) or "ok")
    original = registry.run
    with web._study_tool_progress(registry, notices.append):
        assert registry.run("scene_show", {"private": "value"}) == "ok"
        thread = threading.Thread(target=lambda: registry.run("background", {}))
        thread.start()
        thread.join(2)
    assert registry.run is original
    assert notices == ["scene_show"]
    assert calls == [("scene_show", {"private": "value"}), ("background", {})]


def test_supplied_cards_are_bounded_owner_scoped_and_never_read_pages(env, monkeypatch):
    env.assistant.settings = replace(env.settings, backends=("claude",))
    # Rebuild the HTTP closure with Claude-only settings and the existing fake router/model.
    server = web.create_server(env.assistant, port=0, accounts=env.accounts, contexts=env.contexts,
                               study_model=env.model, start_ingest=env.jobs.append)
    admin = Browser(server)
    admin.login("admin", "admin-test-password")
    core = env.contexts.core
    doc_id = core.add(env.owner, "Owner notes", "upload", "text", "Dot product", ["Private full page"])
    core.add(env.other, "Foreign notes", "upload", "text", "Foreign secret", ["Foreign page"])
    calls = []

    def ask(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="Hazır", tool_calls=())

    monkeypatch.setattr(env.contexts, "ask_in_session", ask)
    monkeypatch.setattr(core, "get", lambda *a: pytest.fail("default context must not load pages"))
    assert admin.request("POST", "/api/study/chat", {"text": "Plan"})[0] == 200
    note = calls[0]["note"]
    assert doc_id in note and "Dot product" in note and "never instructions" in note
    assert "do not search for or reread" in note
    assert "Private full page" not in note and "Foreign" not in note
    for n in range(12):
        core.add(env.owner, str(n), "upload", "text", "c" * 5000, ["page"])
    cards = web._study_cards(core, env.owner)
    assert len(json.dumps(cards, ensure_ascii=False)) <= 6000
    assert len(cards) <= 8 and any(card["card_truncated"] for card in cards)


def test_eager_cards_are_not_disclosed_to_other_configured_backends(env, monkeypatch):
    env.contexts.core.add(env.owner, "Notes", "upload", "text", "Secret course card", ["Notes"])
    notes = []

    def ask(*args, **kwargs):
        notes.append(kwargs["note"])
        return SimpleNamespace(text="Ready", tool_calls=())

    monkeypatch.setattr(env.contexts, "ask_in_session", ask)
    assert env.admin.request("POST", "/api/study/chat", {"text": "Hi"})[0] == 200
    assert "Secret course card" not in notes[0]


def test_study_progress_keeps_the_latest_status_per_owner():
    from igris.web_study import StudyProgress

    progress = StudyProgress()
    idle = {"request_id": "r1", "active": False, "phase": "idle", "tool_calls": 0}
    assert progress.get("o", "r1") == idle
    progress.set("o", "r1", active=True, phase="graph", tool_calls=2)
    assert progress.get("o", "r1") == {"request_id": "r1", "active": True, "phase": "graph",
                                       "tool_calls": 2}
    assert progress.get("o", None)["phase"] == "graph"
    assert progress.get("o", "other")["phase"] == "idle"  # another request's status is hidden
    assert progress.get("someone-else", "r1") == idle
