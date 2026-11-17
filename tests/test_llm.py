import json
import subprocess

import pytest

from igris.llm import (
    BackendError,
    ClaudeCLIBackend,
    CodexCLIBackend,
    EchoBackend,
    Message,
    PaidUsageBlocked,
    RateLimited,
    Router,
    build_backend,
    codex_login_problem as REAL_LOGIN_PROBLEM,  # imported before conftest stubs it
    render_transcript,
)

MSGS = [Message("user", "hi"), Message("assistant", "hello"), Message("user", "bye")]


class FakeRunner:
    def __init__(self, stdout="", stderr="", returncode=0, exc=None):
        self.stdout, self.stderr, self.returncode, self.exc = stdout, stderr, returncode, exc
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def codex_out(*texts):
    lines = [json.dumps({"type": "thread.started"})]
    for t in texts:
        lines.append(json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": t}}))
    return "\n".join(lines) + "\n"


def test_render_transcript():
    assert render_transcript(MSGS) == "USER: hi\n\nASSISTANT: hello\n\nUSER: bye"
    assert render_transcript([]) == ""


def test_render_transcript_labels_tool_results_apart_from_tool_calls():
    text = render_transcript([Message("user", "add it"), Message("tool", "agenda_add -> ok")])
    assert text == "USER: add it\n\nTOOL RESULT: agenda_add -> ok"
    assert "\nTOOL:" not in text


def test_echo():
    b = EchoBackend()
    assert b.name == "echo"
    assert b.complete("s", MSGS) == "echo: bye"
    assert b.complete("s", [Message("assistant", "x")]) == "echo: "


def test_claude_success_and_command():
    r = FakeRunner(stdout=json.dumps({"result": "answer", "is_error": False}))
    b = ClaudeCLIBackend(model="opus", timeout=7, runner=r, exe="cl")
    assert b.complete("SYS", MSGS) == "answer"
    cmd, kw = r.calls[0]
    assert cmd == ["cl", "-p", "--model", "opus", "--output-format", "json",
                   "--system-prompt", "SYS", "--tools", "", "--strict-mcp-config"]
    assert kw["input"] == render_transcript(MSGS)
    assert kw["capture_output"] is True and kw["text"] is True and kw["timeout"] == 7


def test_claude_is_error():
    r = FakeRunner(stdout=json.dumps({"result": "boom", "is_error": True}))
    with pytest.raises(BackendError) as ei:
        ClaudeCLIBackend(runner=r).complete("s", MSGS)
    assert not isinstance(ei.value, RateLimited)


def test_claude_is_error_limit():
    r = FakeRunner(stdout=json.dumps({"result": "You've hit your limit", "is_error": True}))
    with pytest.raises(RateLimited) as ei:
        ClaudeCLIBackend(runner=r).complete("s", MSGS)
    assert ei.value.backend == "claude" and ei.value.retry_after is None


@pytest.mark.parametrize("stdout", ["not json", "", "[1]", json.dumps({"x": 1})])
def test_claude_bad_output(stdout):
    with pytest.raises(BackendError):
        ClaudeCLIBackend(runner=FakeRunner(stdout=stdout)).complete("s", MSGS)


def test_claude_nonzero_exit():
    with pytest.raises(BackendError, match="code 2"):
        ClaudeCLIBackend(runner=FakeRunner(returncode=2, stderr="bad")).complete("s", MSGS)


@pytest.mark.parametrize("text", ["Usage Limit reached", "RATE LIMIT", "you hit your limit"])
def test_claude_rate_limit_exit(text):
    with pytest.raises(RateLimited):
        ClaudeCLIBackend(runner=FakeRunner(returncode=1, stderr=text)).complete("s", MSGS)
    with pytest.raises(RateLimited):
        ClaudeCLIBackend(runner=FakeRunner(returncode=1, stdout=text)).complete("s", MSGS)


def test_claude_timeout_and_missing():
    t = FakeRunner(exc=subprocess.TimeoutExpired("claude", 1))
    with pytest.raises(BackendError, match="timed out"):
        ClaudeCLIBackend(runner=t).complete("s", MSGS)
    m = FakeRunner(exc=FileNotFoundError())
    with pytest.raises(BackendError, match="not found"):
        ClaudeCLIBackend(runner=m).complete("s", MSGS)


def test_codex_success_and_command():
    r = FakeRunner(stdout=codex_out("first", "last"))
    b = CodexCLIBackend(model="m", timeout=9, runner=r, exe="cx")
    assert b.complete("SYS", MSGS) == "last"
    cmd, kw = r.calls[0]
    assert cmd == ["cx", "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                   "-m", "m", "--json", "-"]
    assert kw["input"] == "SYSTEM:\nSYS\n\n" + render_transcript(MSGS)
    assert kw["timeout"] == 9 and kw["text"] is True and kw["capture_output"] is True


def test_codex_ignores_noise():
    out = "garbage\n\n[1]\n" + json.dumps(
        {"type": "item.completed", "item": {"type": "command_execution"}}) + "\n"
    out += codex_out("ok")
    assert CodexCLIBackend(runner=FakeRunner(stdout=out)).complete("s", MSGS) == "ok"


def test_codex_no_message():
    with pytest.raises(BackendError, match="no agent message"):
        CodexCLIBackend(runner=FakeRunner(stdout='{"type":"x"}\n')).complete("s", MSGS)


def test_codex_errors():
    with pytest.raises(BackendError, match="code 3"):
        CodexCLIBackend(runner=FakeRunner(returncode=3)).complete("s", MSGS)
    with pytest.raises(RateLimited):
        CodexCLIBackend(runner=FakeRunner(returncode=1, stderr="usage limit")).complete("s", MSGS)
    with pytest.raises(RateLimited):
        CodexCLIBackend(runner=FakeRunner(stdout="Rate Limit hit")).complete("s", MSGS)
    with pytest.raises(BackendError, match="timed out"):
        CodexCLIBackend(runner=FakeRunner(exc=subprocess.TimeoutExpired("c", 1))).complete("s", MSGS)
    with pytest.raises(BackendError, match="not found"):
        CodexCLIBackend(runner=FakeRunner(exc=FileNotFoundError())).complete("s", MSGS)


class Stub:
    def __init__(self, name, outcomes):
        self.name = name
        self.outcomes = list(outcomes)
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        o = self.outcomes[0] if len(self.outcomes) == 1 else self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def test_router_empty():
    with pytest.raises(ValueError):
        Router([])


def test_router_alternates():
    a, b = Stub("a", ["A"]), Stub("b", ["B"])
    r = Router([a, b])
    got = [r.complete("s", MSGS) for _ in range(4)]
    assert got == [("A", "a"), ("B", "b"), ("A", "a"), ("B", "b")]


def test_router_fallback_on_error():
    a, b = Stub("a", [BackendError("x")]), Stub("b", ["B"])
    r = Router([a, b])
    assert r.complete("s", MSGS) == ("B", "b")
    assert r.complete("s", MSGS) == ("B", "b")
    assert a.calls == 1


def test_router_all_fail_lists_errors():
    r = Router([Stub("a", [BackendError("err-a")]), Stub("b", [BackendError("err-b")])])
    with pytest.raises(BackendError) as ei:
        r.complete("s", MSGS)
    assert "err-a" in str(ei.value) and "err-b" in str(ei.value)


def test_router_rate_limit_rests_default_60():
    clock = Clock()
    a = Stub("a", [RateLimited("a", None), "A"])
    b = Stub("b", ["B"])
    r = Router([a, b], clock=clock)
    assert r.complete("s", MSGS) == ("B", "b")  # a limited, falls to b
    assert r.complete("s", MSGS) == ("B", "b")
    assert r.complete("s", MSGS) == ("B", "b")
    assert a.calls == 1
    clock.t += 59
    r.complete("s", MSGS)
    r.complete("s", MSGS)
    assert a.calls == 1
    clock.t += 1
    results = [r.complete("s", MSGS)[1] for _ in range(2)]
    assert "a" in results and a.calls == 2


def test_router_retry_after_used():
    clock = Clock()
    a = Stub("a", [RateLimited("a", 5.0), "A"])
    b = Stub("b", ["B"])
    r = Router([a, b], clock=clock)
    r.complete("s", MSGS)
    clock.t += 5
    names = [r.complete("s", MSGS)[1] for _ in range(2)]
    assert "a" in names


def test_router_all_resting():
    clock = Clock()
    r = Router([Stub("a", [RateLimited("a", None)])], clock=clock)
    with pytest.raises(BackendError):
        r.complete("s", MSGS)
    with pytest.raises(BackendError, match="resting"):
        r.complete("s", MSGS)


def test_rate_limited_attrs():
    e = RateLimited("x", 3.0, "d")
    assert e.backend == "x" and e.retry_after == 3.0
    assert isinstance(e, BackendError)


def _fake_modules(monkeypatch):
    import sys
    import types

    made = []

    def make_cls(label):
        class Fake:
            def __init__(self, *args, **kwargs):
                self.args, self.kwargs = args, kwargs
                made.append(label)
        return Fake

    openai = types.ModuleType("igris.llm_openai")
    openai.OpenAIAPIBackend = make_cls("openai")
    extra = types.ModuleType("igris.llm_extra")
    extra.GeminiBackend = make_cls("gemini")
    extra.OllamaBackend = make_cls("ollama")
    monkeypatch.setitem(sys.modules, "igris.llm_openai", openai)
    monkeypatch.setitem(sys.modules, "igris.llm_extra", extra)
    return made


def test_build_backend_api_names(monkeypatch):
    from igris.config import Settings

    made = _fake_modules(monkeypatch)
    s = Settings(openai_model="m1", gemini_model="m2", ollama_model="m3",
                 ollama_url="http://u")
    kw = dict(claude_model="c", codex_model="x", timeout=11, settings=s)
    budget = object()
    o = build_backend("openai", budget=budget, **kw)
    assert o.kwargs == {"model": "m1", "budget": budget, "timeout": 11}
    g = build_backend("gemini", **kw)
    assert g.kwargs == {"model": "m2", "timeout": 11}
    ol = build_backend("ollama", **kw)
    assert ol.kwargs == {"model": "m3", "base_url": "http://u", "timeout": 11}
    assert made == ["openai", "gemini", "ollama"]


@pytest.mark.parametrize("name", ["groq", "openrouter", "cloudflare"])
def test_build_backend_compat_names_use_registry_model_override_and_shared_quota(
        name, tmp_path):
    from igris.config import Settings
    from igris.llm_compat import PROVIDERS, CompatBackend

    s = Settings(data_dir=tmp_path, compat_models={"groq": "custom-groq"})
    b = build_backend(name, claude_model="c", codex_model="x", timeout=7, settings=s)
    assert isinstance(b, CompatBackend) and b.provider is PROVIDERS[name]
    assert b.timeout == 7 and b.trains_on_data is PROVIDERS[name].trains_on_data
    assert b.model == ("custom-groq" if name == "groq" else PROVIDERS[name].default_model)
    assert b.quota.path == tmp_path / "quota.json"


@pytest.mark.parametrize("name", ["openai", "gemini", "ollama", "groq"])
def test_build_backend_api_requires_settings(name):
    with pytest.raises(ValueError):
        build_backend(name, claude_model="c", codex_model="x", timeout=1)


def test_build_backend():
    kw = dict(claude_model="c", codex_model="x", timeout=11)
    c = build_backend("claude", **kw)
    assert isinstance(c, ClaudeCLIBackend) and c.model == "c" and c.timeout == 11
    x = build_backend("codex", **kw)
    assert isinstance(x, CodexCLIBackend) and x.model == "x" and x.timeout == 11
    assert isinstance(build_backend("echo", **kw), EchoBackend)
    with pytest.raises(ValueError):
        build_backend("nope", **kw)


# ---- resolving npm .cmd shims on Windows ----

from igris.llm import _resolve_cli  # noqa: E402

CLAUDE_SHIM = ('@ECHO off\r\nCALL :find_dp0\r\n'
               '"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n')
CODEX_SHIM = ('IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n)\r\n'
              '"%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')


def _which(mapping):
    return lambda name: mapping.get(name)


def test_resolve_cli_runs_shim_exe_target(tmp_path):
    shim = tmp_path / "claude.cmd"
    shim.write_text(CLAUDE_SHIM, encoding="utf-8")
    argv = _resolve_cli("claude", which=_which({"claude": str(shim)}), is_windows=True)
    assert argv == [str(tmp_path / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")]


def test_resolve_cli_runs_node_script_with_system_node(tmp_path):
    shim = tmp_path / "codex.cmd"
    shim.write_text(CODEX_SHIM, encoding="utf-8")
    argv = _resolve_cli("codex", which=_which({"codex": str(shim), "node": "C:/nodejs/node.exe"}),
                        is_windows=True)
    assert argv == ["C:/nodejs/node.exe", str(tmp_path / "node_modules" / "@openai" / "codex" / "bin" / "codex.js")]


def test_resolve_cli_prefers_node_next_to_shim(tmp_path):
    shim = tmp_path / "codex.cmd"
    shim.write_text(CODEX_SHIM, encoding="utf-8")
    (tmp_path / "node.exe").write_text("", encoding="utf-8")
    argv = _resolve_cli("codex", which=_which({"codex": str(shim), "node": "C:/other/node.exe"}),
                        is_windows=True)
    assert argv[0] == str(tmp_path / "node.exe")


def test_resolve_cli_plain_executable_and_missing():
    assert _resolve_cli("claude", which=_which({"claude": "/usr/bin/claude"}), is_windows=False) == ["/usr/bin/claude"]
    assert _resolve_cli("claude", which=_which({}), is_windows=True) == ["claude"]


def test_resolve_cli_unknown_shim_falls_back_to_shim(tmp_path):
    shim = tmp_path / "tool.cmd"
    shim.write_text("@echo off\r\nsomething %*\r\n", encoding="utf-8")
    assert _resolve_cli("tool", which=_which({"tool": str(shim)}), is_windows=True) == [str(shim)]


def test_strict_resolver_rejects_unknown_or_unreadable_shims(tmp_path):
    shim = tmp_path / "tool.cmd"
    shim.write_text("@echo off\nsomething %*\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported_cli_shim"):
        _resolve_cli("tool", which=_which({"tool": str(shim)}), is_windows=True, strict=True)
    shim.unlink()
    with pytest.raises(OSError):
        _resolve_cli("tool", which=_which({"tool": str(shim)}), is_windows=True, strict=True)


def test_strict_resolver_checks_explicit_missing_shim(tmp_path):
    with pytest.raises(OSError):
        _resolve_cli(str(tmp_path / "missing.cmd"), which=_which({}),
                     is_windows=True, strict=True)


def test_backends_use_resolved_prefix(monkeypatch):
    import igris.llm as llm

    monkeypatch.setattr(llm, "resolve_cli", lambda exe: ["node", f"/x/{exe}.js"])
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": "ok"}), "")

    assert ClaudeCLIBackend(runner=runner, env={}).complete("sys", [Message("user", "hi")]) == "ok"
    assert seen[0][:3] == ["node", "/x/claude.js", "-p"]


def test_cli_calls_use_utf8_whatever_the_locale():
    # Windows' default codepage (e.g. cp1254) would garble Turkish text; codex rejects non-UTF-8 stdin.
    seen = []

    def runner(cmd, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(cmd, 0, "Logged in using ChatGPT", "")

    import igris.llm as llm

    llm._run_cli("x", runner, ["x"], "ğüşİ", 5)
    REAL_LOGIN_PROBLEM("codex", runner)
    assert len(seen) == 2
    for kw in seen:
        assert kw["encoding"] == "utf-8" and kw["errors"] == "replace"


# --- warm Claude process (a pre-started CLI waits for the next turn) ---

def _stream_result(text="ok", is_error=False):
    events = [{"type": "system", "subtype": "init"},
              {"type": "result", "subtype": "success", "is_error": is_error, "result": text}]
    return "\n".join(json.dumps(e) for e in events) + "\n"


class FakeProc:
    def __init__(self, cmd, stdout="", stderr="", returncode=0, exc=None):
        self.cmd, self._out, self._err, self._rc, self._exc = cmd, stdout, stderr, returncode, exc
        self.returncode = None
        self.dead = False
        self.killed = False
        self.inputs = []

    def poll(self):
        return 1 if self.dead else self.returncode

    def communicate(self, input=None, timeout=None):
        self.inputs.append((input, timeout))
        if self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc
        self.returncode = self._rc
        return self._out, self._err

    def kill(self):
        self.killed = True
        self.dead = True


class FakePopen:
    """Hands out FakeProcs; ``script`` gives each spawned process's (stdout, stderr, rc, exc)."""

    def __init__(self, *script, fail=None):
        self.script = list(script)
        self.fail = fail
        self.procs = []
        self.kwargs = []

    def __call__(self, cmd, **kw):
        if self.fail is not None:
            raise self.fail
        out, err, rc, exc = self.script.pop(0) if self.script else (_stream_result(), "", 0, None)
        proc = FakeProc(cmd, out, err, rc, exc)
        self.procs.append(proc)
        self.kwargs.append(kw)
        return proc


def test_warm_claude_answers_and_keeps_a_process_ready():
    popen = FakePopen((_stream_result("first"), "", 0, None), (_stream_result("second"), "", 0, None))
    b = ClaudeCLIBackend(popen=popen, env={})
    assert b.complete("sys", MSGS) == "first"
    assert len(popen.procs) == 2            # the one that answered + the next one, started in advance
    ready = popen.procs[1]
    assert ready.inputs == []               # waiting, nothing sent yet
    assert b.complete("sys", MSGS) == "second"
    assert ready.inputs                     # the pre-started process answered the second turn
    assert len(popen.procs) == 3


def test_warm_claude_command_and_input():
    popen = FakePopen()
    ClaudeCLIBackend(model="opus", timeout=42, popen=popen, env={}).complete("be brief", MSGS)
    cmd = popen.procs[0].cmd
    assert cmd[:2] == ["claude", "-p"]
    for flag, value in (("--model", "opus"), ("--input-format", "stream-json"),
                        ("--output-format", "stream-json"), ("--system-prompt", "be brief"), ("--tools", "")):
        assert cmd[cmd.index(flag) + 1] == value
    assert "--verbose" in cmd                # stream-json output requires it
    assert "--strict-mcp-config" in cmd      # the owner's claude.ai connectors stay out
    sent, timeout = popen.procs[0].inputs[0]
    assert timeout == 42
    line = json.loads(sent)
    assert line == {"type": "user", "message": {"role": "user", "content": render_transcript(MSGS)}}
    kw = popen.kwargs[0]
    assert kw["encoding"] == "utf-8" and kw["errors"] == "replace"


def test_warm_claude_errors():
    popen = FakePopen((_stream_result("boom", is_error=True), "", 0, None),
                      ("", "", 0, None),
                      ("", "crash", 2, None),
                      (_stream_result("You've hit your limit", is_error=True), "", 0, None))
    b = ClaudeCLIBackend(popen=popen, env={})
    with pytest.raises(BackendError, match="boom"):
        b.complete("sys", MSGS)
    with pytest.raises(BackendError, match="no result"):
        b.complete("sys", MSGS)
    with pytest.raises(BackendError, match="code 2: crash"):
        b.complete("sys", MSGS)
    with pytest.raises(RateLimited):
        b.complete("sys", MSGS)


def test_warm_claude_timeout_kills_the_process():
    popen = FakePopen(("", "", 0, subprocess.TimeoutExpired("claude", 5)))
    b = ClaudeCLIBackend(timeout=5, popen=popen, env={})
    with pytest.raises(BackendError, match="timed out after 5s"):
        b.complete("sys", MSGS)
    assert popen.procs[0].killed


def test_warm_claude_replaces_a_dead_ready_process():
    popen = FakePopen()
    b = ClaudeCLIBackend(popen=popen, env={})
    b.complete("sys", MSGS)
    popen.procs[1].dead = True              # the waiting process exited on its own
    assert b.complete("sys", MSGS) == "ok"
    assert popen.procs[1].inputs == []
    assert popen.procs[2].inputs            # a fresh one answered


def test_warm_claude_keeps_one_ready_process_per_system_prompt_and_evicts():
    popen = FakePopen()
    b = ClaudeCLIBackend(popen=popen, env={})
    b.complete("a", MSGS)
    b.complete("b", MSGS)
    ready_a, ready_b = popen.procs[1], popen.procs[3]
    assert not ready_a.killed and not ready_b.killed
    b.complete("c", MSGS)                   # a third prompt pushes out the least recently used one
    assert ready_a.killed and not ready_b.killed
    b.close()
    assert all(p.killed or p.returncode is not None for p in popen.procs)


def test_warm_claude_paid_env_spawns_nothing():
    popen = FakePopen()
    with pytest.raises(PaidUsageBlocked):
        ClaudeCLIBackend(popen=popen, env={"ANTHROPIC_API_KEY": "x"}).complete("sys", MSGS)
    assert popen.procs == []


def test_warm_claude_missing_executable():
    b = ClaudeCLIBackend(popen=FakePopen(fail=FileNotFoundError("nope")), env={})
    with pytest.raises(BackendError, match="executable not found"):
        b.complete("sys", MSGS)


def test_build_backend_claude_is_warm():
    b = build_backend("claude", claude_model="sonnet", codex_model="x", timeout=10)
    assert b.popen is subprocess.Popen
