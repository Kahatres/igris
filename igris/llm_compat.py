"""One backend for every provider that exposes an OpenAI-compatible /chat/completions.

Providers are plain data (`Provider`) in the `PROVIDERS` registry. Free tiers are
protected by `DailyQuota`, which refuses locally once a provider's daily request
allowance is spent, so a misconfigured loop cannot run into a paid overage.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from igris.llm import BackendError, Message, RateLimited
from igris.llm_extra import _THINK_RE, post_json, retry_after
from igris.llm_openai import get_secret

# Lower-case fragments of a 402/403 body that mean "free allowance used up".
_QUOTA_MARKERS = ("quota", "credit", "limit", "exceeded", "insufficient", "balance")


@dataclass(frozen=True)
class Provider:
    """Static description of an OpenAI-compatible provider."""

    name: str
    base_url: str
    key_name: str
    default_model: str
    trains_on_data: bool = False
    extra_headers: dict = field(default_factory=dict, hash=False)
    requests_per_day: int | None = None
    # credits that expire; never part of a default backend order
    trial: bool = False
    # secrets substituted into base_url, e.g. "{CLOUDFLARE_ACCOUNT_ID}"
    url_secrets: tuple[str, ...] = ()
    # only model ids with this suffix may be used; the default is discovered daily
    free_suffix: str | None = None


# Each entry cites the docs page it was checked against. A provider whose data
# terms could not be confirmed is marked trains_on_data=True so the privacy gate
# keeps private content away from it.
PROVIDERS: dict[str, Provider] = {p.name: p for p in (
    # openrouter.ai/docs/api-reference/limits, checked 2026-10-30: :free models allow
    # 20 req/min and 50 req/day (1,000/day after buying 10 credits); a negative
    # balance returns 402 even for free models. Free endpoints may train on prompts
    # depending on the account privacy setting (docs/guides/privacy/provider-logging).
    # The free catalogue rotates, so the model is discovered from /models.
    Provider("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "",
             trains_on_data=True, requests_per_day=50, free_suffix=":free",
             extra_headers={"X-Title": "Igris"}),
    # console.groq.com/docs/openai and /docs/rate-limits, checked 2026-10-30: limits
    # are per model and reported in x-ratelimit-* headers; inference data is not
    # retained by default (/docs/your-data).
    Provider("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
             "openai/gpt-oss-120b"),
    # inference-docs.cerebras.ai/support/rate-limits, checked 2026-10-30: a free
    # trial of $5 credits expiring after 30 days, not a renewing free tier.
    # Data terms not confirmed.
    Provider("cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY", "gpt-oss-120b",
             trains_on_data=True, trial=True),
    # docs.mistral.ai/admin/user-management-finops/tier and help.mistral.ai article
    # 455207, checked 2026-10-30: Free mode has a monthly allowance; free-mode input
    # and output may be used for training unless opted out in the admin panel.
    # The -latest alias is confirmed by the owner's live call.
    Provider("mistral", "https://api.mistral.ai/v1", "MISTRAL_API_KEY",
             "mistral-small-latest", trains_on_data=True),
    # docs.api.nvidia.com/nim/reference/llm-apis, checked 2026-10-30: base URL only.
    # Free-tier limits (about 40 req/min) are reported by third parties, not NVIDIA;
    # data terms not confirmed. gpt-oss-120b was retired on 2026-09-03 (HTTP 410);
    # mistral-large-4 answered the owner's key on 2026-11-01.
    Provider("nvidia", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY",
             "mistralai/mistral-large-4-1t-a52b", trains_on_data=True),
    # developers.cloudflare.com/workers-ai/platform/pricing and
    # /configuration/open-ai-compatibility, checked 2026-10-30: 10,000 Neurons/day
    # free, reset 00:00 UTC, requests fail (not billed) beyond it on the Free plan.
    # Data terms not confirmed.
    Provider("cloudflare",
             "https://api.cloudflare.com/client/v4/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/v1",
             "CLOUDFLARE_API_TOKEN", "@cf/openai/gpt-oss-120b", trains_on_data=True,
             url_secrets=("CLOUDFLARE_ACCOUNT_ID",)),
    # docs.sambanova.ai/docs/en/models/rate-limits, checked 2026-10-30: without a
    # payment method, 20 req/min and 20 req/day per model. Base URL and data terms
    # not confirmed on the docs page; the owner's live call confirms the URL.
    Provider("sambanova", "https://api.sambanova.ai/v1", "SAMBANOVA_API_KEY",
             "Meta-Llama-3.3-70B-Instruct", trains_on_data=True, requests_per_day=20),
    # docs.cohere.com/docs/rate-limits and /docs/compatibility-api, checked 2026-10-30:
    # trial keys are free, 20 req/min and 1,000 calls/month (about 30/day here),
    # evaluation only. Data terms not confirmed.
    Provider("cohere", "https://api.cohere.ai/compatibility/v1", "COHERE_API_KEY",
             "command-a-plus-05-2026", trains_on_data=True, requests_per_day=30,
             trial=True),
    # Hugging Face is deliberately absent: huggingface.co/docs/inference-providers/
    # pricing (checked 2026-10-30) gives free accounts no credits, so every routed
    # call would need purchased credits.
)}


def provider_secrets(get=None) -> dict[str, str]:
    """Stored keys of the free providers (and Gemini), by secret name, for a child process.

    Only free-provider names are looked up: OPENAI_API_KEY would make the Codex CLI bill
    an API account, so it is never handed on."""
    get = get or get_secret
    names = ["GEMINI_API_KEY", "EVREN_API_KEY"]  # EVREN is a multiagent worker, not an Igris backend
    for p in PROVIDERS.values():
        names += [p.key_name, *p.url_secrets]
    found = {}
    for name in dict.fromkeys(names):
        value = get(name)
        if value:
            found[name] = value
    return found


def _utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _seconds_to_midnight(ts: float) -> float:
    now = datetime.fromtimestamp(ts, timezone.utc)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max((midnight - now).total_seconds(), 1.0)


def _duration(text: str | None) -> float | None:
    """Parse "30s", "1m26.4s", "2h", "250ms" or a plain number of seconds."""
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    parts = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m(?!s))?(?:([\d.]+)s)?(?:([\d.]+)ms)?", text)
    if not parts or not any(parts.groups()):
        return None
    h, m, sec, ms = (float(g) if g else 0.0 for g in parts.groups())
    return h * 3600 + m * 60 + sec + ms / 1000


class DailyQuota:
    """Per-provider request counter in one JSON file, reset at UTC midnight.

    Every attempt counts, including failed ones, because some providers
    (OpenRouter) count failed requests against the free allowance too.
    """

    def __init__(self, path: Path, limits: dict[str, int], *, clock=time.time):
        self.path = Path(path)
        self.limits = dict(limits)
        self.clock = clock
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("day") != _utc_day(self.clock()):
                return {}
            return {str(k): (v if str(k).startswith("str:") else int(v))
                    for k, v in data.get("used", {}).items()}
        except Exception:
            return {}

    def used(self, name: str) -> int:
        return int(self._load().get(name, 0))

    def remaining(self, name: str) -> int | None:
        limit = self.limits.get(name)
        return None if limit is None else max(limit - self.used(name), 0)

    def check(self, name: str) -> None:
        if self._load().get(name + ":exhausted") or self.remaining(name) == 0:
            raise RateLimited(name, _seconds_to_midnight(self.clock()),
                              "daily quota used up")

    def spend(self, name: str) -> None:
        self._update(name, lambda used: int(used.get(name, 0)) + 1)

    def exhaust(self, name: str) -> None:
        """Block the provider until UTC midnight (it refused for quota or credit)."""
        self._update(name + ":exhausted", lambda used: 1)

    def remember(self, key: str, value: str) -> None:
        """Keep a small string until UTC midnight (for example a discovered model)."""
        self._update("str:" + key, lambda used: value)

    def recall(self, key: str) -> str | None:
        value = self._load().get("str:" + key)
        return value if isinstance(value, str) else None

    def _update(self, key: str, change) -> None:
        with self._lock:
            used = self._load()
            used[key] = change(used)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"day": _utc_day(self.clock()), "used": used}),
                           encoding="utf-8")
            os.replace(tmp, self.path)


def _error_text(exc) -> str:
    try:
        raw = exc.read()
    except Exception:
        return ""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    return str(raw)[:2000].lower()


class CompatBackend:
    """Chat completions against any `Provider`; error text never carries the key."""

    def __init__(self, provider: Provider, model: str | None = None, *,
                 api_key: str | None = None, opener=None, timeout: int = 60,
                 quota: DailyQuota | None = None, max_tokens: int = 1024,
                 temperature: float = 0.7, clock=time.monotonic):
        self.provider = provider
        self.name = provider.name
        self.model = model or provider.default_model
        self._api_key = api_key
        self._opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self.quota = quota
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.last_headers: dict = {}
        self._free_model: str | None = None
        self._rest_until = 0.0
        self._clock = clock

    @property
    def trains_on_data(self) -> bool:
        return self.provider.trains_on_data

    def _key(self) -> str:
        key = self._api_key or get_secret(self.provider.key_name)
        if not key:
            raise BackendError(
                f"{self.name} API key not configured (keyring service 'igris', name "
                f"{self.provider.key_name}, or IGRIS_{self.provider.key_name})")
        return key

    def _base_url(self) -> str:
        url = self.provider.base_url.rstrip("/")
        for name in self.provider.url_secrets:
            value = get_secret(name)
            if not value:
                raise BackendError(f"{self.name} needs {name} (keyring service 'igris' "
                                   f"or IGRIS_{name})")
            url = url.replace("{" + name + "}", value)
        return url

    def _model(self, key: str, base: str) -> str:
        suffix = self.provider.free_suffix
        if not suffix:
            return self.model
        if self.model:
            if not self.model.endswith(suffix):
                raise BackendError(f"{self.name} model must end with {suffix} (free only)")
            return self.model
        cached = self._free_model or (self.quota.recall(self.name + ":free_model")
                                      if self.quota is not None else None)
        if cached:
            self._free_model = cached
            return cached
        data = post_json(self.name, self._opener, base + "/models",
                          {"Authorization": "Bearer " + key}, None, self.timeout,
                          http_error=self._http_error)
        items = data.get("data") if isinstance(data.get("data"), list) else []
        free = [m["id"] for m in items if isinstance(m, dict)
                and isinstance(m.get("id"), str) and m["id"].endswith(suffix)]
        if not free:
            raise BackendError(f"{self.name} lists no free models right now")
        self._free_model = free[0]
        if self.quota is not None:
            self.quota.remember(self.name + ":free_model", free[0])
        return free[0]

    def limits(self) -> dict:
        """Rate-limit counters from the last response headers (Groq style)."""
        out = {}
        for key in ("limit_requests", "remaining_requests", "reset_requests",
                    "limit_tokens", "remaining_tokens", "reset_tokens"):
            value = self.last_headers.get("x-ratelimit-" + key.replace("_", "-"))
            if value is not None:
                out[key] = int(value) if value.isdigit() else value
        return out

    def _http_error(self, exc) -> None:
        code = exc.code
        if code == 429:
            raise RateLimited(self.name, retry_after(exc)) from None
        if code in (402, 403):
            text = _error_text(exc)
            if code == 402 or any(marker in text for marker in _QUOTA_MARKERS):
                if self.quota is not None:
                    # never keep sending to an account that refused for money or quota
                    self.quota.exhaust(self.name)
                raise RateLimited(self.name, retry_after(exc),
                                  f"quota or credit exhausted (HTTP {code})") from None
            raise BackendError(f"{self.name} denied access (HTTP 403)") from None
        if code == 401:
            raise BackendError(f"{self.name} rejected the API key: invalid key (HTTP 401)") \
                from None
        if code >= 500:
            raise BackendError(f"{self.name} server error (HTTP {code})") from None

    def _remember_headers(self, headers) -> None:
        try:
            self.last_headers = {str(k).lower(): str(v) for k, v in headers.items()}
        except Exception:
            self.last_headers = {}
        if self.last_headers.get("x-ratelimit-remaining-requests") == "0":
            wait = _duration(self.last_headers.get("x-ratelimit-reset-requests"))
            self._rest_until = self._clock() + (wait if wait is not None else 60.0)

    def complete(self, system: str, messages: list[Message]) -> str:
        if self.quota is not None:
            self.quota.check(self.name)
        if self._clock() < self._rest_until:
            raise RateLimited(self.name, round(self._rest_until - self._clock(), 3),
                              "provider reported no requests left")
        key = self._key()
        base = self._base_url()
        model = self._model(key, base)
        msgs = [{"role": "system", "content": system}]
        for m in messages:
            if m.role == "tool":
                msgs.append({"role": "user", "content": "Tool result: " + m.content})
            else:
                msgs.append({"role": m.role, "content": m.content})
        payload = {"model": model, "messages": msgs, "max_tokens": self.max_tokens,
                   "temperature": self.temperature, "stream": False}
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
        headers.update(self.provider.extra_headers)
        if self.quota is not None:
            self.quota.spend(self.name)
        data = post_json(self.name, self._opener, base + "/chat/completions",
                          headers, payload, self.timeout,
                          http_error=self._http_error, on_headers=self._remember_headers)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise BackendError(f"{self.name} returned a malformed response") from None
        if not isinstance(content, str) or not content.strip():
            raise BackendError(f"{self.name} returned an empty response")
        return _THINK_RE.sub("", content, count=1).strip()


def build_compat(name: str, settings, *, timeout: int = 60, opener=None) -> CompatBackend:
    """Backend for a registered provider, with its model override and shared quota."""
    provider = PROVIDERS[name]
    limits = {p.name: p.requests_per_day for p in PROVIDERS.values()
              if p.requests_per_day is not None}
    quota = DailyQuota(Path(settings.data_dir) / "quota.json", limits)
    model = dict(getattr(settings, "compat_models", {}) or {}).get(name)
    return CompatBackend(provider, model, timeout=timeout, opener=opener, quota=quota)
