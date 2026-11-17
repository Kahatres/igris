import json

import pytest

from igris.priors import load_priors, normalize_model, prior_score

BENCH = {"B1": {"domain": "code", "best": 80, "source": "s"},
         "B2": {"domain": "code", "best": 50, "source": "s"},
         "R1": {"domain": "reasoning", "best": 100, "source": "s"}}


def write(tmp_path, models, benchmarks=BENCH, aliases=None):
    path = tmp_path / "priors.json"
    path.write_text(json.dumps({"benchmarks": benchmarks, "models": models,
                                "aliases": aliases or {}}), encoding="utf-8")
    return path


def test_normalize_strips_provider_prefix_and_free_suffix():
    assert normalize_model("openai/gpt-oss-120b") == "gpt-oss-120b"
    assert normalize_model("@cf/openai/gpt-oss-120b") == "gpt-oss-120b"
    assert normalize_model("glm-5.3-flash-free") == "glm-5.3-flash"
    assert normalize_model("Some/Model:free") == "model"


def test_score_is_share_of_the_benchmark_best(tmp_path):
    priors = load_priors(write(tmp_path, {"m": {"scores": {"B1": 80, "B2": 25}}}))
    # 80/80 and 25/50
    assert prior_score("m", "code", priors=priors) == pytest.approx(0.75)


def test_unknown_model_or_empty_domain_has_no_prior(tmp_path):
    priors = load_priors(write(tmp_path, {"m": {"scores": {"B1": 40}}}))
    assert prior_score("other", "code", priors=priors) is None
    assert prior_score("m", "reasoning", priors=priors) is None


def test_vendor_figures_are_low_confidence_and_ignored_by_default(tmp_path):
    priors = load_priors(write(tmp_path, {"m": {"vendor": [
        {"domain": "research", "benchmark": "x", "score": 50, "source": "s"}]}}))
    assert prior_score("m", "research", priors=priors) is None
    assert prior_score("m", "research", priors=priors, include_low=True) == pytest.approx(0.5)


def test_aliases_share_the_target_entries(tmp_path):
    priors = load_priors(write(tmp_path, {"claude-x": {"scores": {"R1": 90}}},
                               aliases={"x": "claude-x"}))
    assert prior_score("x", "reasoning", priors=priors) == pytest.approx(0.9)


@pytest.mark.parametrize("models,benchmarks,aliases", [
    ({"m": {"scores": {"nope": 5}}}, BENCH, None),
    ({"m": {"scores": {"B1": 101}}}, BENCH, None),
    ({"m": {"scores": {"B1": True}}}, BENCH, None),
    ({"m": {"scores": {}}}, {"B": {"domain": "poetry", "best": 5}}, None),
    ({"m": {"scores": {}}}, {"B": {"domain": "code", "best": 0}}, None),
    ({"m": {"vendor": [{"domain": "code", "score": 5, "benchmark": "b"}]}}, BENCH, None),
    ({"m": {}}, BENCH, {"a": "missing"}),
])
def test_malformed_files_are_rejected(tmp_path, models, benchmarks, aliases):
    with pytest.raises(ValueError):
        load_priors(write(tmp_path, models, benchmarks, aliases))


def test_file_without_models_is_rejected(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_priors(path)


def test_unknown_domain_argument():
    with pytest.raises(ValueError):
        prior_score("m", "poetry", priors={})


def test_shipped_file_is_valid():
    priors = load_priors()
    assert prior_score("gpt-6-astra", "research", priors=priors) > 0.95
    assert prior_score("sonnet", "code", priors=priors) == prior_score(
        "claude-sonnet-5.5", "code", priors=priors)
    assert prior_score("deepseek-v4.1-flash-free", "code", priors=priors) is not None
    assert prior_score("openai/gpt-oss-120b", "code", priors=priors) is None
    assert all(0 <= e["value"] <= 1 for entries in priors.values() for e in entries)
