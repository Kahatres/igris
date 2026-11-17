"""The Claude and Codex backends refuse to run when they would bill an API account."""
import subprocess

import pytest

import igris.llm as llm
from igris.llm import ClaudeCLIBackend, CodexCLIBackend, Message, PaidUsageBlocked, Router, EchoBackend

MSG = [Message("user", "hi")]
# Captured at import, before the autouse fixture in conftest.py replaces it.
REAL_LOGIN_PROBLEM = llm.codex_login_problem


def never(*a, **kw):
    raise AssertionError("the CLI must not run")


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK"])
def test_claude_blocks_paid_environment(name):
    backend = ClaudeCLIBackend(runner=never, env={name: "1"})
    with pytest.raises(PaidUsageBlocked, match=name):
        backend.complete("s", MSG)


def test_claude_allows_false_flags_and_explicit_opt_in():
    ok = subprocess.CompletedProcess([], 0, '{"result": "ok"}', "")
    assert ClaudeCLIBackend(runner=lambda *a, **k: ok, env={"CLAUDE_CODE_USE_VERTEX": "0"}).complete("s", MSG) == "ok"
    paid = {"ANTHROPIC_API_KEY": "sk", "IGRIS_ALLOW_PAID": "1"}
    assert ClaudeCLIBackend(runner=lambda *a, **k: ok, env=paid).complete("s", MSG) == "ok"


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "CODEX_API_KEY"])
def test_codex_blocks_api_key_environment(name):
    backend = CodexCLIBackend(runner=never, env={name: "sk"}, login_check=lambda e, r: None)
    with pytest.raises(PaidUsageBlocked, match=name):
        backend.complete("s", MSG)


def _status(code, stderr):
    return lambda cmd, **kw: subprocess.CompletedProcess(cmd, code, "", stderr)


@pytest.mark.parametrize("code,stderr,ok", [
    (0, "Logged in using ChatGPT\n", True),
    (0, "Logged in using an API key - sk-proj-***\n", False),
    (1, "Not logged in\n", False),
])
def test_login_status_requires_chatgpt(code, stderr, ok):
    problem = REAL_LOGIN_PROBLEM("codex", _status(code, stderr))
    assert (problem is None) is ok


def test_login_is_checked_once_and_blocks_api_key_login():
    calls = []

    def check(exe, runner):
        calls.append(exe)
        return "codex is not logged in with ChatGPT"
    backend = CodexCLIBackend(runner=never, env={}, login_check=check)
    for _ in range(2):
        with pytest.raises(PaidUsageBlocked):
            backend.complete("s", MSG)
    assert calls == ["codex", "codex"]   # a failed check is retried, a passed one is cached

    events = '{"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}'
    seen = []
    good = CodexCLIBackend(runner=lambda *a, **k: subprocess.CompletedProcess([], 0, events, ""), env={},
                           login_check=lambda e, r: seen.append(e))
    assert good.complete("s", MSG) == "ok" and good.complete("s", MSG) == "ok"
    assert seen == ["codex"]


def test_router_falls_back_when_a_backend_would_bill():
    blocked = ClaudeCLIBackend(runner=never, env={"ANTHROPIC_API_KEY": "sk"})
    text, name = Router([blocked, EchoBackend()]).complete("s", MSG)
    assert name == "echo"


def test_default_login_check_is_the_real_one(monkeypatch):
    calls = []
    monkeypatch.setattr(llm, "codex_login_problem", lambda exe, runner: calls.append(exe) or "not ChatGPT")
    with pytest.raises(PaidUsageBlocked, match="not ChatGPT"):
        CodexCLIBackend(runner=never, env={}).complete("s", MSG)
    assert calls == ["codex"]
