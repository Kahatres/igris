"""Settings loaded from IGRIS_* environment variables and an optional .env file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

# OpenAI-compatible free providers; must match igris.llm_compat.PROVIDERS
COMPAT_BACKENDS = ("openrouter", "groq", "cerebras", "mistral", "nvidia", "cloudflare",
                   "sambanova", "cohere")
ALLOWED_BACKENDS = ("claude", "codex", "opencode", "echo", "openai", "gemini", "ollama") + COMPAT_BACKENDS
# text-only backends a regular (non-owner) user may get; host CLIs need OS isolation
USER_SAFE_BACKENDS = ("echo", "ollama", "openai", "gemini") + COMPAT_BACKENDS


def default_data_dir() -> Path:
    """The folder that holds all state: IGRIS_DATA_DIR, else ``~/.igris``."""
    value = os.environ.get("IGRIS_DATA_DIR", "").strip()
    return Path(value) if value else Path.home() / ".igris"


def _in_data_dir(*parts: str):
    return field(default_factory=lambda: default_data_dir().joinpath(*parts))


@dataclass(frozen=True)
class Settings:
    """Runtime settings for Igris."""

    name: str = "Igris"
    # user name of the regular (non-owner) account created by --setup-users
    guest_username: str = "guest"
    backends: tuple[str, ...] = ("claude", "codex")
    claude_model: str = "sonnet"
    codex_model: str = "gpt-6-astra"
    opencode_model: str = "deepseek-v4.1-flash-free"
    memory_path: Path = _in_data_dir("memory.jsonl")
    max_history: int = 20
    timeout: int = 300
    persona: str = "default"
    sessions_dir: Path = _in_data_dir("sessions")
    personas_dir: Path = _in_data_dir("personas")
    workspace: Path | None = None
    multiagent_dir: Path | None = None
    slow_backends: tuple[str, ...] = ("claude", "codex")
    openai_model: str = "gpt-4o"
    openai_daily_budget_usd: float = 1.0
    budget_path: Path = _in_data_dir("budget.json")
    gemini_model: str = "gemini-3.8-flash"
    ollama_model: str = "qwen3:4b"
    ollama_url: str = "http://localhost:11434"
    ma_parallel: int = 1
    jobs_path: Path = _in_data_dir("jobs.json")
    data_dir: Path = _in_data_dir()
    user_backends: tuple[str, ...] = ("ollama",)
    public_origin: str | None = None
    host: str = "127.0.0.1"
    task_workers: int = 2
    # uploaded files: per-file size, per-account total and per-account count
    file_max_mb: int = 25
    file_quota_mb: int = 500
    file_max_count: int = 500
    allow_training_providers: bool = False
    # inbox watcher: seconds between Gmail polls, local hour of the daily digest
    inbox_interval: int = 300
    digest_hour: int = 19
    # shared ESN Google Calendar ids read into the task calendar (IGRIS_ESN_CALENDAR_ID, comma list)
    esn_calendar_ids: tuple[str, ...] = ()
    # the old daily digest of "important" mail; off: only tasks and security problems notify
    mail_digest: bool = False
    # school mail domains (IGRIS_SCHOOL_DOMAINS, comma list): a school mail forwarded from
    # Outlook into the Gmail inbox is recognised by them and its tasks go to the okul branch
    school_domains: tuple = ()
    mail_max_recipients: int = 5
    mail_daily_limit: int = 20
    mail_followup_days: int = 3
    school_address: str = ""
    # one sentence about the owner given to the mail-sorting model (IGRIS_OWNER_CONTEXT);
    # empty uses a generic sentence (igris.mail_decide.DEFAULT_OWNER_CONTEXT)
    owner_context: str = ""
    # local hour (0-23) of the Telegram morning summary; None turns it off
    morning_hour: int | None = 8
    # the owner's web_ara chat tool (igris.web_research): on, seconds per research call,
    # Claude model of the research call (empty: the CLI's default)
    web_research: bool = True
    web_timeout: int = 120
    web_model: str = "sonnet"
    # Small, tool-free study tasks; empty inherits web_model.
    study_fast_model: str = "haiku"
    core: bool = True
    study: bool = True
    vision_model: str | None = None
    # the doviz_kuru chat tool (igris.fx_rates): official TCMB exchange rates for every user
    fx_rates: bool = True
    # the owner's sayfa_oku chat tool (igris.browser_read): on, allowed registrable domains
    # (the page opens in the owner's real Chrome, so this list is the main guard), seconds
    # per call; the call uses web_model
    browser_read: bool = True
    browser_domains: tuple = ()
    browser_timeout: int = 150
    # provider name -> model id, from IGRIS_<PROVIDER>_MODEL
    compat_models: dict = field(default_factory=dict, hash=False)

    def __post_init__(self):
        if self.vision_model is None:
            object.__setattr__(self, "vision_model", self.web_model)


_PERSONA_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")
_DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines of a .env file into a dict."""
    result: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            if value.startswith("#"):
                value = ""
            else:
                idx = value.find(" #")
                if idx == -1:
                    idx = value.find("\t#")
                if idx != -1:
                    value = value[:idx].rstrip()
        result[key] = value
    return result


def _parse_int(name: str, raw: str) -> int:
    try:
        number = int(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer >= 1, got {raw!r}") from None
    if number < 1:
        raise ValueError(f"{name} must be an integer >= 1, got {raw!r}")
    return number


def _parse_ma_parallel(raw: str) -> int:
    try:
        number = int(raw.strip())
    except ValueError:
        raise ValueError(f"IGRIS_MA_PARALLEL must be an integer 1-4, got {raw!r}") from None
    if not 1 <= number <= 4:
        raise ValueError(f"IGRIS_MA_PARALLEL must be an integer 1-4, got {raw!r}")
    return number


def _parse_backends(raw: str) -> tuple[str, ...]:
    items = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    if not items:
        raise ValueError("IGRIS_BACKENDS must list at least one backend")
    for item in items:
        if item not in ALLOWED_BACKENDS:
            raise ValueError(
                f"IGRIS_BACKENDS contains unknown backend {item!r}; "
                f"allowed: {', '.join(ALLOWED_BACKENDS)}"
            )
    return items


def _parse_backend_list(var: str, raw: str) -> tuple[str, ...]:
    items = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    if not items:
        raise ValueError(f"{var} must list at least one backend")
    for item in items:
        if item not in ALLOWED_BACKENDS:
            raise ValueError(
                f"{var} contains unknown backend {item!r}; "
                f"allowed: {', '.join(ALLOWED_BACKENDS)}"
            )
    return items


def _parse_positive_float(name: str, raw: str) -> float:
    import math

    try:
        number = float(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number > 0, got {raw!r}") from None
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a number > 0, got {raw!r}")
    return number


def find_multiagent_dir(base: Path | None = None) -> Path | None:
    """A multiagent checkout next to the Igris checkout (``../multiagent``), or None."""
    base = Path(__file__).resolve().parents[1] if base is None else Path(base)
    candidate = base.parent / "multiagent"
    return candidate if (candidate / "multiagent.py").is_file() else None


def load_settings(
    env: Mapping[str, str] | None = None, dotenv: Path | None = None
) -> Settings:
    """Build Settings; env values win over the .env file, which wins over defaults.

    Only when reading the real process environment is an unset IGRIS_MULTIAGENT_DIR
    filled in with a sibling multiagent checkout.
    """
    reading_process_env = env is None
    if env is None:
        env = os.environ
    merged: dict[str, str] = {}
    if dotenv is not None:
        path = Path(dotenv)
        if path.is_file():
            merged.update(parse_dotenv(path.read_text(encoding="utf-8")))
    for key, value in env.items():
        if key.startswith("IGRIS_"):
            merged[key] = value

    defaults = Settings()
    kwargs: dict[str, object] = {}
    if "IGRIS_NAME" in merged:
        kwargs["name"] = merged["IGRIS_NAME"]
    if "IGRIS_GUEST_USERNAME" in merged:
        guest = merged["IGRIS_GUEST_USERNAME"].strip().lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", guest) or guest == "admin":
            raise ValueError("IGRIS_GUEST_USERNAME must be 1-32 characters of a-z, 0-9, _ or - "
                             f"and not 'admin', got {merged['IGRIS_GUEST_USERNAME']!r}")
        kwargs["guest_username"] = guest
    if "IGRIS_BACKENDS" in merged:
        kwargs["backends"] = _parse_backends(merged["IGRIS_BACKENDS"])
    if "IGRIS_CLAUDE_MODEL" in merged:
        kwargs["claude_model"] = merged["IGRIS_CLAUDE_MODEL"]
    if "IGRIS_CODEX_MODEL" in merged:
        kwargs["codex_model"] = merged["IGRIS_CODEX_MODEL"]
    if "IGRIS_OPENCODE_MODEL" in merged:
        from igris.llm_opencode import FREE_MODELS

        model = merged["IGRIS_OPENCODE_MODEL"].strip()
        if model not in FREE_MODELS:
            raise ValueError("IGRIS_OPENCODE_MODEL must be one of: "
                             + ", ".join(sorted(FREE_MODELS)))
        kwargs["opencode_model"] = model
    if "IGRIS_MEMORY_PATH" in merged:
        kwargs["memory_path"] = Path(merged["IGRIS_MEMORY_PATH"])
    if "IGRIS_MAX_HISTORY" in merged:
        kwargs["max_history"] = _parse_int("IGRIS_MAX_HISTORY", merged["IGRIS_MAX_HISTORY"])
    if "IGRIS_TIMEOUT" in merged:
        kwargs["timeout"] = _parse_int("IGRIS_TIMEOUT", merged["IGRIS_TIMEOUT"])
    if "IGRIS_PERSONA" in merged:
        persona = merged["IGRIS_PERSONA"].strip()
        if not _PERSONA_RE.fullmatch(persona):
            raise ValueError(
                f"IGRIS_PERSONA must match [A-Za-z0-9_-]{{1,40}}, got {merged['IGRIS_PERSONA']!r}"
            )
        kwargs["persona"] = persona
    if "IGRIS_SESSIONS_DIR" in merged:
        kwargs["sessions_dir"] = Path(merged["IGRIS_SESSIONS_DIR"])
    if "IGRIS_PERSONAS_DIR" in merged:
        kwargs["personas_dir"] = Path(merged["IGRIS_PERSONAS_DIR"])
    if "IGRIS_WORKSPACE" in merged:
        ws = merged["IGRIS_WORKSPACE"].strip()
        kwargs["workspace"] = Path(ws) if ws else None
    if "IGRIS_MULTIAGENT_DIR" in merged:
        ma = merged["IGRIS_MULTIAGENT_DIR"].strip()
        kwargs["multiagent_dir"] = Path(ma) if ma else None
    elif reading_process_env:
        # unset (not empty): use a multiagent checkout sitting next to this one
        kwargs["multiagent_dir"] = find_multiagent_dir()
    if "IGRIS_SLOW_BACKENDS" in merged:
        kwargs["slow_backends"] = _parse_backend_list(
            "IGRIS_SLOW_BACKENDS", merged["IGRIS_SLOW_BACKENDS"]
        )
    if "IGRIS_OPENAI_MODEL" in merged:
        kwargs["openai_model"] = merged["IGRIS_OPENAI_MODEL"]
    if "IGRIS_OPENAI_DAILY_BUDGET_USD" in merged:
        kwargs["openai_daily_budget_usd"] = _parse_positive_float(
            "IGRIS_OPENAI_DAILY_BUDGET_USD", merged["IGRIS_OPENAI_DAILY_BUDGET_USD"]
        )
    if "IGRIS_BUDGET_PATH" in merged:
        kwargs["budget_path"] = Path(merged["IGRIS_BUDGET_PATH"])
    if "IGRIS_GEMINI_MODEL" in merged:
        kwargs["gemini_model"] = merged["IGRIS_GEMINI_MODEL"]
    if "IGRIS_OLLAMA_MODEL" in merged:
        kwargs["ollama_model"] = merged["IGRIS_OLLAMA_MODEL"]
    if "IGRIS_OLLAMA_URL" in merged:
        kwargs["ollama_url"] = merged["IGRIS_OLLAMA_URL"]
    compat_models = {}
    for provider in COMPAT_BACKENDS:
        var = f"IGRIS_{provider.upper()}_MODEL"
        if var in merged:
            if not merged[var].strip():
                raise ValueError(f"{var} must not be empty")
            compat_models[provider] = merged[var].strip()
    kwargs["compat_models"] = compat_models
    if "IGRIS_MA_PARALLEL" in merged:
        kwargs["ma_parallel"] = _parse_ma_parallel(merged["IGRIS_MA_PARALLEL"])
    if "IGRIS_JOBS_PATH" in merged:
        kwargs["jobs_path"] = Path(merged["IGRIS_JOBS_PATH"])
    if "IGRIS_DATA_DIR" in merged:
        if not merged["IGRIS_DATA_DIR"].strip():
            raise ValueError("IGRIS_DATA_DIR must not be empty")
        data_dir = Path(merged["IGRIS_DATA_DIR"])
        kwargs["data_dir"] = data_dir
        # A path set on its own above stays where it was put.
        kwargs.setdefault("memory_path", data_dir / "memory.jsonl")
        kwargs.setdefault("sessions_dir", data_dir / "sessions")
        kwargs.setdefault("personas_dir", data_dir / "personas")
        kwargs.setdefault("budget_path", data_dir / "budget.json")
        kwargs.setdefault("jobs_path", data_dir / "jobs.json")
    if "IGRIS_USER_BACKENDS" in merged:
        names = _parse_backend_list("IGRIS_USER_BACKENDS", merged["IGRIS_USER_BACKENDS"])
        if set(names) - set(USER_SAFE_BACKENDS):
            raise ValueError("IGRIS_USER_BACKENDS supports " + ", ".join(USER_SAFE_BACKENDS)
                             + "; CLI backends require OS-level isolation for untrusted users")
        kwargs["user_backends"] = names
    del defaults
    if "IGRIS_TASK_WORKERS" in merged:
        count = _parse_int("IGRIS_TASK_WORKERS", merged["IGRIS_TASK_WORKERS"])
        if count > 4:
            raise ValueError("IGRIS_TASK_WORKERS must be between 1 and 4")
        kwargs["task_workers"] = count
    for env_name, key, ceiling in (("IGRIS_FILE_MAX_MB", "file_max_mb", 200),
                                   ("IGRIS_FILE_QUOTA_MB", "file_quota_mb", 10000),
                                   ("IGRIS_FILE_MAX_COUNT", "file_max_count", 10000)):
        if env_name in merged:
            value = _parse_int(env_name, merged[env_name])
            if value > ceiling:
                raise ValueError(f"{env_name} must be between 1 and {ceiling}")
            kwargs[key] = value
    for key, ceiling in (("mail_max_recipients", 20), ("mail_daily_limit", 200),
                         ("mail_followup_days", 30)):
        name = "IGRIS_" + key.upper()
        if name in merged:
            value = _parse_int(name, merged[name])
            if not 1 <= value <= ceiling:
                raise ValueError(f"{name} must be between 1 and {ceiling}")
            kwargs[key] = value
    if "IGRIS_SCHOOL_ADDRESS" in merged:
        kwargs["school_address"] = merged["IGRIS_SCHOOL_ADDRESS"].strip()
    if "IGRIS_OWNER_CONTEXT" in merged:
        raw = " ".join(merged["IGRIS_OWNER_CONTEXT"].split())
        if len(raw) > 500:
            raise ValueError("IGRIS_OWNER_CONTEXT must be at most 500 characters")
        kwargs["owner_context"] = raw
    if "IGRIS_INBOX_INTERVAL" in merged:
        value = _parse_int("IGRIS_INBOX_INTERVAL", merged["IGRIS_INBOX_INTERVAL"])
        if not 60 <= value <= 3600:
            raise ValueError("IGRIS_INBOX_INTERVAL must be between 60 and 3600 seconds")
        kwargs["inbox_interval"] = value
    if "IGRIS_DIGEST_HOUR" in merged:
        raw = merged["IGRIS_DIGEST_HOUR"].strip()
        if not raw.isdigit() or not 0 <= int(raw) <= 23:
            raise ValueError(f"IGRIS_DIGEST_HOUR must be an hour 0-23, got {raw!r}")
        kwargs["digest_hour"] = int(raw)
    if "IGRIS_MORNING_HOUR" in merged:
        # unset means 08:00; an empty value, "off" or anything that is not an hour 0-23
        # turns the morning summary off instead of stopping the server
        raw = merged["IGRIS_MORNING_HOUR"].strip()
        kwargs["morning_hour"] = int(raw) if raw.isdigit() and 0 <= int(raw) <= 23 else None
    if "IGRIS_ESN_CALENDAR_ID" in merged:
        ids = tuple(dict.fromkeys(
            part.strip() for part in merged["IGRIS_ESN_CALENDAR_ID"].split(",") if part.strip()))
        if any(len(i) > 256 or any(c.isspace() for c in i) for i in ids) or len(ids) > 5:
            raise ValueError("IGRIS_ESN_CALENDAR_ID must be up to 5 calendar ids separated by commas")
        kwargs["esn_calendar_ids"] = ids
    if "IGRIS_MAIL_DIGEST" in merged:
        raw = merged["IGRIS_MAIL_DIGEST"].strip().lower()
        if raw not in ("0", "1", "true", "false", "yes", "no", ""):
            raise ValueError("IGRIS_MAIL_DIGEST must be 0 or 1")
        kwargs["mail_digest"] = raw in ("1", "true", "yes")
    if "IGRIS_SCHOOL_DOMAINS" in merged:
        domains = tuple(d.strip().lower().lstrip("@") for d in
                        merged["IGRIS_SCHOOL_DOMAINS"].split(",") if d.strip())
        for domain in domains:
            if "." not in domain or any(c.isspace() or c in "@/" for c in domain):
                raise ValueError(f"IGRIS_SCHOOL_DOMAINS has an invalid domain: {domain!r}")
        kwargs["school_domains"] = domains
    if "IGRIS_WEB_RESEARCH" in merged:
        raw = merged["IGRIS_WEB_RESEARCH"].strip().lower()
        if raw not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
            raise ValueError("IGRIS_WEB_RESEARCH must be 0 or 1")
        kwargs["web_research"] = raw in ("1", "true", "yes", "on")
    if "IGRIS_FX_RATES" in merged:
        raw = merged["IGRIS_FX_RATES"].strip().lower()
        if raw not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
            raise ValueError("IGRIS_FX_RATES must be 0 or 1")
        kwargs["fx_rates"] = raw in ("1", "true", "yes", "on")
    if "IGRIS_WEB_TIMEOUT" in merged:
        value = _parse_int("IGRIS_WEB_TIMEOUT", merged["IGRIS_WEB_TIMEOUT"])
        if not 10 <= value <= 600:
            raise ValueError("IGRIS_WEB_TIMEOUT must be between 10 and 600 seconds")
        kwargs["web_timeout"] = value
    if "IGRIS_WEB_MODEL" in merged:
        model = merged["IGRIS_WEB_MODEL"].strip()
        if model and not re.fullmatch(r"[A-Za-z0-9._:\[\]-]{1,80}", model):
            raise ValueError(f"IGRIS_WEB_MODEL is not a model name: {model!r}")
        kwargs["web_model"] = model
    if "IGRIS_STUDY_FAST_MODEL" in merged:
        model = merged["IGRIS_STUDY_FAST_MODEL"].strip()
        if model and not re.fullmatch(r"[A-Za-z0-9._:\[\]-]{1,80}", model):
            raise ValueError(f"IGRIS_STUDY_FAST_MODEL is not a model name: {model!r}")
        kwargs["study_fast_model"] = model
    for key in ("core", "study"):
        name = "IGRIS_" + key.upper()
        if name in merged:
            raw = merged[name].strip().lower()
            if raw not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
                raise ValueError(f"{name} must be 0 or 1")
            kwargs[key] = raw in ("1", "true", "yes", "on")
    if "IGRIS_VISION_MODEL" in merged:
        model = merged["IGRIS_VISION_MODEL"].strip()
        if model and not re.fullmatch(r"[A-Za-z0-9._:\[\]-]{1,80}", model):
            raise ValueError(f"IGRIS_VISION_MODEL is not a model name: {model!r}")
        kwargs["vision_model"] = model or kwargs.get("web_model", "sonnet")
    if "IGRIS_BROWSER_READ" in merged:
        raw = merged["IGRIS_BROWSER_READ"].strip().lower()
        if raw not in ("0", "1", "true", "false", "yes", "no", "on", "off"):
            raise ValueError("IGRIS_BROWSER_READ must be 0 or 1")
        kwargs["browser_read"] = raw in ("1", "true", "yes", "on")
    if "IGRIS_BROWSER_DOMAINS" in merged:
        domains = []
        for part in merged["IGRIS_BROWSER_DOMAINS"].split(","):
            name = part.strip().lower().lstrip(".")
            if not name:
                continue
            labels = name.split(".")
            if (len(labels) < 2 or len(name) > 253 or labels[-1].isdigit()
                    or any(not _DOMAIN_LABEL_RE.fullmatch(x) or x.startswith("xn--")
                           for x in labels)):
                raise ValueError(f"IGRIS_BROWSER_DOMAINS has an invalid domain: {name!r} (use "
                                 "plain ASCII names such as example.com, no scheme, path, port or "
                                 "punycode)")
            if name not in domains:
                domains.append(name)
        kwargs["browser_domains"] = tuple(domains)
    if "IGRIS_BROWSER_TIMEOUT" in merged:
        value = _parse_int("IGRIS_BROWSER_TIMEOUT", merged["IGRIS_BROWSER_TIMEOUT"])
        if not 30 <= value <= 600:
            raise ValueError("IGRIS_BROWSER_TIMEOUT must be between 30 and 600 seconds")
        kwargs["browser_timeout"] = value
    if "IGRIS_ALLOW_TRAINING_PROVIDERS" in merged:
        raw = merged["IGRIS_ALLOW_TRAINING_PROVIDERS"].strip().lower()
        if raw not in ("0", "1", "true", "false", "yes", "no", ""):
            raise ValueError("IGRIS_ALLOW_TRAINING_PROVIDERS must be 0 or 1")
        kwargs["allow_training_providers"] = raw in ("1", "true", "yes")
    if "IGRIS_PUBLIC_ORIGIN" in merged:
        origin = merged["IGRIS_PUBLIC_ORIGIN"].strip().rstrip("/")
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or any(c.isspace() for c in origin)):
            raise ValueError("IGRIS_PUBLIC_ORIGIN must be an HTTPS origin without a path")
        kwargs["public_origin"] = origin
    if "IGRIS_HOST" in merged:
        host = merged["IGRIS_HOST"].strip()
        if not host:
            raise ValueError("IGRIS_HOST must not be empty")
        kwargs["host"] = host
    return Settings(**kwargs)  # type: ignore[arg-type]
