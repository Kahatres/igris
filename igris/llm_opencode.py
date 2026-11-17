"""Backend that shells out to the ``opencode`` CLI, restricted to OpenCode Zen's free models."""

from __future__ import annotations

import json
import subprocess
import tempfile

from igris import llm
from igris.llm import (BackendError, Message, PaidUsageBlocked, RateLimited, render_transcript)

__all__ = ["FREE_MODELS", "OpenCodeBackend"]

# Free Zen models and whether the free period may train on prompts, from
# opencode.ai/docs/zen (checked 2026-10-31). Free chat models only work inside OpenCode,
# and the free period is temporary, so this is an allowlist: a name that merely ends in
# "-free" is not trusted, and "big-pickle" is free without that suffix.
FREE_MODELS: dict[str, bool] = {
    "deepseek-v4.1-flash-free": False,   # zero retention, not used for training
    "glm-5.3-flash-free": False,         # zero retention, not used for training
    "beam-free": False,                  # no training, kept 30 days, many countries blocked
    "big-pickle": True,
    "mimo-v2.6-flash-free": True,
    "flint-40b-free": True,
    "nemotron-3.5-lightning-free": True,  # trial use, logged to improve NVIDIA products
    "nemotron-3.5-super-vl-free": True,
}


class OpenCodeBackend:
    """``opencode run`` in an empty temporary folder; tools are never auto-approved."""

    name = "opencode"

    def __init__(self, model: str = "deepseek-v4.1-flash-free", timeout: int = 300,
                 runner=subprocess.run, exe: str = "opencode"):
        if model not in FREE_MODELS:
            raise ValueError(f"opencode model {model!r} is not a known free model; "
                             f"allowed: {', '.join(sorted(FREE_MODELS))}")
        self.model = model
        self.timeout = timeout
        self.runner = runner
        self.exe = exe
        self._workdir: tempfile.TemporaryDirectory | None = None

    @property
    def trains_on_data(self) -> bool:
        return FREE_MODELS[self.model]

    def _run_in_workdir(self, cmd, **kwargs):
        if self._workdir is None:
            self._workdir = tempfile.TemporaryDirectory(prefix="igris-opencode-")
        return self.runner(cmd, cwd=self._workdir.name, **kwargs)

    def complete(self, system: str, messages: list[Message]) -> str:
        cmd = [*llm.resolve_cli(self.exe), "run", "-m", f"opencode/{self.model}",
               "--format", "json"]
        stdin = "SYSTEM:\n" + system + "\n\n" + render_transcript(messages)
        proc = llm._run_cli(self.name, self._run_in_workdir, cmd, stdin, self.timeout)
        texts: list[str] = []
        cost = 0.0
        for line in (proc.stdout or "").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if kind == "error":
                err = event.get("error")
                err = err if isinstance(err, dict) else {}
                detail = llm._short(str(err.get("message") or err.get("type") or event))
                if llm._is_limit(detail) or "429" in detail:
                    raise RateLimited(self.name, None, detail)
                raise BackendError(f"opencode error: {detail}")
            if kind == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif kind == "step_finish" and isinstance(part.get("cost"), (int, float)):
                cost += part["cost"]
        if cost > 0:
            raise PaidUsageBlocked(
                f"opencode reported a cost of {cost} for a free model; check your Zen credits")
        if not texts:
            raise BackendError("opencode produced no text")
        return "".join(texts)
