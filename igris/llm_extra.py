"""Extra LLM backends: Google Gemini and local Ollama (standard library only)."""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request

from igris.llm import BackendError, Message, RateLimited

_THINK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)


def retry_after(exc: urllib.error.HTTPError) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        return None
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_refused(exc: BaseException | None) -> bool:
    return isinstance(exc, ConnectionRefusedError)


def post_json(name, opener, url, headers, payload, timeout, refused_msg=None, *,
               http_error=None, on_headers=None):
    """POST JSON and return the decoded object; map all failures to backend errors.

    http_error(exc) may raise a more specific error for an HTTP status; if it
    returns, the default mapping applies. on_headers(headers) sees the headers
    of a successful response (for example rate-limit counters).
    """
    # payload None sends a GET (for example a model listing)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="GET" if payload is None else "POST")
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
            if on_headers is not None:
                on_headers(getattr(resp, "headers", None) or {})
    except urllib.error.HTTPError as exc:
        if http_error is not None:
            http_error(exc)
        if exc.code == 429:
            raise RateLimited(name, retry_after(exc)) from None
        raise BackendError(f"{name} request failed: HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if _is_refused(reason) and refused_msg:
            raise BackendError(refused_msg) from None
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise BackendError(f"{name} request timed out") from None
        raise BackendError(
            f"{name} network error: {type(reason).__name__}"
        ) from None
    except (socket.timeout, TimeoutError):
        raise BackendError(f"{name} request timed out") from None
    except ConnectionRefusedError:
        raise BackendError(refused_msg or f"{name} connection refused") from None
    except OSError as exc:
        raise BackendError(f"{name} network error: {type(exc).__name__}") from None
    try:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BackendError(f"{name} returned invalid JSON") from None
    if not isinstance(data, dict):
        raise BackendError(f"{name} returned an unexpected response")
    return data


class GeminiBackend:
    name = "gemini"
    # Free-tier content is used to improve Google products and may be read by
    # human reviewers (ai.google.dev/gemini-api/terms, checked 2026-10-30).
    trains_on_data = True

    # gemini-2.0-flash is shut down; current stable Flash per
    # ai.google.dev/gemini-api/docs/models (checked 2026-10-30), free tier available.
    def __init__(self, model: str = "gemini-3.8-flash", *, api_key: str | None = None,
                 timeout: int = 60, opener=urllib.request.urlopen,
                 max_tokens: int = 300, temperature: float = 0.7):
        self.model = model
        self._api_key = api_key
        self.timeout = timeout
        self._opener = opener
        self.max_tokens = max_tokens
        self.temperature = temperature

    def _key(self) -> str:
        if self._api_key:
            return self._api_key
        from igris.llm_openai import get_secret
        key = get_secret("GEMINI_API_KEY")
        if not key:
            raise BackendError("Gemini API key not configured (GEMINI_API_KEY)")
        return key

    def complete(self, system: str, messages: list[Message]) -> str:
        key = self._key()
        contents = []
        for m in messages:
            if m.role == "assistant":
                role, text = "model", m.content
            elif m.role == "tool":
                role, text = "user", "Tool result: " + m.content
            else:
                role, text = "user", m.content
            contents.append({"role": role, "parts": [{"text": text}]})
        payload = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent")
        headers = {"Content-Type": "application/json", "x-goog-api-key": key}
        data = post_json(self.name, self._opener, url, headers, payload, self.timeout)
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise BackendError(f"Gemini blocked the prompt: {feedback['blockReason']}")
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise BackendError("Gemini returned no candidates")
        try:
            parts = candidates[0]["content"]["parts"]
            texts = [p["text"] for p in parts if isinstance(p, dict) and "text" in p]
        except (KeyError, TypeError):
            raise BackendError("Gemini returned a malformed candidate") from None
        if not texts:
            raise BackendError("Gemini returned an empty response")
        return "".join(str(t) for t in texts)


class OllamaBackend:
    name = "ollama"

    def __init__(self, model: str = "qwen3:4b", *, base_url: str = "http://localhost:11434",
                 timeout: int = 120, opener=urllib.request.urlopen):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener

    def complete(self, system: str, messages: list[Message]) -> str:
        msgs = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "tool":
                msgs.append({"role": "user", "content": "Tool result: " + m.content})
            elif m.role == "assistant":
                msgs.append({"role": "assistant", "content": m.content})
            else:
                msgs.append({"role": "user", "content": m.content})
        payload = {"model": self.model, "messages": msgs, "stream": False}
        data = post_json(
            self.name, self._opener, f"{self.base_url}/api/chat",
            {"Content-Type": "application/json"}, payload, self.timeout,
            refused_msg=f"Ollama is not running at {self.base_url}",
        )
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise BackendError("Ollama returned no message content")
        return _THINK_RE.sub("", content, count=1).strip()
