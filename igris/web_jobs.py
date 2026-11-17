"""Web routes for background work: each user's task queue, the owner's multiagent jobs,
and the Models panel with its benchmark action.

Route functions take the request handler ``h`` and the server context ``ctx``.
Depends on tasks, multiagent_jobs and llm (imported where they are used).
"""

import re
import threading
import time
from pathlib import Path

from igris.web_http import MAX_TEXT

DOMAINS = ("code", "turkish_chat", "summarization", "tool_use", "long_context", "reasoning")


def tasks(h, ctx, path, data=None):
    queue = ctx.tasks
    if ctx.accounts is None or queue is None:
        return h._error(404, "not found")
    from igris.tasks import TaskError, TaskMissing

    owner = h.login.user.id
    try:
        if data is None:
            if path == "/api/tasks":
                return h._json(200, {"tasks": queue.list(owner),
                                     "max_pending": queue.max_pending,
                                     "max_workers": queue.max_workers})
            match = re.fullmatch(r"/api/tasks/([a-f0-9]{32})", path)
            if match:
                return h._json(200, queue.get(owner, match[1]))
        elif path == "/api/tasks/submit":
            return h._json(202, queue.submit(owner, data.get("prompt")))
        elif path == "/api/tasks/cancel":
            return h._json(200, queue.cancel(owner, data.get("id")))
        elif path == "/api/tasks/resume":
            return h._json(200, queue.resume(owner, data.get("id"), data.get("answer")))
    except TaskMissing as exc:
        return h._error(404, str(exc))
    except TaskError as exc:
        return h._error(400, str(exc))
    return h._error(404, "not found")


def ma_root(ctx, assistant):
    if ctx.multiagent_dir is not None:
        return Path(ctx.multiagent_dir)
    configured = getattr(getattr(assistant, "settings", None), "multiagent_dir", None)
    return Path(configured) if configured else None


def workspace(assistant):
    return getattr(getattr(assistant, "settings", None), "workspace", None)


def multiagent(h, ctx, path):
    """GET /api/multiagent/jobs: readiness checks and the owner's jobs."""
    if path != "/api/multiagent/jobs":
        return h._error(404, "not found")
    from igris.multiagent_jobs import readiness

    jobs = ctx.jobs
    checks = readiness(ma_root(ctx, h.assistant), workspace(h.assistant))
    if jobs is None:
        return h._json(200, {"enabled": False, "checks": checks, "max_parallel": 1, "jobs": []})
    items = [j.to_dict() for j in jobs.jobs()]
    return h._json(200, {"enabled": True, "checks": checks,
                         "max_parallel": jobs.max_parallel,
                         "implement_ready": workspace(h.assistant) is not None,
                         "jobs": items})


def submit(h, ctx, path, data):
    jobs = ctx.jobs
    if jobs is None:
        return h._error(404, "not found")
    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return h._error(400, "goal must be a non-empty string")
    if len(goal) > MAX_TEXT:
        return h._error(400, f"goal exceeds {MAX_TEXT} characters")
    flags = {}
    for key in ("parallel", "implement"):
        value = data.get(key, False)
        if not isinstance(value, bool):
            return h._error(400, f"{key} must be a boolean")
        flags[key] = value
    test_cmd = data.get("test")
    if test_cmd is not None and not isinstance(test_cmd, str):
        return h._error(400, "test must be a string")
    from igris.llm import PaidUsageBlocked

    # same context as chat-routed jobs: the assistant's workspace setting
    context = workspace(h.assistant)
    if flags["implement"]:
        if context is None:
            return h._error(400, "changing code needs IGRIS_WORKSPACE (the repository to change)")
        if not (test_cmd or "").strip():
            return h._error(400, "changing code needs a test command")
        flags["test_cmd"] = test_cmd.strip()
    try:
        payload = jobs.submit(goal, context=context, **flags).to_dict()
    except PaidUsageBlocked as exc:
        return h._error(409, str(exc))
    except ValueError as exc:
        return h._error(400, str(exc))
    except Exception as exc:
        return h._failure(exc)
    h._json(200, payload)


def cancel(h, ctx, path, data):
    if ctx.jobs is None:
        return h._error(404, "not found")
    job_id = data.get("id")
    if not isinstance(job_id, str) or not job_id:
        return h._error(400, "id must be a non-empty string")
    try:
        done = bool(ctx.jobs.cancel(job_id))
    except Exception as exc:
        return h._failure(exc)
    h._json(200, {"cancelled": done})


class WorkerModels:
    """multiagent's worker chain for the Models panel, read at most once a minute (it
    needs a subprocess); None when multiagent is off."""

    def __init__(self, jobs, clock=time.monotonic):
        self.jobs, self.clock = jobs, clock
        self._at = self._data = None
        self._lock = threading.Lock()

    def __call__(self) -> dict | None:
        if self.jobs is None or not callable(getattr(self.jobs, "worker_models", None)):
            return None
        with self._lock:
            now = self.clock()
            if self._at is None or now - self._at > 60:
                try:
                    self._data = self.jobs.worker_models()
                except Exception as exc:
                    self._data = {"error": str(exc)[:300]}
                self._at = now
            return self._data


def model_list(ctx, assistant) -> dict:
    """Read currently configured backends; unknown quota is explicitly null."""
    if ctx.models_loader is not None:
        return ctx.models_loader(assistant)
    providers = []
    seen = set()
    ranks = {}
    routers = (getattr(assistant, "router", None), getattr(assistant, "slow_router", None))
    for router in routers:
        ranker = getattr(router, "ranker", None)
        if ranker is None:
            continue
        for domain in DOMAINS:
            for place, item in enumerate(ranker(domain), 1):
                name = (item.provider if hasattr(item, "provider") else
                        item[0] if isinstance(item, (tuple, list)) else item)
                ranks.setdefault(name, {})[domain] = place
    for router in routers:
        if router is None:
            continue
        for idx, backend in enumerate(router.backends):
            if backend.name in seen:
                continue
            seen.add(backend.name)
            provider = getattr(backend, "provider", None)
            quota = getattr(backend, "quota", None)
            used = getattr(quota, "used_today", None)
            if callable(used):
                used = used()
            if quota is not None and callable(getattr(quota, "used", None)):
                used = quota.used(backend.name)
            providers.append({
                "name": backend.name,
                "models": [getattr(backend, "model", getattr(provider, "default_model", ""))],
                "ranks": ranks.get(backend.name, {}),
                "requests_used_today": used if isinstance(used, int) else None,
                "daily_limit": getattr(provider, "requests_per_day", None),
                "health": (router.health().get(backend.name, {}).get("state")
                           if callable(getattr(router, "health", None)) else
                           "resting" if router._rest_until.get(idx, 0) > router.clock()
                           else "available"),
                "trains_on_data": backend.name == "gemini" or bool(
                    getattr(provider, "trains_on_data", False)),
            })
    return {"domains": list(DOMAINS), "providers": providers,
            "benchmark_available": ctx.benchmark_runner is not None,
            "decision": getattr(getattr(assistant, "router", None), "last_decision", None)}


def models(h, ctx, path):
    payload = model_list(ctx, h.assistant)
    if h._is_admin():  # multiagent is owner-only, like its tab
        payload = dict(payload, multiagent=ctx.worker_models())
    return h._json(200, payload)


def benchmark(h, ctx, path, data):
    if ctx.accounts is None or h.login.user.role != "admin":
        return h._error(403, "Administrator access required")
    if ctx.benchmark_runner is None:
        return h._error(503, "Benchmark runner is not configured")
    domain = data.get("domain")
    if domain not in DOMAINS:
        return h._error(400, "Invalid benchmark domain")
    return h._json(200, ctx.benchmark_runner(domain))
