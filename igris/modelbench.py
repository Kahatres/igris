"""Domain benchmark for models: automatic checkers, SQLite results and per-domain ranks.

Manual use only: real runs call real providers and are never part of the test suite.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass
from pathlib import Path

from igris.llm import BackendError, Message, RateLimited
from igris.textfold import fold_ascii as _fold

__all__ = ["DOMAINS", "Case", "CaseResult", "BenchStore", "load_cases", "run_benchmark",
           "score_answer", "format_rank"]

DOMAINS = ("code", "turkish_chat", "summarization", "tool_use", "long_context", "reasoning")
CASES_DIR = Path(__file__).with_name("bench_cases")
MIN_CASES = 8
MAX_AGE_DAYS = 30


@dataclass(frozen=True)
class Case:
    id: str
    domain: str
    prompt: str
    check: dict
    system: str = ""


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    score: float | None  # None: disabled or unavailable, not a zero
    latency: float
    error: str = ""


def _expand(raw: dict, domain: str) -> Case:
    prompt = raw["prompt"]
    hay = raw.get("haystack")
    if hay:
        filler = ("Sunucu günlüğü kaydı: rutin bakım penceresi tamamlandı, bir sorun görülmedi. "
                  .split())
        words = [filler[i % len(filler)] for i in range(int(hay["words"]))]
        words.insert(int(len(words) * float(hay["depth"])), hay["needle"])
        prompt = " ".join(words) + "\n\n" + prompt
    return Case(raw["id"], domain, prompt, raw["check"], raw.get("system", ""))


def load_cases(domain: str, directory: Path | None = None) -> list[Case]:
    if domain not in DOMAINS:
        raise ValueError(f"unknown domain {domain!r}; choose from {', '.join(DOMAINS)}")
    path = (directory or CASES_DIR) / f"{domain}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [_expand(item, domain) for item in data]


# --- checkers -------------------------------------------------------------------------------

EXEC_DISABLED = "Code execution disabled; set IGRIS_BENCH_EXEC=1 to run unsandboxed."
_EXEC_NOTICE = "Code ran unsandboxed (IGRIS_BENCH_EXEC=1); no OS sandbox is available."
_FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_code(answer: str) -> str:
    blocks = _FENCE.findall(answer)
    return "\n".join(blocks) if blocks else answer


def _check_code(answer: str, params: dict, runner=subprocess.run, timeout: float = 10.0,
                *, env=None) -> float | None:
    environ = os.environ if env is None else env
    if environ.get("IGRIS_BENCH_EXEC") != "1":
        return None
    warnings.warn("Executing model-generated code unsandboxed (IGRIS_BENCH_EXEC=1). "
                  "It can access the host filesystem, network and processes.",
                  RuntimeWarning, stacklevel=2)
    tests = params["tests"]
    # a fresh nonce marks the harness's own result line, so model code that prints
    # "RESULT 9 9" itself does not score
    nonce = secrets.token_hex(16)
    harness = (
        _extract_code(answer) + "\n\nimport json as _j\n"
        f"_tests = _j.loads({json.dumps(json.dumps(tests))})\n"
        "_ok = 0\n"
        "for _args, _want in _tests:\n"
        "    try:\n"
        f"        if {params['function']}(*_args) == _want:\n"
        "            _ok += 1\n"
        "    except Exception:\n"
        "        pass\n"
        f"print('RESULT', {nonce!r}, _ok, len(_tests))\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "case.py"
        script.write_text(harness, encoding="utf-8")
        # Windows needs SystemRoot. No home, PATH, provider credentials or IGRIS settings.
        child_env = {key: value for key, value in environ.items()
                     if key.casefold() == "systemroot"}
        try:
            proc = runner([sys.executable, "-I", str(script)], capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=timeout, cwd=tmp, env=child_env)
        except subprocess.TimeoutExpired:
            return 0.0
    found = re.findall(r"RESULT " + nonce + r" (\d+) (\d+)", proc.stdout or "")
    if not found:
        return 0.0
    passed, total = (int(n) for n in found[-1])  # the harness prints last
    if total == 0 or total != len(tests) or passed > total:
        return 0.0
    return passed / total


def _find_json_objects(text: str):
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


def _check_tool(answer: str, params: dict, **_) -> float:
    for obj in _find_json_objects(answer):
        name = obj.get("name") or obj.get("tool")
        args = obj.get("arguments", obj.get("args", {}))
        if name is None:
            continue
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        score = 0.5 if name == params["name"] else 0.0
        wanted = params.get("args", {})
        if isinstance(args, dict) and all(args.get(k) == v for k, v in wanted.items()):
            score += 0.5
        return score
    return 0.0


def _check_facts(answer: str, params: dict, **_) -> float:
    folded = _fold(answer)
    required = params.get("required", [])
    if any(_fold(w) in folded for w in params.get("forbidden", [])):
        return 0.0
    if not (params.get("min_chars", 0) <= len(answer.strip()) <= params.get("max_chars", 10**9)):
        return 0.0
    if not required:
        return 1.0
    return sum(1 for w in required if _fold(w) in folded) / len(required)


def _check_contains(answer: str, params: dict, **_) -> float:
    return 1.0 if _fold(params["answer"]) in _fold(answer) else 0.0


def _numbers(text: str) -> list[str]:
    return [m.replace(",", ".").rstrip(".") for m in re.findall(r"-?\d+(?:[.,]\d+)?", text)]


def _check_exact(answer: str, params: dict, **_) -> float:
    wanted = [str(a) for a in params["answers"]]
    nums = _numbers(answer)
    last = nums[-1] if nums else _fold(answer).strip().strip(".")
    for want in wanted:
        want_n = _numbers(want)
        if want_n and nums and want_n[0] == last:
            return 1.0
        if not want_n and _fold(want) == _fold(answer).strip().strip("."):
            return 1.0
    return 0.0


CHECKERS = {"code": _check_code, "tool": _check_tool, "facts": _check_facts,
            "contains": _check_contains, "exact": _check_exact}


def score_answer(case: Case, answer: str, *, runner=subprocess.run, env=None) -> float | None:
    checker = CHECKERS[case.check["type"]]
    params = {k: v for k, v in case.check.items() if k != "type"}
    if case.check["type"] == "code":
        return checker(answer, params, runner=runner, env=env)
    return checker(answer, params)


# --- running --------------------------------------------------------------------------------

def _bench_env(backend, env):
    if env is None:
        env = getattr(backend, "env", None)
    return dict(os.environ if env is None else env)


def model_requests(backend, cases: list[Case], env=None) -> int:
    """How many of ``cases`` ``run_benchmark`` sends to the model: code cases are skipped
    unless IGRIS_BENCH_EXEC=1 (read like ``run_benchmark`` does)."""
    environ = _bench_env(backend, env)
    if environ.get("IGRIS_BENCH_EXEC") == "1":
        return len(cases)
    return sum(1 for case in cases if case.check["type"] != "code")


def run_benchmark(backend, cases: list[Case], *, clock=time.monotonic, sleep=time.sleep,
                  runner=subprocess.run, max_wait: float = 300.0,
                  retries: int = 3, env=None) -> list[CaseResult]:
    """Run every case; a rate limit pauses and retries the same case instead of scoring zero."""
    results: list[CaseResult] = []
    environ = _bench_env(backend, env)
    for case in cases:
        executable = case.check["type"] == "code"
        if executable and environ.get("IGRIS_BENCH_EXEC") != "1":
            results.append(CaseResult(case.id, None, 0.0, EXEC_DISABLED))
            continue
        messages = [Message("user", case.prompt)]
        answer, error, started = None, "", clock()
        for attempt in range(retries + 1):
            try:
                answer = backend.complete(case.system, messages)
                break
            except RateLimited as exc:
                error = str(exc)
                if attempt == retries:
                    break
                sleep(min(exc.retry_after or 60.0, max_wait))
            except BackendError as exc:
                error = str(exc)
                break
        latency = clock() - started
        if answer is None:
            results.append(CaseResult(case.id, None, latency, error))
        else:
            score = score_answer(case, answer, runner=runner, env=environ)
            results.append(CaseResult(case.id, score, latency,
                                      _EXEC_NOTICE if executable else ""))
    return results


# --- storage and ranks ----------------------------------------------------------------------

@dataclass(frozen=True)
class RankRow:
    provider: str
    model: str
    score: float
    latency: float
    cases: int


class BenchStore:
    def __init__(self, path: Path | str, clock=time.time):
        self.path = str(path)
        self.clock = clock
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (provider TEXT NOT NULL, model TEXT NOT NULL,"
                       " domain TEXT NOT NULL, case_id TEXT NOT NULL, score REAL, latency REAL,"
                       " error TEXT NOT NULL DEFAULT '', run_at REAL NOT NULL)")

    def _db(self):
        db = sqlite3.connect(self.path)
        return _Closing(db)

    def record(self, provider: str, model: str, domain: str, results: list[CaseResult]) -> None:
        now = self.clock()
        with self._db() as db:
            db.executemany(
                "INSERT INTO runs VALUES (?,?,?,?,?,?,?,?)",
                [(provider, model, domain, r.case_id, r.score, r.latency, r.error, now)
                 for r in results])

    def rank(self, domain: str, *, min_cases: int = MIN_CASES,
             max_age_days: int = MAX_AGE_DAYS) -> list[RankRow]:
        cutoff = self.clock() - max_age_days * 86400
        with self._db() as db:
            rows = db.execute(
                "SELECT provider, model, AVG(score), AVG(latency), COUNT(DISTINCT case_id)"
                " FROM runs WHERE domain=? AND score IS NOT NULL AND run_at>=?"
                " GROUP BY provider, model HAVING COUNT(DISTINCT case_id)>=?"
                " ORDER BY AVG(score) DESC, AVG(latency) ASC, provider, model",
                (domain, cutoff, min_cases)).fetchall()
        return [RankRow(*row) for row in rows]


class _Closing:
    """sqlite3 connections only commit in ``with``; this also closes them."""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self.db.commit()
            else:
                self.db.rollback()
        finally:
            self.db.close()


def format_rank(domain: str, rows: list[RankRow]) -> str:
    if not rows:
        return f"{domain}: not enough scored cases to rank (need {MIN_CASES})"
    lines = [f"{domain}:", f"  {'#':>2} {'provider':<12} {'model':<28} {'score':>6} {'sec':>6}"]
    for i, r in enumerate(rows, 1):
        lines.append(f"  {i:>2} {r.provider:<12} {r.model:<28} {r.score:>6.2f} {r.latency:>6.1f}")
    return "\n".join(lines)
