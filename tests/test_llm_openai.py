import io
import json
import socket
import urllib.error
from email.message import Message as EmailMessage

import pytest

from igris.llm import BackendError, Message, RateLimited
from igris.llm_openai import (
    Budget,
    BudgetExceeded,
    OpenAIAPIBackend,
    PRICES,
    estimate_cost,
    get_secret,
)

KEY = "sk-test-SECRETKEY-12345"
DAY1 = 1_700_000_000.0  # 2023-11-14 UTC
DAY2 = DAY1 + 86400


class FakeKeyring:
    def __init__(self, store=None, raises=False):
        self.store = store or {}
        self.raises = raises
        self.calls = []

    def get_password(self, service, name):
        self.calls.append((service, name))
        if self.raises:
            raise RuntimeError("boom")
        return self.store.get((service, name))


class FakeResp:
    def __init__(self, payload):
        self.payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.requests = []
        self.timeouts = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        if self.exc is not None:
            raise self.exc
        return FakeResp(self.result)


def ok(content="hi", usage=None):
    d = {"choices": [{"message": {"content": content}}]}
    if usage is not None:
        d["usage"] = usage
    return d


def http_error(code, headers=None):
    h = EmailMessage()
    for k, v in (headers or {}).items():
        h[k] = v
    return urllib.error.HTTPError("http://x", code, "err", h, io.BytesIO(b"{}"))


def backend(opener, **kw):
    kw.setdefault("api_key", KEY)
    kw.setdefault("env", {"IGRIS_ALLOW_PAID": "1"})
    return OpenAIAPIBackend(opener=opener, **kw)


MSGS = [Message("user", "hello")]


# ---- get_secret ----

def test_secret_keyring_first_service_wins():
    kr = FakeKeyring({("igris", "N"): "a", ("openjarvis", "N"): "b"})
    assert get_secret("N", keyring_module=kr, env={"IGRIS_N": "c"}) == "a"


def test_secret_second_service():
    kr = FakeKeyring({("openjarvis", "N"): "b"})
    assert get_secret("N", keyring_module=kr, env={"IGRIS_N": "c"}) == "b"
    assert [c[0] for c in kr.calls] == ["igris", "openjarvis"]


def test_secret_env_fallback_and_empty_is_missing():
    kr = FakeKeyring({("igris", "N"): ""})
    assert get_secret("N", keyring_module=kr, env={"IGRIS_N": "c"}) == "c"
    assert get_secret("N", keyring_module=kr, env={"IGRIS_N": ""}) is None


def test_secret_keyring_error_counts_as_missing():
    kr = FakeKeyring(raises=True)
    assert get_secret("N", keyring_module=kr, env={"IGRIS_N": "c"}) == "c"


def test_secret_ignores_plain_openai_env():
    kr = FakeKeyring()
    env = {"OPENAI_API_KEY": "plain"}
    assert get_secret("OPENAI_API_KEY", keyring_module=kr, env=env) is None


def test_secret_default_keyring_import(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "keyring", None)  # import raises ImportError
    assert get_secret("N", env={"IGRIS_N": "e"}) == "e"


# ---- cost / budget ----

def test_estimate_cost():
    assert estimate_cost("gpt-4o", 1_000_000, 1_000_000) == pytest.approx(12.5)
    assert estimate_cost("gpt-4o-mini", 1_000_000, 0) == pytest.approx(0.15)
    assert estimate_cost("unknown", 1_000_000, 0) == pytest.approx(PRICES["gpt-4o"][0])


def test_budget_invalid():
    with pytest.raises(ValueError):
        Budget.__call__ if False else Budget("x.json", 0)


def test_budget_persist_and_reset(tmp_path):
    now = [DAY1]
    p = tmp_path / "a" / "b" / "budget.json"
    b = Budget(p, 1.0, clock=lambda: now[0])
    assert b.spent_today() == 0
    b.charge(0.25)
    b.charge(0.25)
    assert not p.with_name("budget.json.tmp").exists()
    data = json.loads(p.read_text())
    assert data["day"] == "2023-11-14" and data["spent"] == pytest.approx(0.5)
    assert Budget(p, 1.0, clock=lambda: now[0]).spent_today() == pytest.approx(0.5)
    assert b.remaining() == pytest.approx(0.5)
    now[0] = DAY2
    assert b.spent_today() == 0
    b.charge(0.1)
    assert json.loads(p.read_text())["day"] == "2023-11-15"


def test_budget_corrupt_file(tmp_path):
    p = tmp_path / "b.json"
    p.write_text("{not json")
    b = Budget(p, 1.0, clock=lambda: DAY1)
    assert b.spent_today() == 0
    b.charge(0.1)
    assert b.spent_today() == pytest.approx(0.1)


def test_budget_check(tmp_path):
    b = Budget(tmp_path / "b.json", 1.0, clock=lambda: DAY1)
    b.charge(0.9)
    b.check(0.1)
    with pytest.raises(BudgetExceeded):
        b.check(0.11)
    assert issubclass(BudgetExceeded, BackendError)


# ---- backend ----

def test_request_shape_and_result():
    op = FakeOpener(ok("answer"))
    be = backend(op, timeout=7, max_tokens=50, temperature=0.2)
    msgs = [Message("user", "u"), Message("assistant", "a"), Message("tool", "t")]
    assert be.complete("sys", msgs) == "answer"
    req = op.requests[0]
    assert req.full_url == "https://api.openai.com/v1/chat/completions"
    assert req.get_header("Authorization") == "Bearer " + KEY
    assert req.get_header("Content-type") == "application/json"
    assert op.timeouts == [7]
    body = json.loads(req.data)
    assert body["model"] == "gpt-4o"
    assert body["max_tokens"] == 50 and body["temperature"] == 0.2
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "Tool result: t"},
    ]
    assert be.name == "openai"


def test_key_from_secret_lookup(monkeypatch):
    import igris.llm_openai as m

    monkeypatch.setattr(m, "get_secret", lambda name, **kw: KEY if name == "OPENAI_API_KEY" else None)
    op = FakeOpener(ok())
    OpenAIAPIBackend(opener=op, env={"IGRIS_ALLOW_PAID": "1"}).complete("s", MSGS)
    assert op.requests[0].get_header("Authorization") == "Bearer " + KEY


def test_missing_key(monkeypatch):
    import igris.llm_openai as m

    monkeypatch.setattr(m, "get_secret", lambda name, **kw: None)
    op = FakeOpener(ok())
    with pytest.raises(BackendError) as ei:
        OpenAIAPIBackend(opener=op, env={"IGRIS_ALLOW_PAID": "1"}).complete("s", MSGS)
    assert str(ei.value) == (
        "OpenAI API key not found (keyring service 'igris' or 'openjarvis', "
        "name OPENAI_API_KEY)"
    )
    assert op.requests == []


def test_charges_actual_usage(tmp_path):
    b = Budget(tmp_path / "b.json", 10.0, clock=lambda: DAY1)
    op = FakeOpener(ok(usage={"prompt_tokens": 1000, "completion_tokens": 500}))
    backend(op, budget=b).complete("s", MSGS)
    assert b.spent_today() == pytest.approx(estimate_cost("gpt-4o", 1000, 500))


def test_charges_estimate_without_usage(tmp_path):
    b = Budget(tmp_path / "b.json", 10.0, clock=lambda: DAY1)
    op = FakeOpener(ok())
    backend(op, budget=b, max_tokens=100).complete("x" * 40, [Message("user", "y" * 40)])
    assert b.spent_today() == pytest.approx(estimate_cost("gpt-4o", 20, 100))


def test_downgrade(tmp_path):
    b = Budget(tmp_path / "b.json", 1.0, clock=lambda: DAY1)
    op = FakeOpener(ok())
    be = backend(op, budget=b)
    b.charge(0.79)
    be.complete("s", MSGS)
    assert json.loads(op.requests[0].data)["model"] == "gpt-4o"
    b.charge(0.02 - 0.0)  # now >= 0.8
    be.complete("s", MSGS)
    assert json.loads(op.requests[1].data)["model"] == "gpt-4o-mini"


def test_downgrade_resets_next_day(tmp_path):
    now = [DAY1]
    b = Budget(tmp_path / "b.json", 1.0, clock=lambda: now[0])
    b.charge(0.9)
    op = FakeOpener(ok())
    be = backend(op, budget=b)
    be.complete("s", MSGS)
    now[0] = DAY2
    be.complete("s", MSGS)
    assert json.loads(op.requests[0].data)["model"] == "gpt-4o-mini"
    assert json.loads(op.requests[1].data)["model"] == "gpt-4o"


def test_pre_call_refusal(tmp_path):
    b = Budget(tmp_path / "b.json", 0.001, clock=lambda: DAY1)
    b.charge(0.001)
    op = FakeOpener(ok())
    with pytest.raises(BudgetExceeded):
        backend(op, budget=b).complete("s", MSGS)
    assert op.requests == []


def test_rate_limited_with_retry_after():
    op = FakeOpener(exc=http_error(429, {"Retry-After": "12"}))
    with pytest.raises(RateLimited) as ei:
        backend(op).complete("s", MSGS)
    assert ei.value.retry_after == 12.0


def test_rate_limited_without_retry_after():
    op = FakeOpener(exc=http_error(429))
    with pytest.raises(RateLimited) as ei:
        backend(op).complete("s", MSGS)
    assert ei.value.retry_after is None


@pytest.mark.parametrize("code", [401, 403])
def test_key_rejected(code):
    op = FakeOpener(exc=http_error(code))
    with pytest.raises(BackendError) as ei:
        backend(op).complete("s", MSGS)
    assert str(ei.value) == f"OpenAI API key was rejected (HTTP {code})"
    assert KEY not in str(ei.value)


@pytest.mark.parametrize(
    "exc",
    [
        http_error(500),
        urllib.error.URLError("dns " + KEY),
        socket.timeout("t " + KEY),
        TimeoutError(KEY),
        ConnectionResetError(KEY),
    ],
)
def test_other_errors_hide_key(exc, tmp_path):
    b = Budget(tmp_path / "b.json", 1.0, clock=lambda: DAY1)
    with pytest.raises(BackendError) as ei:
        backend(FakeOpener(exc=exc), budget=b).complete("s", MSGS)
    assert KEY not in str(ei.value)
    assert not isinstance(ei.value, RateLimited)
    assert b.spent_today() == 0


@pytest.mark.parametrize(
    "payload",
    [b"not json", b"{}", {"choices": []}, {"choices": [{"message": {"content": None}}]}],
)
def test_malformed_response(payload, tmp_path):
    b = Budget(tmp_path / "b.json", 1.0, clock=lambda: DAY1)
    with pytest.raises(BackendError) as ei:
        backend(FakeOpener(payload), budget=b).complete("s", MSGS)
    assert KEY not in str(ei.value)
    assert b.spent_today() == 0


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes", " 1"])
def test_paid_usage_requires_explicit_opt_in(monkeypatch, value):
    monkeypatch.setenv("IGRIS_ALLOW_PAID", "1")
    env = {} if value is None else {"IGRIS_ALLOW_PAID": value}
    op = FakeOpener(ok())
    with pytest.raises(BackendError, match="IGRIS_ALLOW_PAID=1"):
        backend(op, env=env).complete("s", MSGS)
    assert op.requests == []


def test_paid_guard_defaults_to_process_environment_and_rechecks(monkeypatch):
    monkeypatch.delenv("IGRIS_ALLOW_PAID", raising=False)
    op = FakeOpener(ok())
    be = OpenAIAPIBackend(opener=op, api_key=KEY)
    with pytest.raises(BackendError, match="IGRIS_ALLOW_PAID=1"):
        be.complete("s", MSGS)
    assert op.requests == []
    monkeypatch.setenv("IGRIS_ALLOW_PAID", "1")
    assert be.complete("s", MSGS) == "hi"
    monkeypatch.setenv("IGRIS_ALLOW_PAID", "0")
    with pytest.raises(BackendError, match="IGRIS_ALLOW_PAID=1"):
        be.complete("s", MSGS)
    assert len(op.requests) == 1


def test_secret_lookup_receives_injected_environment(monkeypatch):
    import igris.llm_openai as module

    env = {"IGRIS_ALLOW_PAID": "1", "IGRIS_OPENAI_API_KEY": KEY}
    seen = []

    def lookup(name, **kwargs):
        seen.append(kwargs.get("env"))
        return KEY

    monkeypatch.setattr(module, "get_secret", lookup)
    OpenAIAPIBackend(opener=FakeOpener(ok()), env=env).complete("s", MSGS)
    assert seen == [env]
