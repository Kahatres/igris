"""OpenAI API backend with keyring-based secret lookup and a daily USD budget."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from igris.llm import BackendError, Message, RateLimited, allow_paid

# USD per 1M tokens (input, output).
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}


class BudgetExceeded(BackendError):
    """Raised when a call would exceed the daily budget."""


def get_secret(name, *, services=("igris", "openjarvis"), env=None, keyring_module=None):
    """Look up a secret in keyring (each service in order), then IGRIS_<name> env.

    The plain OPENAI_API_KEY variable is deliberately not consulted: the Codex
    CLI would bill that key instead of the ChatGPT subscription.
    """
    kr = keyring_module
    if kr is None:
        try:
            import keyring as kr  # type: ignore
        except Exception:
            kr = None
    if kr is not None:
        for service in services:
            try:
                value = kr.get_password(service, name)
            except Exception:
                value = None
            if value:
                return value
    environ = os.environ if env is None else env
    value = environ.get("IGRIS_" + name)
    return value or None


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = PRICES.get(model, PRICES["gpt-4o"])
    return (max(input_tokens, 0) * pin + max(output_tokens, 0) * pout) / 1_000_000


class Budget:
    def __init__(self, path: Path, daily_usd: float, clock=time.time):
        if not daily_usd > 0:
            raise ValueError("daily_usd must be > 0")
        self.path = Path(path)
        self.daily_usd = float(daily_usd)
        self.clock = clock

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")

    def spent_today(self) -> float:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("day") != self._today():
                return 0.0
            spent = float(data.get("spent", 0.0))
            return spent if spent > 0 else 0.0
        except Exception:
            return 0.0

    def remaining(self) -> float:
        return max(self.daily_usd - self.spent_today(), 0.0)

    def check(self, estimate_usd: float = 0.0) -> None:
        spent = self.spent_today()
        if spent + estimate_usd > self.daily_usd:
            raise BudgetExceeded(
                f"Daily OpenAI budget exceeded (spent ${spent:.4f} of ${self.daily_usd:.2f})"
            )

    def charge(self, usd: float) -> None:
        total = self.spent_today() + max(float(usd), 0.0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"day": self._today(), "spent": total}), encoding="utf-8")
        os.replace(tmp, self.path)


class OpenAIAPIBackend:
    name = "openai"

    def __init__(
        self,
        model: str = "gpt-4o",
        *,
        budget: Budget | None = None,
        max_tokens: int = 300,
        temperature: float = 0.7,
        timeout: int = 60,
        api_key: str | None = None,
        opener=urllib.request.urlopen,
        base_url: str = "https://api.openai.com/v1",
        downgrade_model: str = "gpt-4o-mini",
        downgrade_at: float = 0.8,
        env=None,
    ):
        self.model = model
        self.budget = budget
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self._api_key = api_key
        self.opener = opener
        self.base_url = base_url.rstrip("/")
        self.downgrade_model = downgrade_model
        self.downgrade_at = downgrade_at
        self.env = os.environ if env is None else env

    def _key(self) -> str:
        key = self._api_key or get_secret("OPENAI_API_KEY", env=self.env)
        if not key:
            raise BackendError(
                "OpenAI API key not found (keyring service 'igris' or 'openjarvis', "
                "name OPENAI_API_KEY)"
            )
        return key

    def complete(self, system: str, messages: list[Message]) -> str:
        if not allow_paid(self.env):
            raise BackendError("OpenAI API paid usage is disabled; set IGRIS_ALLOW_PAID=1 to allow it")
        key = self._key()
        model = self.model
        if self.budget is not None and (
            self.budget.spent_today() >= self.downgrade_at * self.budget.daily_usd
        ):
            model = self.downgrade_model

        msgs = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "tool":
                msgs.append({"role": "user", "content": "Tool result: " + m.content})
            else:
                msgs.append({"role": m.role, "content": m.content})

        chars = len(system) + sum(len(m.content) for m in messages)
        estimate = estimate_cost(model, chars // 4, self.max_tokens)
        if self.budget is not None:
            self.budget.check(estimate)

        body = json.dumps(
            {
                "model": model,
                "messages": msgs,
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code == 429:
                retry = None
                try:
                    retry = float(exc.headers.get("Retry-After"))
                except Exception:
                    retry = None
                raise RateLimited("openai", retry) from None
            if code in (401, 403):
                raise BackendError(f"OpenAI API key was rejected (HTTP {code})") from None
            raise BackendError(f"OpenAI API error (HTTP {code})") from None
        except urllib.error.URLError as exc:
            raise BackendError(
                f"OpenAI API unreachable ({type(exc.reason).__name__})"
            ) from None
        except (socket.timeout, TimeoutError):
            raise BackendError("OpenAI API request timed out") from None
        except OSError as exc:
            raise BackendError(f"OpenAI API connection error ({type(exc).__name__})") from None

        try:
            data = json.loads(raw)
            content = data["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content")
        except Exception:
            raise BackendError("OpenAI API returned a malformed response") from None

        if self.budget is not None:
            cost = estimate
            try:
                usage = data.get("usage")
                if isinstance(usage, dict):
                    cost = estimate_cost(
                        model,
                        int(usage["prompt_tokens"]),
                        int(usage["completion_tokens"]),
                    )
            except Exception:
                cost = estimate
            self.budget.charge(cost)
        return content
