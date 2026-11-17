"""OpenAI-compatible backend tests with a fake opener; no network access."""

import io
import json
import urllib.error
from datetime import datetime, timezone

import pytest

from igris.llm import BackendError, Message, RateLimited
from igris.llm_compat import PROVIDERS, CompatBackend, DailyQuota, Provider

KEY = "sk-secret-test-key-1234567890"
FAKE = Provider(name="fake", base_url="https://api.fake.test/v1/", key_name="FAKE_API_KEY",
                default_model="fake-model", trains_on_data=False,
                extra_headers={"X-Title": "Igris"})


class Response:
    def __init__(self, payload, headers=None):
        self.raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.headers = headers or {}

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok(text="merhaba", headers=None):
    return Response({"choices": [{"message": {"role": "assistant", "content": text}}]}, headers)


def http_error(code, body=b"", headers=None):
    return urllib.error.HTTPError("https://api.fake.test/v1/chat/completions", code, "err",
                                  headers or {}, io.BytesIO(body))


class Opener:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def body(self, i=-1):
        return json.loads(self.requests[i][0].data.decode())


def backend(*results, **kwargs):
    opener = Opener(*results)
    kwargs.setdefault("api_key", KEY)
    return CompatBackend(FAKE, opener=opener, **kwargs), opener


def test_success_request_shape_system_first_and_roles_preserved():
    b, opener = backend(ok("selam"))
    messages = [Message("user", "soru"), Message("assistant", "cevap"),
                Message("tool", "42"), Message("user", "devam")]
    assert b.complete("sen Igris'sin", messages) == "selam"
    request, timeout = opener.requests[0]
    assert request.full_url == "https://api.fake.test/v1/chat/completions"
    assert request.get_method() == "POST" and timeout == 60
    assert request.get_header("Authorization") == "Bearer " + KEY
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("X-title") == "Igris"
    body = opener.body()
    assert body["model"] == "fake-model"
    assert body["messages"] == [
        {"role": "system", "content": "sen Igris'sin"},
        {"role": "user", "content": "soru"},
        {"role": "assistant", "content": "cevap"},
        {"role": "user", "content": "Tool result: 42"},
        {"role": "user", "content": "devam"},
    ]
    assert body["stream"] is False


def test_name_model_override_and_provider_exposed():
    b, opener = backend(ok(), model="other-model", timeout=5)
    assert b.name == "fake" and b.provider is FAKE and b.model == "other-model"
    b.complete("s", [Message("user", "x")])
    assert opener.body()["model"] == "other-model" and opener.requests[0][1] == 5


def test_reasoning_preamble_is_stripped():
    b, _ = backend(ok("<think>plan</think>\n cevap "))
    assert b.complete("s", [Message("user", "x")]) == "cevap"


def test_missing_key_names_the_variable_and_does_not_call_network(monkeypatch):
    monkeypatch.setattr("igris.llm_compat.get_secret", lambda name: None)
    opener = Opener()
    with pytest.raises(BackendError, match="IGRIS_FAKE_API_KEY"):
        CompatBackend(FAKE, opener=opener).complete("s", [Message("user", "x")])
    assert opener.requests == []


def test_key_comes_from_get_secret(monkeypatch):
    seen = []
    monkeypatch.setattr("igris.llm_compat.get_secret", lambda name: seen.append(name) or KEY)
    opener = Opener(ok())
    CompatBackend(FAKE, opener=opener).complete("s", [Message("user", "x")])
    assert seen == ["FAKE_API_KEY"]
    assert opener.requests[0][0].get_header("Authorization") == "Bearer " + KEY


def test_provider_secrets_lists_stored_free_provider_keys_only():
    from igris.llm_compat import provider_secrets

    stored = {"GROQ_API_KEY": "g", "CLOUDFLARE_ACCOUNT_ID": "acc", "GEMINI_API_KEY": "m",
              "EVREN_API_KEY": "e", "OPENAI_API_KEY": "paid"}
    asked = []
    out = provider_secrets(get=lambda name: asked.append(name) or stored.get(name))
    assert out == {"GROQ_API_KEY": "g", "CLOUDFLARE_ACCOUNT_ID": "acc", "GEMINI_API_KEY": "m",
                   "EVREN_API_KEY": "e"}
    assert "OPENAI_API_KEY" not in asked
    assert {p.key_name for p in PROVIDERS.values()} <= set(asked)


def test_429_is_rate_limited_with_retry_after():
    b, _ = backend(http_error(429, headers={"Retry-After": "17"}))
    with pytest.raises(RateLimited) as error:
        b.complete("s", [Message("user", "x")])
    assert error.value.backend == "fake" and error.value.retry_after == 17.0


@pytest.mark.parametrize("code, body", [
    (402, b'{"error": {"message": "Insufficient credits"}}'),
    (403, b'{"error": {"message": "You exceeded your current quota"}}'),
    (403, b'{"error": "daily limit reached"}'),
])
def test_quota_errors_are_rate_limited_with_quota_reason(code, body):
    b, _ = backend(http_error(code, body))
    with pytest.raises(RateLimited) as error:
        b.complete("s", [Message("user", "x")])
    assert "quota" in error.value.detail


def test_403_without_quota_text_is_access_denied():
    b, _ = backend(http_error(403, b'{"error": "model not allowed for this key"}'))
    with pytest.raises(BackendError) as error:
        b.complete("s", [Message("user", "x")])
    assert not isinstance(error.value, RateLimited)
    assert "HTTP 403" in str(error.value)


def test_401_is_invalid_key():
    b, _ = backend(http_error(401, b'{"error": "bad key"}'))
    with pytest.raises(BackendError, match="invalid key") as error:
        b.complete("s", [Message("user", "x")])
    assert not isinstance(error.value, RateLimited)


@pytest.mark.parametrize("failure", [
    http_error(500), http_error(503), TimeoutError(),
    urllib.error.URLError(TimeoutError()), urllib.error.URLError(OSError("dns")),
])
def test_server_errors_and_timeouts_are_backend_errors(failure):
    b, _ = backend(failure)
    with pytest.raises(BackendError) as error:
        b.complete("s", [Message("user", "x")])
    assert not isinstance(error.value, RateLimited)


@pytest.mark.parametrize("payload", [
    {"choices": []}, {"choices": [{"message": {"content": None}}]}, {"error": "x"}, b"not json",
])
def test_malformed_responses_are_backend_errors(payload):
    b, _ = backend(Response(payload))
    with pytest.raises(BackendError):
        b.complete("s", [Message("user", "x")])


@pytest.mark.parametrize("failure", [
    http_error(401, KEY.encode()), http_error(402, b"quota " + KEY.encode()),
    http_error(403, KEY.encode()), http_error(429, KEY.encode()), http_error(500, KEY.encode()),
    urllib.error.URLError(OSError(KEY)), Response({"error": KEY}),
])
def test_key_is_never_in_an_error_message(failure):
    b, _ = backend(failure)
    with pytest.raises(BackendError) as error:
        b.complete("s", [Message("user", "x")])
    assert KEY not in str(error.value) and KEY not in repr(error.value)
    assert error.value.__cause__ is None


class Clock:
    def __init__(self, when):
        self.now = when.timestamp()

    def __call__(self):
        return self.now


def test_quota_exhaustion_is_local_and_counts_failed_requests(tmp_path):
    clock = Clock(datetime(2026, 10, 30, 23, 59, 0, tzinfo=timezone.utc))
    quota = DailyQuota(tmp_path / "quota.json", {"fake": 2}, clock=clock)
    b, opener = backend(http_error(500), ok(), quota=quota)
    with pytest.raises(BackendError):
        b.complete("s", [Message("user", "x")])
    assert b.complete("s", [Message("user", "x")]) == "merhaba"
    assert quota.used("fake") == 2 and quota.remaining("fake") == 0
    with pytest.raises(RateLimited) as error:
        b.complete("s", [Message("user", "x")])
    assert len(opener.requests) == 2
    assert "quota" in error.value.detail and error.value.retry_after == 60.0


def test_quota_resets_at_utc_midnight_and_persists(tmp_path):
    clock = Clock(datetime(2026, 10, 30, 23, 0, tzinfo=timezone.utc))
    path = tmp_path / "quota.json"
    quota = DailyQuota(path, {"fake": 1}, clock=clock)
    quota.spend("fake")
    assert DailyQuota(path, {"fake": 1}, clock=clock).remaining("fake") == 0
    clock.now = datetime(2026, 10, 31, 0, 0, 1, tzinfo=timezone.utc).timestamp()
    assert quota.remaining("fake") == 1 and quota.used("fake") == 0
    b, _ = backend(ok(), quota=quota)
    assert b.complete("s", [Message("user", "x")]) == "merhaba"


def test_quota_without_limit_counts_but_never_blocks_and_survives_corrupt_file(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text("{not json", encoding="utf-8")
    quota = DailyQuota(path, {}, clock=lambda: 0)
    assert quota.remaining("fake") is None
    quota.spend("fake")
    quota.spend("fake")
    assert quota.used("fake") == 2
    quota.check("fake")


def test_registry_holds_providers_by_name():
    assert all(isinstance(p, Provider) and p.name == name for name, p in PROVIDERS.items())


def test_402_spends_the_rest_of_the_day_locally(tmp_path):
    quota = DailyQuota(tmp_path / "q.json", {}, clock=lambda: 0)
    b, opener = backend(http_error(402, b"negative balance"), quota=quota)
    with pytest.raises(RateLimited):
        b.complete("s", [Message("user", "x")])
    with pytest.raises(RateLimited, match="quota"):
        b.complete("s", [Message("user", "x")])
    assert len(opener.requests) == 1


def test_url_secret_is_filled_into_base_url_and_missing_one_is_named(monkeypatch):
    provider = Provider("acct", "https://api.acct.test/accounts/{ACCT_ID}/ai/v1", "ACCT_TOKEN",
                        "m", url_secrets=("ACCT_ID",))
    secrets = {"ACCT_TOKEN": KEY, "ACCT_ID": "abc123"}
    monkeypatch.setattr("igris.llm_compat.get_secret", lambda name: secrets.get(name))
    opener = Opener(ok())
    CompatBackend(provider, opener=opener).complete("s", [Message("user", "x")])
    assert opener.requests[0][0].full_url == \
        "https://api.acct.test/accounts/abc123/ai/v1/chat/completions"
    del secrets["ACCT_ID"]
    with pytest.raises(BackendError, match="IGRIS_ACCT_ID"):
        CompatBackend(provider, opener=Opener()).complete("s", [Message("user", "x")])


def test_free_model_is_discovered_once_a_day_and_cached(tmp_path):
    provider = Provider("router", "https://router.test/v1", "R_KEY", "", free_suffix=":free")
    models = Response({"data": [{"id": "big/paid-model"}, {"id": "vendor/small:free"},
                                {"id": "vendor/large:free"}]})
    clock = Clock(datetime(2026, 10, 30, 10, tzinfo=timezone.utc))
    quota = DailyQuota(tmp_path / "q.json", {}, clock=clock)
    opener = Opener(models, ok(), ok())
    b = CompatBackend(provider, api_key=KEY, opener=opener, quota=quota)
    b.complete("s", [Message("user", "x")])
    b.complete("s", [Message("user", "y")])
    listing = opener.requests[0][0]
    assert listing.get_method() == "GET" and listing.full_url == "https://router.test/v1/models"
    assert opener.body(1)["model"] == "vendor/small:free" == opener.body(2)["model"]
    assert len(opener.requests) == 3
    # a paid model id is never chosen, even when it is explicitly configured
    with pytest.raises(BackendError, match=":free"):
        CompatBackend(provider, "big/paid-model", api_key=KEY, opener=Opener()) \
            .complete("s", [Message("user", "x")])


def test_free_model_discovery_without_free_models_fails_cleanly(tmp_path):
    provider = Provider("router", "https://router.test/v1", "R_KEY", "", free_suffix=":free")
    opener = Opener(Response({"data": [{"id": "paid"}]}))
    b = CompatBackend(provider, api_key=KEY, opener=opener,
                      quota=DailyQuota(tmp_path / "q.json", {}, clock=lambda: 0))
    with pytest.raises(BackendError, match="no free models"):
        b.complete("s", [Message("user", "x")])
    assert len(opener.requests) == 1


def test_rate_limit_headers_are_exposed():
    b, _ = backend(ok(headers={"x-ratelimit-remaining-requests": "998",
                               "x-ratelimit-limit-requests": "1000",
                               "x-ratelimit-reset-requests": "1m26s"}))
    b.complete("s", [Message("user", "x")])
    assert b.limits() == {"limit_requests": 1000, "remaining_requests": 998,
                          "reset_requests": "1m26s"}


def test_header_reports_zero_remaining_requests_then_backend_rests_locally():
    b, opener = backend(ok(headers={"x-ratelimit-remaining-requests": "0",
                                    "x-ratelimit-reset-requests": "30s"}))
    b.complete("s", [Message("user", "x")])
    with pytest.raises(RateLimited) as error:
        b.complete("s", [Message("user", "x")])
    assert error.value.retry_after == 30.0 and len(opener.requests) == 1


@pytest.mark.parametrize("name", sorted(PROVIDERS))
def test_every_registered_provider_request_shape(name, monkeypatch, tmp_path):
    provider = PROVIDERS[name]
    monkeypatch.setattr("igris.llm_compat.get_secret", lambda n: "account-id" if n in
                        provider.url_secrets else KEY)
    results = [ok("tamam")]
    if provider.free_suffix:
        results.insert(0, Response({"data": [{"id": "vendor/m" + provider.free_suffix}]}))
    opener = Opener(*results)
    b = CompatBackend(provider, opener=opener,
                      quota=DailyQuota(tmp_path / "q.json", {}, clock=lambda: 0))
    assert b.complete("sistem", [Message("user", "merhaba")]) == "tamam"
    request = opener.requests[-1][0]
    assert request.full_url.startswith("https://") and "{" not in request.full_url
    assert request.full_url.endswith("/chat/completions")
    assert request.get_header("Authorization") == "Bearer " + KEY
    body = opener.body()
    assert body["messages"][0] == {"role": "system", "content": "sistem"}
    assert body["model"] and (not provider.free_suffix or body["model"].endswith(":free"))
    assert provider.key_name.isupper() and provider.key_name.replace("_", "").isalnum()


def test_registry_lists_the_verified_providers():
    assert set(PROVIDERS) == {"openrouter", "groq", "cerebras", "mistral", "nvidia",
                              "cloudflare", "sambanova", "cohere"}
    assert "huggingface" not in PROVIDERS  # free accounts have no credits; every call bills
    trains = {n for n, p in PROVIDERS.items() if p.trains_on_data}
    assert {"openrouter", "mistral"} <= trains and "groq" not in trains
    assert {n for n, p in PROVIDERS.items() if p.trial} == {"cerebras", "cohere"}


def test_gemini_backend_is_marked_as_training_on_free_tier():
    from igris.llm_extra import GeminiBackend

    assert GeminiBackend.trains_on_data is True
    assert GeminiBackend().model == "gemini-3.8-flash"


def test_provider_is_frozen():
    with pytest.raises(Exception):
        FAKE.name = "other"
