"""Agent ranks: S to E tiers per domain and overall, plus each agent's specialty.

An agent is one model served by one provider; its id is ``provider:model``. A domain score
comes from Igris's own benchmark when it has enough recent cases, otherwise from published
scores (``igris.priors``, as a share of the frontier model), otherwise the domain is unranked.
Tiers use fixed thresholds, so adding an agent never changes another agent's tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from igris.priors import load_priors, normalize_model

__all__ = ["RANK_DOMAINS", "TIERS", "AgentRank", "DomainRank", "agent_id", "tier_for",
           "rank_agents", "format_ranks"]

# own-benchmark domains first; research only has published scores
RANK_DOMAINS = ("code", "reasoning", "tool_use", "long_context", "summarization", "research")
TIERS = (("S", 0.90), ("A", 0.80), ("B", 0.70), ("C", 0.60), ("D", 0.50), ("E", 0.0))
# fewer scored domains than this and the overall tier stays empty
MIN_DOMAINS_FOR_OVERALL = 2
SPECIALTY_TIERS = ("S", "A")
_ORDER = [tier for tier, _ in TIERS]


@dataclass(frozen=True)
class DomainRank:
    domain: str
    score: float
    tier: str
    source: str  # "bench", "published" or "vendor" (low-confidence vendor figure)


@dataclass(frozen=True)
class AgentRank:
    id: str
    provider: str
    model: str
    domains: dict = field(default_factory=dict)  # domain -> DomainRank
    overall: str | None = None
    overall_score: float | None = None
    specialty: tuple = ()

    def to_dict(self) -> dict:
        """JSON-ready form for the web interface."""
        return {"id": self.id, "overall": self.overall,
                "overall_score": (round(self.overall_score, 3)
                                  if self.overall_score is not None else None),
                "domains": {d.domain: {"tier": d.tier, "source": d.source,
                                       "score": round(d.score, 3)}
                            for d in self.domains.values()},
                "specialty": list(self.specialty)}


def agent_id(provider: str, model: str) -> str:
    return f"{provider}:{normalize_model(model)}"


def tier_for(score: float) -> str:
    for tier, floor in TIERS:
        if score >= floor:
            return tier
    return TIERS[-1][0]


def _published(priors, model, domain):
    """Mean published score and its source label, preferring high-confidence entries."""
    entries = [e for e in priors.get(normalize_model(model), []) if e["domain"] == domain]
    for source, confidence in (("published", "high"), ("vendor", "low")):
        values = [e["value"] for e in entries if e["confidence"] == confidence]
        if values:
            return sum(values) / len(values), source
    return None


def rank_agents(agents, bench_rows=None, priors=None) -> list[AgentRank]:
    """Rank ``(provider, model)`` pairs.

    ``bench_rows`` maps a domain to rows with ``provider``, ``model`` and a 0-1 ``score``,
    already filtered to enough recent cases (``BenchStore.rank``).
    """
    priors = load_priors() if priors is None else priors
    bench: dict[tuple, float] = {}
    for domain, rows in (bench_rows or {}).items():
        for row in rows:
            bench[(row.provider, normalize_model(row.model), domain)] = row.score
    ranked = []
    for provider, model in dict.fromkeys(agents):
        domains = {}
        for domain in RANK_DOMAINS:
            own = bench.get((provider, normalize_model(model), domain))
            found = (own, "bench") if own is not None else _published(priors, model, domain)
            if found is not None:
                score, source = found
                domains[domain] = DomainRank(domain, score, tier_for(score), source)
        overall_score = overall = None
        if len(domains) >= MIN_DOMAINS_FOR_OVERALL:
            overall_score = sum(d.score for d in domains.values()) / len(domains)
            overall = tier_for(overall_score)
        best = min((_ORDER.index(d.tier) for d in domains.values()), default=None)
        specialty = ()
        if best is not None and _ORDER[best] in SPECIALTY_TIERS:
            specialty = tuple(d.domain for d in domains.values() if d.tier == _ORDER[best])
        ranked.append(AgentRank(agent_id(provider, model), provider, model, domains,
                                overall, overall_score, specialty))
    ranked.sort(key=lambda a: (a.overall is None, -(a.overall_score or 0), -len(a.domains),
                               a.id))
    return ranked


def format_ranks(ranked: list[AgentRank]) -> str:
    """Plain table; '*' marks a bench score, '?' a low-confidence vendor figure."""
    mark = {"bench": "*", "published": "", "vendor": "?"}
    head = ["id", "overall"] + list(RANK_DOMAINS) + ["specialty"]
    lines = [head]
    for agent in ranked:
        cells = [agent.id, agent.overall or "-"]
        for domain in RANK_DOMAINS:
            d = agent.domains.get(domain)
            cells.append(f"{d.tier}{mark[d.source]}" if d else "-")
        cells.append(", ".join(agent.specialty) or "-")
        lines.append(cells)
    widths = [max(len(row[i]) for row in lines) for i in range(len(head))]
    text = "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip()
                     for row in lines)
    return text + "\n* own benchmark   ? vendor-reported (low confidence)   - no data"
