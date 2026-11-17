"""web_jobs routes and helpers with fakes; HTTP flows are in test_web."""
from types import SimpleNamespace

from igris import web_jobs
from web_serving import FakeRequest, login


def ctx(**kw):
    base = dict(accounts=None, tasks=None, jobs=None, multiagent_dir=None,
                models_loader=None, benchmark_runner=None, worker_models=lambda: None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_tasks_need_accounts_and_a_queue():
    h = FakeRequest(login=login())
    web_jobs.tasks(h, ctx(), "/api/tasks")
    assert h.status == 404


def test_only_the_jobs_view_remains_under_multiagent():
    h = FakeRequest(assistant=SimpleNamespace(settings=SimpleNamespace()))
    for path in ("/api/multiagent/runs", "/api/multiagent/live", "/api/multiagent/run/1"):
        web_jobs.multiagent(h, ctx(), path)
        assert h.status == 404, path


def test_submit_and_cancel_without_jobs_are_missing():
    h = FakeRequest()
    web_jobs.submit(h, ctx(), "/api/multiagent/jobs", {"goal": "g"})
    assert h.status == 404
    web_jobs.cancel(h, ctx(), "/api/multiagent/cancel", {"id": "j"})
    assert h.status == 404


def test_worker_models_are_read_at_most_once_a_minute():
    now = [0.0]
    jobs = SimpleNamespace(calls=0)

    def worker_models():
        jobs.calls += 1
        if jobs.calls == 2:
            raise RuntimeError("x" * 400)
        return {"zincir": []}

    jobs.worker_models = worker_models
    cache = web_jobs.WorkerModels(jobs, clock=lambda: now[0])
    assert cache() == {"zincir": []} and cache() == {"zincir": []} and jobs.calls == 1
    now[0] = 61
    assert cache() == {"error": "x" * 300}
    assert web_jobs.WorkerModels(None)() is None


def test_benchmark_is_owner_only_and_checks_the_domain():
    h = FakeRequest(login=login(role="user"))
    web_jobs.benchmark(h, ctx(accounts=object()), "/api/models/benchmark", {"domain": "code"})
    assert h.status == 403
    h = FakeRequest(login=login())
    web_jobs.benchmark(h, ctx(accounts=object()), "/api/models/benchmark", {"domain": "code"})
    assert h.status == 503
    c = ctx(accounts=object(), benchmark_runner=lambda d: {"ran": d})
    web_jobs.benchmark(h, c, "/api/models/benchmark", {"domain": "x"})
    assert h.status == 400
    web_jobs.benchmark(h, c, "/api/models/benchmark", {"domain": "code"})
    assert (h.status, h.body) == (200, {"ran": "code"})


def test_models_add_the_worker_chain_for_the_owner_only():
    c = ctx(models_loader=lambda a: {"providers": []}, worker_models=lambda: {"zincir": []})
    h = FakeRequest()
    web_jobs.models(h, c, "/api/models")
    assert h.body == {"providers": [], "multiagent": {"zincir": []}}
    h = FakeRequest(login=login(role="user"))
    web_jobs.models(h, c, "/api/models")
    assert h.body == {"providers": []}
