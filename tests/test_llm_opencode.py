import json
import subprocess

import pytest

from igris.llm import BackendError, Message, PaidUsageBlocked, RateLimited
from igris.llm_opencode import FREE_MODELS, OpenCodeBackend
from igris.config import Settings
from igris.gate import GatedRouter
from igris.llm import build_backend

MSGS = [Message("user", "merhaba")]


def event(kind, **part):
    return json.dumps({"type": kind, "part": {"type": kind, **part}})


def ok_output(text="selam", cost=0):
    return "\n".join([event("step_start"), event("text", text=text),
                      event("step_finish", cost=cost)])


class Runner:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.proc = subprocess.CompletedProcess([], returncode, stdout, stderr)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return self.proc


def test_command_prompt_on_stdin_and_empty_workdir():
    runner = Runner(ok_output("tamam"))
    backend = OpenCodeBackend(runner=runner)
    assert backend.complete("sys", MSGS) == "tamam"
    cmd, kwargs = runner.calls[0]
    assert cmd == ["opencode", "run", "-m", "opencode/deepseek-v4.1-flash-free",
                   "--format", "json"]
    assert "--auto" not in cmd
    assert "SYSTEM:\nsys" in kwargs["input"] and "USER: merhaba" in kwargs["input"]
    assert kwargs["cwd"].split("igris-opencode-")[0] != kwargs["cwd"]
    assert kwargs["encoding"] == "utf-8"


def test_text_parts_are_joined_and_noise_is_ignored():
    out = "\n".join(["not json", "[1]", event("text", text="a"), event("text", text="b"),
                     event("step_finish", cost=0)])
    assert OpenCodeBackend(runner=Runner(out)).complete("s", MSGS) == "ab"


def test_only_allowlisted_free_models():
    for bad in ("claude-sonnet-4", "something-free", "", "opencode/big-pickle"):
        with pytest.raises(ValueError):
            OpenCodeBackend(bad)
    assert OpenCodeBackend("big-pickle").model == "big-pickle"


def test_training_flag_follows_the_allowlist():
    assert OpenCodeBackend("glm-5.3-flash-free").trains_on_data is False
    assert OpenCodeBackend("deepseek-v4.1-flash-free").trains_on_data is False
    assert OpenCodeBackend("big-pickle").trains_on_data is True
    assert all(isinstance(v, bool) for v in FREE_MODELS.values())


def test_error_event_becomes_backend_error():
    err = json.dumps({"type": "error", "error": {"type": "provider.no-route",
                                                 "message": "Model unavailable: x"}})
    with pytest.raises(BackendError, match="Model unavailable"):
        OpenCodeBackend(runner=Runner(err)).complete("s", MSGS)


def test_rate_limit_error_event():
    err = json.dumps({"type": "error", "error": {"message": "Rate limit reached"}})
    with pytest.raises(RateLimited):
        OpenCodeBackend(runner=Runner(err)).complete("s", MSGS)


def test_nonzero_exit_and_timeout_and_missing_exe():
    with pytest.raises(BackendError, match="code 1"):
        OpenCodeBackend(runner=Runner("", 1, "boom")).complete("s", MSGS)

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with pytest.raises(BackendError, match="timed out"):
        OpenCodeBackend(runner=timeout).complete("s", MSGS)

    def missing(cmd, **kw):
        raise FileNotFoundError

    with pytest.raises(BackendError, match="not found"):
        OpenCodeBackend(runner=missing).complete("s", MSGS)


def test_no_text_is_an_error():
    with pytest.raises(BackendError, match="no text"):
        OpenCodeBackend(runner=Runner(event("step_start"))).complete("s", MSGS)


def test_reported_cost_blocks_the_answer():
    with pytest.raises(PaidUsageBlocked):
        OpenCodeBackend(runner=Runner(ok_output(cost=0.02))).complete("s", MSGS)


def test_build_backend_selects_configured_opencode_model():
    settings = Settings(opencode_model="glm-5.3-flash-free")
    backend = build_backend("opencode", claude_model="c", codex_model="x",
                            timeout=17, settings=settings)
    assert isinstance(backend, OpenCodeBackend)
    assert backend.model == "glm-5.3-flash-free" and backend.timeout == 17
    with pytest.raises(ValueError, match="requires settings"):
        build_backend("opencode", claude_model="c", codex_model="x", timeout=17)


def test_private_request_skips_training_opencode_model():
    training = OpenCodeBackend("big-pickle", runner=Runner(ok_output("training")))
    safe_runner = Runner(ok_output("safe"))
    safe = OpenCodeBackend("glm-5.3-flash-free", runner=safe_runner)
    assert GatedRouter([training, safe]).complete("s", MSGS, private=True) == ("safe", "opencode")
    assert safe_runner.calls
    assert training._workdir is None
