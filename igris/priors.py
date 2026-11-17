"""Published benchmark scores per model and domain: a starting guess before Igris has its own results.

``model_priors.json`` holds current Vals AI results only, so models stay comparable. Each score
is divided by the best score on that benchmark: a value of 1.0 means "as good as the frontier".
A model Vals did not run has no entry; nothing is estimated. Vendor figures are kept apart with
``confidence: low`` (scaled out of 100) and are ignored unless asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["PRIOR_DOMAINS", "normalize_model", "load_priors", "prior_score"]

PRIOR_DOMAINS = ("code", "reasoning", "tool_use", "research")
PRIORS_PATH = Path(__file__).with_name("model_priors.json")


def normalize_model(name: str) -> str:
    """Backend model id to the key used in the priors file.

    ``openai/gpt-oss-120b``, ``@cf/openai/gpt-oss-120b`` and ``glm-5.3-flash-free`` become
    ``gpt-oss-120b`` and ``glm-5.3-flash``.
    """
    key = name.strip().lower().rsplit("/", 1)[-1]
    for suffix in (":free", "-free"):
        if key.endswith(suffix):
            key = key[: -len(suffix)]
    return key


def _number(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError(f"{what} must be a number from 0 to 100")
    return float(value)


def load_priors(path: Path | None = None) -> dict[str, list[dict]]:
    """Model key (and every alias) to entries with ``domain``, ``benchmark``, a 0-1 ``value``,
    ``source`` and ``confidence``; raises ValueError on a malformed file."""
    data = json.loads((path or PRIORS_PATH).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise ValueError("priors file needs a 'models' object")
    benchmarks = data.get("benchmarks", {})
    for name, bench in benchmarks.items():
        if bench.get("domain") not in PRIOR_DOMAINS:
            raise ValueError(f"benchmark {name}: unknown domain {bench.get('domain')!r}")
        if _number(bench.get("best"), f"benchmark {name}: best") <= 0:
            raise ValueError(f"benchmark {name}: best must be above 0")
    priors: dict[str, list[dict]] = {}
    for model, info in data["models"].items():
        entries = []
        for name, score in info.get("scores", {}).items():
            bench = benchmarks.get(name)
            if bench is None:
                raise ValueError(f"{model}: unknown benchmark {name!r}")
            value = _number(score, f"{model}: {name}") / bench["best"]
            entries.append({"domain": bench["domain"], "benchmark": name,
                            "value": min(value, 1.0), "source": info.get("source", ""),
                            "confidence": "high"})
        for item in info.get("vendor", []):
            if item.get("domain") not in PRIOR_DOMAINS:
                raise ValueError(f"{model}: unknown domain {item.get('domain')!r}")
            if not item.get("benchmark") or not item.get("source"):
                raise ValueError(f"{model}: every vendor figure needs a benchmark and a source")
            entries.append({"domain": item["domain"], "benchmark": item["benchmark"],
                            "value": _number(item.get("score"), f"{model}: vendor score") / 100,
                            "source": item["source"], "confidence": "low"})
        priors[normalize_model(model)] = entries
    for alias, target in data.get("aliases", {}).items():
        if normalize_model(target) not in priors:
            raise ValueError(f"alias {alias!r} points to unknown model {target!r}")
        priors[normalize_model(alias)] = priors[normalize_model(target)]
    return priors


def prior_score(model: str, domain: str, *, priors: dict[str, list[dict]] | None = None,
                include_low: bool = False) -> float | None:
    """Mean share of the frontier for a domain (0-1), or None when nothing is known."""
    if domain not in PRIOR_DOMAINS:
        raise ValueError(f"unknown domain {domain!r}; choose from {', '.join(PRIOR_DOMAINS)}")
    table = load_priors() if priors is None else priors
    values = [e["value"] for e in table.get(normalize_model(model), [])
              if e["domain"] == domain and (include_low or e["confidence"] != "low")]
    return sum(values) / len(values) if values else None
