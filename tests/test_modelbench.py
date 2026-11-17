import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

from igris.llm import BackendError, RateLimited
from igris.modelbench import (DOMAINS, BenchStore, Case, CaseResult, format_rank, load_cases,
                              run_benchmark, score_answer)


def case(check, prompt="p", id_="c1"):
    return Case(id_, "reasoning", prompt, check)


class Scripted:
    name = "fake"
    model = "fake-1"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def complete(self, system, messages):
        self.prompts.append(messages[-1].content)
        item = self.answers.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


# --- case files -----------------------------------------------------------------------------

@pytest.mark.parametrize("domain", DOMAINS)
def test_every_domain_has_enough_valid_cases(domain):
    cases = load_cases(domain)
    assert 10 <= len(cases) <= 20
    assert len({c.id for c in cases}) == len(cases)
    for c in cases:
        assert c.check["type"] in {"code", "tool", "facts", "contains", "exact"}


def test_unknown_domain_rejected():
    with pytest.raises(ValueError):
        load_cases("poetry")


def test_haystack_places_needle_at_depth(tmp_path):
    raw = [{"id": "x", "prompt": "Q?", "haystack": {"words": 100, "depth": 0.5, "needle": "NEEDLE"},
            "check": {"type": "contains", "answer": "NEEDLE"}}]
    (tmp_path / "long_context.json").write_text(json.dumps(raw), encoding="utf-8")
    prompt = load_cases("long_context", tmp_path)[0].prompt
    words = prompt.split("\n\n")[0].split()
    assert words.index("NEEDLE") == 50 and len(words) == 101 and prompt.endswith("Q?")


# --- checkers -------------------------------------------------------------------------------

def harness_nonce(cmd) -> str:
    """The per-run nonce the harness prints on its result line."""
    harness = Path(cmd[-1]).read_text(encoding="utf-8")
    return re.search(r"print\('RESULT', '([0-9a-f]+)'", harness).group(1)


def result_runner(template):
    def runner(cmd, **kwargs):
        stdout = template.format(nonce=harness_nonce(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return runner


@pytest.mark.parametrize("stdout,expected", [
    ("RESULT {nonce} 3 3\n", 1.0), ("RESULT {nonce} 2 3\n", 2 / 3), ("", 0.0),
    ("RESULT {nonce} 0 0\n", 0.0),
    # model code printing a result line of its own does not score
    ("RESULT 3 3\n", 0.0), ("RESULT 3 3\nRESULT {nonce} 1 3\n", 1 / 3),
    ("RESULT {nonce} 3 3\nRESULT {nonce} 1 3\n", 1 / 3),  # only the last line counts
    ("RESULT {nonce} 9 9\n", 0.0),  # the total must be the number of tests
])
def test_code_checker_scores_fraction_with_fake_runner(stdout, expected):
    params = {"type": "code", "function": "add", "tests": [[[1, 2], 3], [[2, 2], 4], [[0, 0], 0]]}
    good = "```python\ndef add(a, b):\n    return a + b\n```"

    def runner(cmd, **kwargs):
        harness = Path(cmd[-1]).read_text(encoding="utf-8")
        assert "def add(a, b):\n    return a + b" in harness
        assert "```" not in harness
        assert "if add(*_args) == _want:" in harness
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout.format(nonce=harness_nonce(cmd)))

    with pytest.warns(RuntimeWarning, match="unsandboxed"):
        assert score_answer(case(params), good, runner=runner,
                            env={"IGRIS_BENCH_EXEC": "1"}) == pytest.approx(expected)


def test_code_checker_timeout_scores_zero():
    def runner(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    params = {"type": "code", "function": "f", "tests": [[[], 1]]}
    with pytest.warns(RuntimeWarning, match="unsandboxed"):
        assert score_answer(case(params), "def f(): return 1", runner=runner,
                            env={"IGRIS_BENCH_EXEC": "1"}) == 0.0


def test_code_checker_uses_minimal_environment_and_fresh_directory(monkeypatch):
    seen = {}
    monkeypatch.setenv("ANOTHER_API_KEY", "secret")
    env = {"IGRIS_BENCH_EXEC": "1", "IGRIS_OPENAI_API_KEY": "secret",
           "OPENAI_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
           "PATH": "untrusted", "PYTHONPATH": "untrusted", "SystemRoot": "C:\\Windows"}

    def runner(cmd, **kwargs):
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        cwd = Path(kwargs["cwd"])
        assert cwd.is_dir() and cwd != Path.cwd()
        assert list(cwd.iterdir()) == [Path(cmd[-1])]
        assert kwargs["env"] == {"SystemRoot": "C:\\Windows"}
        assert kwargs["stdin"] == subprocess.DEVNULL
        return result_runner("RESULT {nonce} 1 1\n")(cmd)

    params = {"type": "code", "function": "f", "tests": [[[], 1]]}
    with pytest.warns(RuntimeWarning, match="unsandboxed"):
        assert score_answer(case(params), "def f(): return 1", runner=runner, env=env) == 1.0
    assert seen["cmd"][:2] == [sys.executable, "-I"] and seen["kwargs"]["timeout"] > 0
    assert not Path(seen["kwargs"]["cwd"]).exists()


@pytest.mark.parametrize("value", [None, "", "0", "true", "yes"])
def test_code_checker_disabled_without_explicit_opt_in(monkeypatch, value):
    monkeypatch.delenv("IGRIS_BENCH_EXEC", raising=False)
    if value is not None:
        monkeypatch.setenv("IGRIS_BENCH_EXEC", value)
    params = {"type": "code", "function": "f", "tests": [[[], 1]]}

    def runner(*args, **kwargs):
        pytest.fail("disabled code benchmark invoked runner")

    assert score_answer(case(params), "def f(): return 1", runner=runner) is None
    backend = Scripted()
    result = run_benchmark(backend, [case(params)], runner=runner)[0]
    assert result.score is None and "IGRIS_BENCH_EXEC=1" in result.error
    assert backend.prompts == []


def test_benchmark_uses_injected_env_and_records_unsandboxed_execution(monkeypatch, tmp_path):
    monkeypatch.setenv("IGRIS_BENCH_EXEC", "0")
    backend = Scripted("def f(): return 1")
    backend.env = {"IGRIS_BENCH_EXEC": "1"}
    params = {"type": "code", "function": "f", "tests": [[[], 1]]}
    calls = []

    def runner(cmd, **kwargs):
        calls.append(kwargs)
        return result_runner("RESULT {nonce} 1 1\n")(cmd)

    with pytest.warns(RuntimeWarning, match="unsandboxed"):
        results = run_benchmark(backend, [case(params)], runner=runner)
    assert len(calls) == 1 and calls[0]["env"] == {}
    assert results[0].score == 1.0 and "ran unsandboxed" in results[0].error
    store = BenchStore(tmp_path / "bench.db")
    store.record("fake", "fake", "code", results)
    with store._db() as db:
        assert "ran unsandboxed" in db.execute("SELECT error FROM runs").fetchone()[0]
    # An explicit empty mapping must override both backend and process settings.
    result = run_benchmark(backend, [case(params)], runner=runner, env={})[0]
    assert result.score is None and len(calls) == 1


def test_tool_checker():
    params = {"type": "tool", "name": "get_weather", "args": {"city": "Ankara"}}
    ok = 'Sure: {"name": "get_weather", "arguments": {"city": "Ankara"}}'
    assert score_answer(case(params), ok) == 1.0
    wrong_args = '{"name": "get_weather", "arguments": {"city": "Izmir"}}'
    assert score_answer(case(params), wrong_args) == 0.5
    wrong_tool = '{"name": "calculator", "arguments": {"city": "Ankara"}}'
    assert score_answer(case(params), wrong_tool) == 0.5
    assert score_answer(case(params), "no json here") == 0.0
    as_string = '{"name": "get_weather", "arguments": "{\\"city\\": \\"Ankara\\"}"}'
    assert score_answer(case(params), as_string) == 1.0


def test_facts_checker_required_forbidden_length_and_turkish_folding():
    params = {"type": "facts", "required": ["ankara", "yedi"], "forbidden": [" the "],
              "min_chars": 5, "max_chars": 50}
    assert score_answer(case(params), "ANKARA ve YEDİ") == 1.0
    assert score_answer(case(params), "Ankara burada") == 0.5
    assert score_answer(case(params), "ankara yedi them") == 1.0  # " the " needs spaces both sides
    assert score_answer(case(params), "ankara yedi in the end") == 0.0
    assert score_answer(case(params), "ank") == 0.0
    assert score_answer(case(params), "ankara yedi " + "x" * 60) == 0.0


def test_contains_checker():
    params = {"type": "contains", "answer": "KR-90210"}
    assert score_answer(case(params), "Müşteri numarası kr-90210 olarak kaydedildi") == 1.0
    assert score_answer(case(params), "bilmiyorum") == 0.0


def test_exact_checker_numbers_and_words():
    nums = {"type": "exact", "answers": ["23"]}
    assert score_answer(case(nums), "Cevap: 23") == 1.0
    assert score_answer(case(nums), "Önce 5 sonra 23") == 1.0
    assert score_answer(case(nums), "230") == 0.0
    assert score_answer(case(nums), "bilmiyorum") == 0.0
    decimal = {"type": "exact", "answers": ["7.5"]}
    assert score_answer(case(decimal), "7,5 derece") == 1.0
    word = {"type": "exact", "answers": ["hayır"]}
    assert score_answer(case(word), "Hayır.") == 1.0
    assert score_answer(case(word), "evet") == 0.0


# --- running --------------------------------------------------------------------------------

def test_run_records_scores_latency_and_error_without_scoring_zero():
    ticks = iter(range(0, 100, 2))
    backend = Scripted("23", BackendError("boom"))
    cases = [case({"type": "exact", "answers": ["23"]}, id_="a"),
             case({"type": "exact", "answers": ["23"]}, id_="b")]
    results = run_benchmark(backend, cases, clock=lambda: next(ticks), sleep=lambda s: None)
    assert [r.case_id for r in results] == ["a", "b"]
    assert results[0].score == 1.0 and results[0].latency == 2
    assert results[1].score is None and "boom" in results[1].error


def test_rate_limit_pauses_then_resumes_same_case():
    sleeps = []
    backend = Scripted(RateLimited("fake", 30), RateLimited("fake", None), "23")
    results = run_benchmark(backend, [case({"type": "exact", "answers": ["23"]})],
                            sleep=sleeps.append)
    assert results[0].score == 1.0
    assert sleeps == [30, 60.0]
    assert len(backend.prompts) == 3


def test_rate_limit_wait_is_capped_and_gives_up_unscored():
    sleeps = []
    backend = Scripted(*[RateLimited("fake", 9999)] * 4)
    results = run_benchmark(backend, [case({"type": "exact", "answers": ["1"]})],
                            sleep=sleeps.append, max_wait=120, retries=3)
    assert results[0].score is None
    assert sleeps == [120, 120, 120]


# --- store and rank -------------------------------------------------------------------------

class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def results(score, n=8, latency=1.0):
    return [CaseResult(f"c{i}", score, latency) for i in range(n)]


def test_rank_orders_by_score_then_latency(tmp_path):
    store = BenchStore(tmp_path / "b.sqlite3", Clock())
    store.record("groq", "m-fast", "code", results(0.8, latency=1.0))
    store.record("gemini", "m-slow", "code", results(0.8, latency=3.0))
    store.record("mistral", "m-best", "code", results(0.9, latency=9.0))
    rows = store.rank("code")
    assert [r.model for r in rows] == ["m-best", "m-fast", "m-slow"]
    assert rows[0].cases == 8


def test_rank_needs_minimum_scored_cases(tmp_path):
    store = BenchStore(tmp_path / "b.sqlite3", Clock())
    store.record("groq", "few", "code", results(1.0, n=7))
    store.record("groq", "enough", "code", results(0.1, n=8))
    unscored = [CaseResult(f"c{i}", None, 1.0, "limit") for i in range(8)]
    store.record("groq", "errors", "code", unscored)
    assert [r.model for r in store.rank("code")] == ["enough"]
    assert store.rank("code", min_cases=5)[0].model == "few"


def test_rank_ignores_old_results_and_other_domains(tmp_path):
    clock = Clock()
    store = BenchStore(tmp_path / "b.sqlite3", clock)
    store.record("groq", "old", "code", results(1.0))
    store.record("groq", "other", "reasoning", results(1.0))
    clock.now += 31 * 86400
    store.record("groq", "new", "code", results(0.5))
    assert [r.model for r in store.rank("code")] == ["new"]


def test_rank_persists_across_store_instances(tmp_path):
    path = tmp_path / "b.sqlite3"
    BenchStore(path, Clock()).record("groq", "m", "code", results(0.7))
    assert BenchStore(path, Clock()).rank("code")[0].score == pytest.approx(0.7)


def test_format_rank():
    assert "not enough" in format_rank("code", [])
    store_rows = []
    from igris.modelbench import RankRow

    store_rows.append(RankRow("groq", "llama", 0.85, 1.234, 10))
    text = format_rank("code", store_rows)
    assert "llama" in text and "0.85" in text
