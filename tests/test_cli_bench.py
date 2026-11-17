import io

from igris.cli import main
from igris.modelbench import BenchStore


class Answering:
    """Answers every prompt with a fixed string; counts calls."""

    model = "fake-1"

    def __init__(self, answer="23", quota=None):
        self.answer = answer
        self.calls = 0
        if quota is not None:
            self.quota_remaining = lambda: quota

    def complete(self, system, messages):
        self.calls += 1
        return self.answer


def run(argv, factory, monkeypatch, tmp_path):
    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IGRIS_BACKENDS", "echo")
    out = io.StringIO()
    code = main(argv, stdout=out, bench_factory=lambda name, settings: factory)
    return code, out.getvalue()


def test_bench_runs_one_domain_and_prints_rank(monkeypatch, tmp_path):
    backend = Answering("5")
    code, text = run(["--bench", "--domain", "reasoning", "--provider", "echo"], backend,
                     monkeypatch, tmp_path)
    assert code == 0
    assert "echo/reasoning:" in text and "scored" in text
    assert backend.calls >= 10
    assert "reasoning:" in text


def test_bench_results_are_stored(monkeypatch, tmp_path):
    run(["--bench", "--domain", "reasoning", "--provider", "echo"], Answering("5"),
        monkeypatch, tmp_path)
    rows = BenchStore(tmp_path / "bench.sqlite3").rank("reasoning")
    assert len(rows) == 1 and rows[0].model == "fake-1" and rows[0].provider == "echo"


def test_bench_refuses_when_quota_too_small(monkeypatch, tmp_path):
    backend = Answering(quota=3)
    code, text = run(["--bench", "--domain", "reasoning", "--provider", "echo"], backend,
                     monkeypatch, tmp_path)
    assert code == 1 and "skipped" in text and "quota" in text
    assert backend.calls == 0


def test_bench_unknown_domain(monkeypatch, tmp_path):
    code, text = run(["--bench", "--domain", "poetry"], Answering(), monkeypatch, tmp_path)
    assert code == 1 and "unknown domain" in text


def test_bench_backend_build_failure_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IGRIS_BACKENDS", "echo")

    def boom(name, settings):
        raise RuntimeError("no key")

    out = io.StringIO()
    code = main(["--bench", "--domain", "reasoning", "--provider", "echo"], stdout=out,
                bench_factory=boom)
    assert code == 1 and "no key" in out.getvalue()


class Named(Answering):
    name = "opencode"
    model = "glm-5.3-flash-free"


def test_bench_model_requires_provider(monkeypatch, tmp_path):
    code, text = run(["--bench", "--model", "glm-5.3-flash-free"], Named("5"), monkeypatch,
                     tmp_path)
    assert code == 1 and "--model needs --provider" in text


def test_bench_model_stores_backend_model_under_opencode(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    out = io.StringIO()

    def factory(name, settings):
        seen.append(settings.opencode_model)
        return Named("5")

    code = main(["--bench", "--domain", "reasoning", "--provider", "opencode", "--model",
                 "glm-5.3-flash-free", "--yes"], stdout=out, bench_factory=factory)
    assert code == 0 and seen == ["glm-5.3-flash-free"]
    rows = BenchStore(tmp_path / "bench.sqlite3").rank("reasoning")
    assert rows[0].provider == "opencode" and rows[0].model == "glm-5.3-flash-free"
    assert "will send" in out.getvalue()


def test_bench_asks_before_sending_to_quota_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    backend = Named("5")
    out = io.StringIO()
    code = main(["--bench", "--domain", "reasoning", "--provider", "opencode"], stdout=out,
                stdin=io.StringIO("n\n"), bench_factory=lambda n, s: backend)
    assert code == 1 and backend.calls == 0 and "requests" in out.getvalue()
    assert "aborted" in out.getvalue()


def test_bench_no_confirmation_without_quota(monkeypatch, tmp_path):
    code, text = run(["--bench", "--domain", "reasoning", "--provider", "echo"],
                     Answering("5"), monkeypatch, tmp_path)
    assert code == 0 and "Continue?" not in text


def test_bench_code_cases_disabled_are_explained_and_not_counted(monkeypatch, tmp_path):
    monkeypatch.delenv("IGRIS_BENCH_EXEC", raising=False)
    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    backend = Named("5")
    out = io.StringIO()
    code = main(["--bench", "--domain", "code", "--provider", "opencode", "--yes"], stdout=out,
                bench_factory=lambda n, s: backend)
    text = out.getvalue()
    assert code == 0 and backend.calls == 0
    assert "will send 0 requests" in text
    assert "code case(s) skipped" in text and "IGRIS_BENCH_EXEC=1" in text


def test_bench_quota_counts_only_requests_that_are_sent(monkeypatch, tmp_path):
    monkeypatch.delenv("IGRIS_BENCH_EXEC", raising=False)
    backend = Answering("5", quota=1)
    code, text = run(["--bench", "--domain", "code", "--provider", "echo", "--yes"], backend,
                     monkeypatch, tmp_path)
    assert "needs" not in text and "will send 0 requests" in text


def test_bench_code_cases_count_when_execution_is_on(monkeypatch, tmp_path):
    from igris.modelbench import load_cases, model_requests

    cases = load_cases("code")
    assert model_requests(None, cases, env={}) == 0
    assert model_requests(None, cases, env={"IGRIS_BENCH_EXEC": "1"}) == len(cases)
    reasoning = load_cases("reasoning")
    assert model_requests(None, reasoning, env={}) == len(reasoning)
    assert model_requests(type("B", (), {"env": {"IGRIS_BENCH_EXEC": "1"}})(), cases) == len(cases)
