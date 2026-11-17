"""Privacy gate and health-aware fallback for backends that may train on request data."""

from __future__ import annotations

import time

from igris.llm import BackendError, Message, RateLimited, Router
from igris.routing import classify_domain, order_free_backends

__all__ = ["GatedRouter", "request_is_private", "trains_on_data"]

_DEFAULT_REST = 60.0


def trains_on_data(backend) -> bool:
    """True when the provider may use request content for model training."""
    if backend.name == "gemini":
        return True
    flag = getattr(backend, "trains_on_data", None)
    if flag is None:
        flag = getattr(getattr(backend, "provider", None), "trains_on_data", False)
    return bool(flag)


def request_is_private(*, has_attachments: bool = False, uses_workspace: bool = False,
                       uses_memory: bool = False, user_context: bool = False) -> bool:
    """A request is private unless it is a plain general question typed by the user."""
    return has_attachments or uses_workspace or uses_memory or user_context


class GatedRouter(Router):
    """Router that keeps private requests away from training providers and skips sick backends.

    ``complete`` defaults to ``private=True`` so callers that do not know about the gate fail
    closed. A backend that fails ``fail_limit`` times within ``window`` seconds is skipped for
    ``cooldown`` seconds.
    """

    def __init__(self, backends, clock=time.monotonic, *, allow_training: bool = False,
                 fail_limit: int = 3, window: float = 600.0, cooldown: float = 600.0,
                 ranker=None):
        super().__init__(backends, clock)
        self.ranker = ranker
        self.allow_training = allow_training
        self.fail_limit = fail_limit
        self.window = window
        self.cooldown = cooldown
        self._failures: dict[int, list[float]] = {}
        self._skip_until: dict[int, float] = {}

    def _eligible(self, private: bool) -> list[int]:
        if not private or self.allow_training:
            return list(range(len(self.backends)))
        return [i for i, b in enumerate(self.backends) if not trains_on_data(b)]

    def _record_failure(self, idx: int) -> None:
        now = self.clock()
        hits = [t for t in self._failures.get(idx, []) if now - t < self.window]
        hits.append(now)
        if len(hits) >= self.fail_limit:
            self._skip_until[idx] = now + self.cooldown
            hits = []
        self._failures[idx] = hits

    def complete(self, system: str, messages: list[Message], *,
                 private: bool = True) -> tuple[str, str]:
        eligible = self._eligible(private)
        prompt = next((m.content for m in reversed(messages) if m.role == "user"), "")
        domain = classify_domain(prompt)
        ranked = list(self.ranker(domain)) if self.ranker is not None else []
        if ranked:
            order = order_free_backends([b.name for b in self.backends],
                                        [row.provider if hasattr(row, "provider") else row
                                         for row in ranked],
                                        training={b.name for b in self.backends if trains_on_data(b)},
                                        private=private, allow_training=self.allow_training)
            eligible = [i for name in order for i in eligible if self.backends[i].name == name]
        self.last_decision = {"domain": domain,
                              "ranked": [row.provider if hasattr(row, "provider") else row
                                         for row in ranked], "chosen": None}
        if not eligible:
            raise BackendError(
                "no backend may receive private content: every configured backend may train "
                "on request data (set IGRIS_ALLOW_TRAINING_PROVIDERS=1 to allow it)")
        n = len(eligible)
        start = self._start % n
        self._start = (self._start + 1) % len(self.backends)
        errors: list[str] = []
        for offset in range(n):
            idx = eligible[offset if ranked else (start + offset) % n]
            backend = self.backends[idx]
            now = self.clock()
            until = self._rest_until.get(idx)
            if until is not None:
                if now < until:
                    errors.append(f"{backend.name}: resting after rate limit")
                    continue
                del self._rest_until[idx]
            until = self._skip_until.get(idx)
            if until is not None:
                if now < until:
                    errors.append(f"{backend.name}: skipped after repeated failures")
                    continue
                del self._skip_until[idx]
            try:
                text = backend.complete(system, messages)
            except RateLimited as exc:
                self._rest_until[idx] = self.clock() + (exc.retry_after or _DEFAULT_REST)
                self._record_failure(idx)
                errors.append(f"{backend.name}: {exc}")
            except BackendError as exc:
                self._record_failure(idx)
                errors.append(f"{backend.name}: {exc}")
            else:
                self._failures.pop(idx, None)
                self.last_decision["chosen"] = backend.name
                return text, backend.name
        raise BackendError("all backends failed: " + "; ".join(errors))

    def health(self) -> dict[str, dict]:
        """Per-backend status for the UI: ok, resting or skipped."""
        now = self.clock()
        report: dict[str, dict] = {}
        for idx, backend in enumerate(self.backends):
            if self._skip_until.get(idx, 0) > now:
                state = "skipped"
            elif self._rest_until.get(idx, 0) > now:
                state = "resting"
            else:
                state = "ok"
            report[backend.name] = {"state": state, "trains_on_data": trains_on_data(backend),
                                    "recent_failures": len(self._failures.get(idx, []))}
        return report
