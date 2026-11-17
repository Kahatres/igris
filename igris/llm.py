"""LLM backends (Claude CLI, Codex CLI, echo) and a round-robin router."""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

__all__ = [
    "Message",
    "BackendError",
    "RateLimited",
    "PaidUsageBlocked",
    "Backend",
    "render_transcript",
    "EchoBackend",
    "ClaudeCLIBackend",
    "CodexCLIBackend",
    "Router",
    "build_backend",
    "allow_paid",
    "paid_env",
    "resolve_cli",
    "CLAUDE_PAID_ENV",
    "CODEX_PAID_ENV",
]

_LIMIT_MARKERS = ("usage limit", "rate limit", "hit your limit")
_DEFAULT_REST = 60.0


@dataclass(frozen=True)
class Message:
    """A single conversation message."""

    role: str  # "user" | "assistant" | "tool"
    content: str


class BackendError(RuntimeError):
    """A backend failed to produce an answer."""


class RateLimited(BackendError):
    """A backend reported a usage or rate limit."""

    def __init__(self, backend: str, retry_after: float | None, detail: str = ""):
        self.backend = backend
        self.retry_after = retry_after
        self.detail = detail
        msg = f"{backend} rate limited"
        if detail:
            msg += f": {detail}"
        super().__init__(msg)


class PaidUsageBlocked(BackendError):
    """The CLI would bill an API account instead of the subscription (set IGRIS_ALLOW_PAID=1 to allow)."""


# Environment variables that make the CLIs bill an API account instead of the subscription.
CLAUDE_PAID_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
                   "CLAUDE_CODE_USE_VERTEX")
CODEX_PAID_ENV = ("OPENAI_API_KEY", "CODEX_API_KEY")


def paid_env(names: tuple[str, ...], env) -> list[str]:
    """Names of active billing settings in the supplied environment."""
    return [n for n in names if str(env.get(n, "")).strip().lower() not in ("", "0", "false")]


def allow_paid(env) -> bool:
    """Whether the owner explicitly enabled paid usage."""
    return env.get("IGRIS_ALLOW_PAID") == "1"


_SHIM_TARGET_RE = re.compile(r'"%dp0%\\([^"%]+\.(?:exe|js))"', re.IGNORECASE)


def _resolve_cli(exe: str, *, which=shutil.which, is_windows: bool | None = None,
                 strict: bool = False) -> list[str]:
    """Return the argv prefix that starts ``exe`` without going through cmd.exe.

    On Windows npm installs CLIs as ``.cmd`` shims, which ``subprocess`` cannot start
    directly and which would let cmd.exe re-parse our arguments. The shim's real target
    (an ``.exe`` or a ``node`` script) is run instead.
    ``strict`` rejects unreadable or unsupported shims instead of returning them.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    found = which(exe)
    if not found and not strict:
        return [exe]
    found = found or exe
    if not is_windows or not found.lower().endswith((".cmd", ".bat")):
        return [found]
    try:
        text = Path(found).read_text(encoding="utf-8", errors="replace")
    except OSError:
        if strict:
            raise
        return [found]
    # The codex shim also names "%dp0%\node.exe" (the interpreter), which is not the target.
    targets = [m for m in _SHIM_TARGET_RE.findall(text) if m.lower() != "node.exe"]
    if not targets:
        if strict:
            raise ValueError("unsupported_cli_shim")
        return [found]
    folder = Path(found).parent
    target = folder / targets[-1]
    if target.suffix.lower() == ".exe":
        return [str(target)]
    node = folder / "node.exe"
    return [str(node) if node.is_file() else (which("node") or "node"), str(target)]


def resolve_cli(exe: str, *, strict: bool = False) -> list[str]:
    """Argv prefix for ``exe`` on this machine (tests replace this)."""
    return _resolve_cli(exe, strict=strict)


def codex_login_problem(exe: str, runner, timeout: int = 30) -> str | None:
    """None when `codex login status` reports a ChatGPT login, else the reason (the CLI prints to stderr)."""
    try:
        proc = runner([*resolve_cli(exe), "login", "status"], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"codex login could not be verified: {exc}"
    out = f"{proc.stdout or ''} {proc.stderr or ''}".strip()
    if proc.returncode != 0:
        return f"codex is not logged in ({_short(out, 120)}); run `codex login` with ChatGPT"
    if "chatgpt" not in out.lower():
        return f"codex is not logged in with ChatGPT ({_short(out, 120)}); an API key login is billed"
    return None


class Backend(Protocol):
    name: str

    def complete(self, system: str, messages: list[Message]) -> str: ...


def render_transcript(messages: list[Message]) -> str:
    """Render messages as ``ROLE: content`` blocks separated by blank lines.

    A tool result is labelled ``TOOL RESULT:``, so it is not read as a ``TOOL:`` call line
    or as text the user pasted.
    """
    return "\n\n".join(f"{'TOOL RESULT' if m.role == 'tool' else m.role.upper()}: {m.content}"
                       for m in messages)


def _is_limit(*texts: str | None) -> bool:
    blob = " ".join(t for t in texts if t).lower()
    return any(marker in blob for marker in _LIMIT_MARKERS)


def _short(text: str | None, n: int = 300) -> str:
    return (text or "").strip()[:n]


class EchoBackend:
    """Trivial backend that echoes the last user message."""

    name = "echo"

    def complete(self, system: str, messages: list[Message]) -> str:
        for m in reversed(messages):
            if m.role == "user":
                return "echo: " + m.content
        return "echo: "


def _run_cli(
    name: str,
    runner: Callable,
    cmd: list[str],
    stdin: str,
    timeout: int,
):
    """Run a CLI, mapping launch failures and error exits to exceptions."""
    try:
        proc = runner(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            # The CLIs speak UTF-8; the Windows default codepage would garble non-ASCII text.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise BackendError(f"{name} timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise BackendError(f"{name} executable not found: {cmd[0]}") from exc
    except OSError as exc:
        raise BackendError(f"{name} could not be started: {exc}") from exc
    if proc.returncode != 0:
        if _is_limit(proc.stderr, proc.stdout):
            raise RateLimited(name, None, _short(proc.stderr or proc.stdout))
        raise BackendError(
            f"{name} exited with code {proc.returncode}: "
            f"{_short(proc.stderr or proc.stdout)}"
        )
    return proc


_WARM_KEEP = 2  # ready Claude processes kept, one per system prompt (answers + routing classifier)


def _kill(proc) -> None:
    try:
        proc.kill()
    except OSError:
        pass


class ClaudeCLIBackend:
    """Backend that shells out to the ``claude`` CLI.

    With ``popen`` set, the next turn's process is started in advance and waits on
    stdin (stream-json), so a turn skips the CLI's startup time. Each process still
    answers exactly one turn from the full transcript, as with ``runner``.
    """

    name = "claude"

    def __init__(
        self,
        model: str = "sonnet",
        timeout: int = 300,
        runner=subprocess.run,
        exe: str = "claude",
        env=None,
        popen=None,
    ):
        self.model = model
        self.timeout = timeout
        self.runner = runner
        self.exe = exe
        self.env = os.environ if env is None else env
        self.popen = popen
        self._ready: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()
        if popen is not None:
            atexit.register(self.close)

    def complete(self, system: str, messages: list[Message]) -> str:
        paid = [] if allow_paid(self.env) else paid_env(CLAUDE_PAID_ENV, self.env)
        if paid:
            raise PaidUsageBlocked(f"claude would bill an API account ({', '.join(paid)} set); "
                                   "unset it or set IGRIS_ALLOW_PAID=1")
        if self.popen is not None:
            return self._complete_warm(system, messages)
        cmd = [
            *resolve_cli(self.exe), "-p",
            "--model", self.model,
            "--output-format", "json",
            "--system-prompt", system,
            "--tools", "",
            "--strict-mcp-config",  # no MCP servers: the owner's claude.ai connectors stay out
        ]
        proc = _run_cli(
            self.name, self.runner, cmd, render_transcript(messages), self.timeout
        )
        try:
            data = json.loads(proc.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            if _is_limit(proc.stderr, proc.stdout):
                raise RateLimited(self.name, None, _short(proc.stdout)) from exc
            raise BackendError(f"claude produced unreadable output: {exc}") from exc
        if not isinstance(data, dict):
            raise BackendError("claude output is not a JSON object")
        return self._result_text(data, proc.stderr)

    def _result_text(self, data: dict, stderr: str | None) -> str:
        result = data.get("result")
        if data.get("is_error"):
            if _is_limit(str(result), stderr):
                raise RateLimited(self.name, None, _short(str(result)))
            raise BackendError(f"claude reported an error: {_short(str(result))}")
        if not isinstance(result, str):
            raise BackendError("claude output has no result")
        return result

    def _spawn(self, system: str):
        cmd = [
            *resolve_cli(self.exe), "-p",
            "--model", self.model,
            "--input-format", "stream-json",
            "--output-format", "stream-json", "--verbose",
            "--system-prompt", system,
            "--tools", "",
            "--strict-mcp-config",  # no MCP servers: the owner's claude.ai connectors stay out
        ]
        try:
            return self.popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, encoding="utf-8",
                              errors="replace")
        except FileNotFoundError as exc:
            raise BackendError(f"{self.name} executable not found: {cmd[0]}") from exc
        except OSError as exc:
            raise BackendError(f"{self.name} could not be started: {exc}") from exc

    def _take(self, system: str):
        """The process waiting for ``system``, or a new one if none is alive."""
        with self._lock:
            proc = self._ready.pop(system, None)
        if proc is not None and proc.poll() is None:
            return proc
        return self._spawn(system)

    def _prepare(self, system: str) -> None:
        """Start the process for the next turn with ``system``; evict the least recently used."""
        try:
            proc = self._spawn(system)
        except BackendError:
            return  # the next turn starts one itself and reports the error
        evicted = []
        with self._lock:
            old = self._ready.pop(system, None)
            if old is not None:
                evicted.append(old)
            self._ready[system] = proc
            while len(self._ready) > _WARM_KEEP:
                evicted.append(self._ready.popitem(last=False)[1])
        for p in evicted:
            _kill(p)

    def _complete_warm(self, system: str, messages: list[Message]) -> str:
        proc = self._take(system)
        line = json.dumps({"type": "user",
                           "message": {"role": "user", "content": render_transcript(messages)}})
        try:
            out, err = proc.communicate(line + "\n", timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            _kill(proc)
            raise BackendError(f"{self.name} timed out after {self.timeout}s") from exc
        finally:
            self._prepare(system)
        if proc.returncode != 0:
            if _is_limit(err, out):
                raise RateLimited(self.name, None, _short(err or out))
            raise BackendError(f"{self.name} exited with code {proc.returncode}: "
                               f"{_short(err or out)}")
        result = None
        for raw in (out or "").splitlines():
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                result = event
        if result is None:
            if _is_limit(err, out):
                raise RateLimited(self.name, None, _short(err or out))
            raise BackendError("claude output has no result")
        return self._result_text(result, err)

    def close(self) -> None:
        """Stop the processes waiting for a turn."""
        with self._lock:
            procs = list(self._ready.values())
            self._ready.clear()
        for p in procs:
            _kill(p)


class CodexCLIBackend:
    """Backend that shells out to the ``codex`` CLI."""

    name = "codex"

    def __init__(
        self,
        model: str = "gpt-6-astra",
        timeout: int = 300,
        runner=subprocess.run,
        exe: str = "codex",
        env=None,
        login_check=None,
    ):
        self.model = model
        self.timeout = timeout
        self.runner = runner
        self.exe = exe
        self.env = os.environ if env is None else env
        self.login_check = codex_login_problem if login_check is None else login_check
        self._login_ok = False   # checked once per backend, on the first call

    def _check_subscription(self) -> None:
        if allow_paid(self.env):
            return
        paid = paid_env(CODEX_PAID_ENV, self.env)
        if paid:
            raise PaidUsageBlocked(f"codex may bill an API key ({', '.join(paid)} set); "
                                   "unset it or set IGRIS_ALLOW_PAID=1")
        if not self._login_ok:
            problem = self.login_check(self.exe, self.runner)
            if problem:
                raise PaidUsageBlocked(problem)
            self._login_ok = True

    def complete(self, system: str, messages: list[Message]) -> str:
        self._check_subscription()
        cmd = [
            *resolve_cli(self.exe), "exec",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "-m", self.model,
            "--json", "-",
        ]
        stdin = "SYSTEM:\n" + system + "\n\n" + render_transcript(messages)
        proc = _run_cli(self.name, self.runner, cmd, stdin, self.timeout)
        answer: str | None = None
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            item = event.get("item")
            if (
                event.get("type") == "item.completed"
                and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                answer = item["text"]
        if answer is None:
            if _is_limit(proc.stderr, proc.stdout):
                raise RateLimited(self.name, None, _short(proc.stderr or proc.stdout))
            raise BackendError("codex produced no agent message")
        return answer


class Router:
    """Round-robin router with fallback and rate-limit resting."""

    def __init__(self, backends: list[Backend], clock=time.monotonic):
        if not backends:
            raise ValueError("Router needs at least one backend")
        self.backends = list(backends)
        self.clock = clock
        self._start = 0
        self._rest_until: dict[int, float] = {}

    def complete(self, system: str, messages: list[Message]) -> tuple[str, str]:
        n = len(self.backends)
        start = self._start
        self._start = (start + 1) % n
        errors: list[str] = []
        for offset in range(n):
            idx = (start + offset) % n
            backend = self.backends[idx]
            until = self._rest_until.get(idx)
            if until is not None:
                if self.clock() < until:
                    errors.append(f"{backend.name}: resting after rate limit")
                    continue
                del self._rest_until[idx]
            try:
                text = backend.complete(system, messages)
            except RateLimited as exc:
                self._rest_until[idx] = self.clock() + (exc.retry_after or _DEFAULT_REST)
                errors.append(f"{backend.name}: {exc}")
            except BackendError as exc:
                errors.append(f"{backend.name}: {exc}")
            else:
                return text, backend.name
        raise BackendError("all backends failed: " + "; ".join(errors))


def build_backend(
    name: str, *, claude_model: str, codex_model: str, timeout: int,
    settings=None, budget=None,
) -> Backend:
    """Create a backend by name: ``claude``, ``codex`` or ``echo``."""
    if name == "claude":
        return ClaudeCLIBackend(model=claude_model, timeout=timeout, popen=subprocess.Popen)
    if name == "codex":
        return CodexCLIBackend(model=codex_model, timeout=timeout)
    if name == "opencode":
        if settings is None:
            raise ValueError("backend 'opencode' requires settings")
        from igris.llm_opencode import OpenCodeBackend

        return OpenCodeBackend(model=settings.opencode_model, timeout=timeout)
    if name == "echo":
        return EchoBackend()
    from igris.config import COMPAT_BACKENDS

    if name in COMPAT_BACKENDS:
        if settings is None:
            raise ValueError(f"backend {name!r} requires settings")
        from igris.llm_compat import build_compat

        return build_compat(name, settings, timeout=timeout)
    if name in ("openai", "gemini", "ollama"):
        if settings is None:
            raise ValueError(f"backend {name!r} requires settings")
        if name == "openai":
            from igris.llm_openai import OpenAIAPIBackend

            return OpenAIAPIBackend(
                model=settings.openai_model, budget=budget, timeout=timeout
            )
        if name == "gemini":
            from igris.llm_extra import GeminiBackend

            return GeminiBackend(model=settings.gemini_model, timeout=timeout)
        from igris.llm_extra import OllamaBackend

        return OllamaBackend(
            model=settings.ollama_model, base_url=settings.ollama_url, timeout=timeout
        )
    raise ValueError(f"unknown backend: {name!r}")
