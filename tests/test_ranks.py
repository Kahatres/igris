import io
from types import SimpleNamespace

import pytest

from igris.cli import main
from igris.modelbench import BenchStore, CaseResult
from igris.ranks import RANK_DOMAINS, agent_id, format_ranks, rank_agents, tier_for


def prior(domain, score, confidence="high"):
    return {"domain": domain, "benchmark": "b", "value": score / 100, "source": "s",
            "confidence": confidence}


def row(provider, model, score):
    return SimpleNamespace(provider=provider, model=model, score=score)


@pytest.mark.parametrize("score,tier", [
    (1.0, "S"), (0.90, "S"), (0.899, "A"), (0.80, "A"), (0.75, "B"), (0.65, "C"),
    (0.55, "D"), (0.49, "E"), (0.0, "E")])
def test_fixed_thresholds(score, tier):
    assert tier_for(score) == tier


def test_agent_id_is_provider_and_normalized_model():
    assert agent_id("groq", "openai/gpt-oss-120b") == "groq:gpt-oss-120b"
    assert agent_id("opencode", "glm-5.3-flash-free") == "opencode:glm-5.3-flash"


def test_published_scores_give_domain_and_overall_tiers():
    priors = {"m": [prior("code", 92), prior("reasoning", 84), prior("tool_use", 70)]}
    [agent] = rank_agents([("p", "m")], priors=priors)
    assert {d: r.tier for d, r in agent.domains.items()} == {
        "code": "S", "reasoning": "A", "tool_use": "B"}
    assert agent.overall == "A" and agent.overall_score == pytest.approx(0.82)
    assert agent.specialty == ("code",)


def test_own_benchmark_overrides_published_score():
    priors = {"m": [prior("code", 95), prior("reasoning", 95)]}
    [agent] = rank_agents([("p", "m")], {"code": [row("p", "m", 0.55)]}, priors)
    assert agent.domains["code"].tier == "D" and agent.domains["code"].source == "bench"
    assert agent.domains["reasoning"].source == "published"


def test_bench_rows_of_another_provider_do_not_leak():
    [agent] = rank_agents([("p", "m")], {"code": [row("q", "m", 0.95)]}, {})
    assert agent.domains == {} and agent.overall is None


def test_low_confidence_vendor_figure_is_used_and_labelled():
    priors = {"m": [prior("research", 91.5, confidence="low"), prior("tool_use", 87)]}
    [agent] = rank_agents([("p", "m")], priors=priors)
    assert agent.domains["research"].source == "vendor"
    assert agent.specialty == ("research",)


def test_high_confidence_wins_over_vendor_in_the_same_domain():
    priors = {"m": [prior("code", 60), prior("code", 99, confidence="low")]}
    [agent] = rank_agents([("p", "m")], priors=priors)
    assert agent.domains["code"].tier == "C" and agent.domains["code"].source == "published"


def test_one_domain_is_not_enough_for_an_overall_tier():
    [agent] = rank_agents([("p", "m")], priors={"m": [prior("code", 95)]})
    assert agent.overall is None and agent.specialty == ("code",)


def test_no_specialty_below_tier_a():
    priors = {"m": [prior("code", 75), prior("reasoning", 72)]}
    [agent] = rank_agents([("p", "m")], priors=priors)
    assert agent.specialty == ()


def test_order_best_overall_first_unranked_last():
    priors = {"low": [prior("code", 60), prior("reasoning", 60)],
              "high": [prior("code", 90), prior("reasoning", 90)]}
    ranked = rank_agents([("p", "none"), ("p", "low"), ("p", "high"), ("p", "high")],
                         priors=priors)
    assert [a.id for a in ranked] == ["p:high", "p:low", "p:none"]


def test_format_marks_sources():
    priors = {"m": [prior("code", 95), prior("research", 91, confidence="low")]}
    text = format_ranks(rank_agents([("p", "m")], {"reasoning": [row("p", "m", 0.85)]},
                                    priors))
    line = text.splitlines()[1]
    assert line.startswith("p:m") and "S?" in line and "A*" in line
    assert all(d in text.splitlines()[0] for d in RANK_DOMAINS)


def test_shipped_priors_rank_subscription_models():
    ranked = {a.id: a for a in rank_agents([("codex", "gpt-6-astra"), ("claude", "sonnet"),
                                             ("claude", "haiku")])}
    assert "research" in ranked["codex:gpt-6-astra"].specialty
    assert ranked["claude:sonnet"].overall is not None
    assert ranked["claude:sonnet"].overall_score > ranked["claude:haiku"].overall_score


def test_cli_ranks_lists_only_providers_with_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("IGRIS_DATA_DIR", str(tmp_path))
    BenchStore(tmp_path / "bench.sqlite3").record(
        "groq", "openai/gpt-oss-120b", "code",
        [CaseResult(f"c{i}", 1.0, 0.1) for i in range(8)])
    out = io.StringIO()
    code = main(["--ranks"], stdout=out, has_key=lambda name: name == "GROQ_API_KEY")
    text = out.getvalue()
    assert code == 0
    assert "groq:gpt-oss-120b" in text and "S*" in text
    assert "nvidia:" not in text and "gemini:" not in text
    assert "codex:gpt-6-astra" in text and "opencode:glm-5.3-flash" in text
    assert "claude:opus" in text and "codex:gpt-6.1-sol" in text


def test_to_dict_is_json_ready():
    priors = {"m": [prior("code", 95), prior("research", 91, confidence="low")]}
    [agent] = rank_agents([("p", "m")], priors=priors)
    assert agent.to_dict() == {
        "id": "p:m", "overall": "S", "overall_score": 0.93,
        "domains": {"code": {"tier": "S", "source": "published", "score": 0.95},
                    "research": {"tier": "S", "source": "vendor", "score": 0.91}},
        "specialty": ["code", "research"]}


def test_cli_multiagent_check(monkeypatch, tmp_path):
    import shutil

    monkeypatch.setenv("IGRIS_MULTIAGENT_DIR", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    out = io.StringIO()
    assert main(["--multiagent-check"], stdout=out) == 1
    text = out.getvalue()
    assert "[MISS] script" in text and "multiagent is not ready" in text
    (tmp_path / "multiagent.py").write_text("")
    monkeypatch.setattr(shutil, "which", lambda name: "x")
    out = io.StringIO()
    assert main(["--multiagent-check"], stdout=out) == 0
    assert "multiagent is ready" in out.getvalue() and "[warn] workspace" in out.getvalue()
