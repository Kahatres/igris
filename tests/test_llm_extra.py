import io
import json
import socket
import sys
import types
import urllib.error

import pytest

from igris.llm import BackendError, Message, RateLimited
from igris.llm_extra import GeminiBackend, OllamaBackend

KEY = "SECRET-KEY-123"


class FakeResp:
    def __init__(self, body):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, req, timeout=None):
        self.calls.append((req, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return FakeResp(self.result)


def http_error(code, headers=None):
    return urllib.error.HTTPError("http://x/?k=" + KEY, code, "msg", headers or {}, io.BytesIO(b"body"))


MSGS = [Message("user", "hi"), Message("assistant", "yo"), Message("tool", "42")]


def gem(result, **kw):
    op = Opener(result)
    return GeminiBackend(api_key=KEY, opener=op, **kw), op


def test_gemini_request_shape_and_parse():
    b, op = gem({"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]},
                max_tokens=10, temperature=0.1)
    assert b.complete("sys", MSGS) == "ab"
    req, timeout = op.calls[0]
    assert timeout == 60
    assert req.full_url == ("https://generativelanguage.googleapis.com/v1beta/models/"
                            "gemini-3.8-flash:generateContent")
    assert KEY not in req.full_url
    assert req.get_method() == "POST"
    assert req.get_header("X-goog-api-key") == KEY
    body = json.loads(req.data)
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "yo"}]},
        {"role": "user", "parts": [{"text": "Tool result: 42"}]},
    ]
    assert body["generationConfig"] == {"maxOutputTokens": 10, "temperature": 0.1}


def test_gemini_key_from_get_secret(monkeypatch):
    seen = []
    fake = types.ModuleType("igris.llm_openai")
    fake.get_secret = lambda name, **kw: seen.append(name) or KEY
    monkeypatch.setitem(sys.modules, "igris.llm_openai", fake)
    op = Opener({"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    assert GeminiBackend(opener=op).complete("s", MSGS) == "ok"
    assert seen == ["GEMINI_API_KEY"]
    assert op.calls[0][0].get_header("X-goog-api-key") == KEY


def test_gemini_missing_key(monkeypatch):
    fake = types.ModuleType("igris.llm_openai")
    fake.get_secret = lambda name, **kw: None
    monkeypatch.setitem(sys.modules, "igris.llm_openai", fake)
    op = Opener({})
    with pytest.raises(BackendError):
        GeminiBackend(opener=op).complete("s", MSGS)
    assert op.calls == []


@pytest.mark.parametrize("data", [
    {}, {"candidates": []},
    {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": [{"content": {"parts": [{"text": "x"}]}}]},
    {"candidates": [{}]}, {"candidates": [{"content": {"parts": []}}]},
    [1, 2],
])
def test_gemini_bad_responses(data):
    b, _ = gem(data)
    with pytest.raises(BackendError):
        b.complete("s", MSGS)


def test_gemini_rate_limited_with_retry_after():
    b, _ = gem(http_error(429, {"Retry-After": "7"}))
    with pytest.raises(RateLimited) as ei:
        b.complete("s", MSGS)
    assert ei.value.retry_after == 7.0


def test_gemini_rate_limited_without_retry_after():
    b, _ = gem(http_error(429))
    with pytest.raises(RateLimited) as ei:
        b.complete("s", MSGS)
    assert ei.value.retry_after is None


def test_gemini_rate_limited_bad_retry_after():
    b, _ = gem(http_error(429, {"Retry-After": "soon"}))
    with pytest.raises(RateLimited) as ei:
        b.complete("s", MSGS)
    assert ei.value.retry_after is None


@pytest.mark.parametrize("err", [
    http_error(500),
    urllib.error.URLError("boom " + KEY),
    urllib.error.URLError(socket.timeout()),
    socket.timeout(),
    TimeoutError(),
    OSError("x " + KEY),
    ConnectionRefusedError(),
])
def test_gemini_errors_hide_key(err):
    b, _ = gem(err)
    with pytest.raises(BackendError) as ei:
        b.complete("s", MSGS)
    assert KEY not in str(ei.value)


def test_gemini_invalid_json():
    b, _ = gem(b"not json")
    with pytest.raises(BackendError) as ei:
        b.complete("s", MSGS)
    assert KEY not in str(ei.value)


def ollama(result, **kw):
    op = Opener(result)
    return OllamaBackend(opener=op, **kw), op


def test_ollama_request_and_parse():
    b, op = ollama({"message": {"content": "hello"}}, base_url="http://h:1/")
    assert b.complete("sys", MSGS) == "hello"
    req, timeout = op.calls[0]
    assert timeout == 120
    assert req.full_url == "http://h:1/api/chat"
    body = json.loads(req.data)
    assert body["model"] == "qwen3:4b"
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo"},
        {"role": "user", "content": "Tool result: 42"},
    ]


def test_ollama_strips_think_block():
    b, _ = ollama({"message": {"content": "<think>\nreasoning\n</think>\n\nAnswer <think>x</think>"}})
    assert b.complete("s", MSGS) == "Answer <think>x</think>"


def test_ollama_plain_content_unchanged():
    b, _ = ollama({"message": {"content": "plain"}})
    assert b.complete("s", MSGS) == "plain"


@pytest.mark.parametrize("err", [
    urllib.error.URLError(ConnectionRefusedError()),
    ConnectionRefusedError(),
])
def test_ollama_not_running(err):
    b, _ = ollama(err)
    with pytest.raises(BackendError, match="Ollama is not running at http://localhost:11434"):
        b.complete("s", MSGS)


def test_ollama_rate_limited():
    b, _ = ollama(http_error(429, {"Retry-After": "2.5"}))
    with pytest.raises(RateLimited) as ei:
        b.complete("s", MSGS)
    assert ei.value.retry_after == 2.5


@pytest.mark.parametrize("err", [
    http_error(404),
    urllib.error.URLError("dns"),
    urllib.error.URLError(socket.timeout()),
    socket.timeout(),
    OSError("x"),
])
def test_ollama_other_errors(err):
    b, _ = ollama(err)
    with pytest.raises(BackendError):
        b.complete("s", MSGS)


@pytest.mark.parametrize("data", [b"nope", {}, {"message": {}}, {"message": {"content": 5}}])
def test_ollama_bad_responses(data):
    b, _ = ollama(data)
    with pytest.raises(BackendError):
        b.complete("s", MSGS)


def test_names():
    assert GeminiBackend.name == "gemini"
    assert OllamaBackend.name == "ollama"
