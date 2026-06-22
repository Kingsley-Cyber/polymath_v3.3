"""Deterministic gap-analysis profile routing.

This module does not perform the analysis itself. It gives graph synthesis a
stable, query-derived frame so Gap mode can adapt to prediction, business,
stock, process, market, or structural graph questions without relying on the
LLM to guess the methodology from scratch.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9+./_-]*")


@dataclass(frozen=True)
class GapDomainSpec:
    domain: str
    label: str
    method_frame: str
    output_shape: str
    terms: tuple[str, ...]
    metrics: tuple[str, ...]
    required_evidence: tuple[str, ...]
    missing_data: tuple[str, ...]
    synthesis_rule: str


_SPECS: tuple[GapDomainSpec, ...] = (
    GapDomainSpec(
        domain="prediction",
        label="Prediction / forecasting gap",
        method_frame="Forecast diagnostic: residuals, error decomposition, validation, and drift checks",
        output_shape="forecast gap ledger",
        terms=(
            "prediction",
            "predictions",
            "predict",
            "predicted",
            "predictive",
            "forecast",
            "forecasting",
            "actual",
            "actuals",
            "outcome",
            "residual",
            "error",
            "mae",
            "rmse",
            "mape",
            "brier",
            "log-loss",
            "logloss",
            "holdout",
            "backtest",
            "backtesting",
            "drift",
            "regime change",
            "bias",
            "variance",
            "confidence interval",
            "coverage",
            "shap",
            "lime",
        ),
        metrics=("MAE", "RMSE", "MAPE", "Brier score", "log-loss", "coverage gap"),
        required_evidence=(
            "predicted values",
            "actual outcomes",
            "forecast timestamps or holdout period",
            "model/version context",
            "error attribution or feature context",
        ),
        missing_data=(
            "actual outcomes",
            "predicted values",
            "forecast horizon",
            "holdout/backtest window",
            "model version and feature snapshot",
        ),
        synthesis_rule=(
            "If numeric forecast logs are absent, explain the diagnostic method "
            "and name the missing fields instead of inventing error metrics."
        ),
    ),
    GapDomainSpec(
        domain="business",
        label="Business / strategic performance gap",
        method_frame="Strategic gap analysis: KPI variance, benchmark comparison, capability gaps, and initiatives",
        output_shape="strategy gap ledger",
        terms=(
            "business",
            "strategy",
            "strategic",
            "revenue",
            "margin",
            "profit",
            "kpi",
            "okr",
            "target",
            "goal",
            "benchmark",
            "competitor",
            "competitive",
            "market share",
            "balanced scorecard",
            "swot",
            "pestel",
            "7s",
            "capability",
            "maturity",
            "customer satisfaction",
        ),
        metrics=("KPI variance", "revenue gap", "margin gap", "market share gap", "capability maturity"),
        required_evidence=(
            "current KPI or capability state",
            "target KPI or strategic goal",
            "benchmark or competitor reference",
            "root-cause evidence",
            "initiative or constraint evidence",
        ),
        missing_data=(
            "current KPI values",
            "target KPI values",
            "benchmark/competitor baseline",
            "time period",
            "root-cause evidence",
        ),
        synthesis_rule=(
            "Frame gaps as current-state versus target-state deltas and rank "
            "initiatives by evidence, leverage, and uncertainty."
        ),
    ),
    GapDomainSpec(
        domain="stocks",
        label="Stock / financial-market gap",
        method_frame="Market gap analysis: price gap, valuation gap, estimate surprise, and risk context",
        output_shape="financial gap research ledger",
        terms=(
            "stock",
            "stocks",
            "equity",
            "share price",
            "price gap",
            "gap fill",
            "chart gap",
            "technical analysis",
            "intrinsic value",
            "valuation",
            "dcf",
            "wacc",
            "market cap",
            "earnings",
            "eps",
            "earnings surprise",
            "10-k",
            "10q",
            "sec filing",
            "margin of safety",
            "portfolio",
            "pair trading",
            "stat arb",
        ),
        metrics=("gap size", "fill probability", "margin of safety", "DCF sensitivity", "earnings surprise"),
        required_evidence=(
            "security/ticker and date",
            "price or chart data",
            "valuation assumptions",
            "filings or earnings evidence",
            "risk and uncertainty context",
        ),
        missing_data=(
            "ticker/security identity",
            "price series and event date",
            "valuation assumptions",
            "filings/financial statements",
            "backtest or comparable sample",
        ),
        synthesis_rule=(
            "Treat financial output as research and risk framing. Do not make "
            "price predictions or trade recommendations without a separate "
            "forecasting/backtesting layer."
        ),
    ),
    GapDomainSpec(
        domain="process",
        label="Process improvement gap",
        method_frame="DMAIC / Lean Six Sigma: define target, measure current state, analyze causes, improve, control",
        output_shape="DMAIC gap ledger",
        terms=(
            "process",
            "workflow",
            "cycle time",
            "lead time",
            "defect",
            "defects",
            "quality",
            "sigma",
            "six sigma",
            "lean",
            "dmaic",
            "ctq",
            "voc",
            "cpk",
            "cp",
            "ppm",
            "copq",
            "root cause",
            "fishbone",
            "5 whys",
            "pareto",
            "value stream",
            "bottleneck",
            "throughput",
        ),
        metrics=("cycle time", "defect rate", "sigma level", "Cp/Cpk", "PPM", "COPQ"),
        required_evidence=(
            "voice of customer or CTQ target",
            "current-state process measure",
            "target or entitlement",
            "root-cause evidence",
            "control/monitoring plan",
        ),
        missing_data=(
            "current cycle time or defect rate",
            "target CTQ threshold",
            "sample size/time window",
            "process map",
            "root-cause observations",
        ),
        synthesis_rule=(
            "Separate current state, target state, root causes, and the cheapest "
            "measurement that would close the diagnostic uncertainty."
        ),
    ),
    GapDomainSpec(
        domain="market",
        label="Market opportunity / consumer gap",
        method_frame="Market opportunity analysis: unmet jobs, demand-supply gap, positioning, and validation",
        output_shape="market white-space ledger",
        terms=(
            "market",
            "white space",
            "whitespace",
            "opportunity",
            "unmet need",
            "underserved",
            "customer",
            "consumer",
            "jobs to be done",
            "jtbd",
            "tam",
            "sam",
            "som",
            "demand",
            "supply",
            "segment",
            "segmentation",
            "positioning",
            "perceptual map",
            "price value",
            "reviews",
            "competitor offerings",
            "product market",
            "market sizing",
        ),
        metrics=("TAM/SAM/SOM", "unmet need frequency", "willingness-to-pay", "NPS", "share of gap"),
        required_evidence=(
            "target customer/job",
            "existing alternatives",
            "unmet need evidence",
            "market sizing assumptions",
            "validation signal",
        ),
        missing_data=(
            "target segment",
            "customer interviews or reviews",
            "competitor/offering list",
            "market size assumptions",
            "validation threshold",
        ),
        synthesis_rule=(
            "Distinguish locally missing corpus coverage from a real market gap; "
            "web evidence can challenge whether the gap is only local."
        ),
    ),
    GapDomainSpec(
        domain="structural",
        label="Structural graph / corpus gap",
        method_frame="Graph structural gap analysis: missing edges, fragile bridges, terminological splits, analogies, and transfers",
        output_shape="structural gap ledger",
        terms=(
            "corpus",
            "graph",
            "ontology",
            "ontologies",
            "knowledge graph",
            "rag",
            "retrieval",
            "connect",
            "connected",
            "connection",
            "relationship",
            "bridge",
            "bridges",
            "missing link",
            "weak link",
            "what does not connect",
            "not connect",
            "not linked",
            "under connected",
            "taxonomy",
            "schema",
        ),
        metrics=("topology similarity", "neighbor Jaccard", "bridge fragility", "support status"),
        required_evidence=(
            "resolved query anchors",
            "candidate gap or fragile bridge",
            "supporting source chunks",
            "graph metrics or shared-neighbor basis",
        ),
        missing_data=(
            "supporting chunk text for both endpoints",
            "direct relation evidence",
            "cross-document corroboration",
        ),
        synthesis_rule=(
            "Treat graph gaps as hypotheses. State whether the absence is a "
            "missing asserted edge, weak provenance, or merely thin evidence."
        ),
    ),
)


_SPEC_BY_DOMAIN = {spec.domain: spec for spec in _SPECS}
_PHRASE_BONUS = 1.0
_TOKEN_BONUS = 0.55
_EXPLICIT_GAP_TERMS = {
    "gap",
    "gaps",
    "missing",
    "shortfall",
    "delta",
    "variance",
    "difference",
    "underperform",
    "underperformance",
    "unmet",
    "weakness",
    "hole",
    "absence",
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return set(_WORD_RE.findall(text))


def _score_spec(text: str, token_set: set[str], spec: GapDomainSpec) -> tuple[float, list[str]]:
    score = 0.0
    hits: list[str] = []
    for term in spec.terms:
        t = _normalize(term)
        if not t:
            continue
        if " " in t or "-" in t or "/" in t:
            if t in text:
                score += _PHRASE_BONUS + min(1.0, len(t.split()) * 0.12)
                hits.append(term)
            continue
        if t in token_set:
            score += _TOKEN_BONUS
            hits.append(term)
    return score, hits


def _confidence(raw_score: float, best_score: float) -> float:
    if best_score <= 0:
        return 0.0
    # Saturating confidence: a few strong domain hits should be enough, but
    # close second domains still remain visible as secondary frames.
    absolute = min(1.0, raw_score / 4.0)
    relative = raw_score / best_score
    return round((0.68 * absolute) + (0.32 * relative), 3)


def _missing_for_query(spec: GapDomainSpec, text: str) -> list[str]:
    """Conservatively list data likely needed for computation.

    These are not hard failures; the corpus may contain them even if the raw
    query does not. The prompt is told to treat this as a check-list.
    """

    lower = text
    missing = []
    for item in spec.missing_data:
        item_text = _normalize(item)
        words = [w for w in _WORD_RE.findall(item_text) if len(w) > 3]
        if words and any(w in lower for w in words):
            continue
        missing.append(item)
    return missing[:6]


def build_gap_profile(query: str) -> dict[str, Any]:
    """Return a deterministic, serializable gap-analysis profile.

    The profile is intentionally lightweight: it steers retrieval synthesis and
    output shape, but it does not pretend to compute MAE, DCF, Cp/Cpk, TAM, or
    any other metric unless separate numeric data/tools are added later.
    """

    text = _normalize(query)
    token_set = _tokens(text)

    scored: list[dict[str, Any]] = []
    for spec in _SPECS:
        score, hits = _score_spec(text, token_set, spec)
        # Generic gap wording should not make the structural profile win by
        # itself, but it should lift structural when no domain is visible.
        if spec.domain == "structural" and token_set & _EXPLICIT_GAP_TERMS:
            score += 0.25
        if hits or score > 0:
            scored.append({"spec": spec, "score": score, "hits": hits[:10]})

    if not scored:
        structural = _SPEC_BY_DOMAIN["structural"]
        scored = [{"spec": structural, "score": 0.35, "hits": []}]

    scored.sort(
        key=lambda row: (
            float(row["score"]),
            -next(i for i, spec in enumerate(_SPECS) if spec.domain == row["spec"].domain),
        ),
        reverse=True,
    )
    best = scored[0]
    best_score = float(best["score"] or 0.0)

    domain_scores: dict[str, float] = {}
    indicators: dict[str, list[str]] = {}
    for row in scored:
        spec = row["spec"]
        conf = _confidence(float(row["score"] or 0.0), best_score)
        if conf <= 0:
            continue
        domain_scores[spec.domain] = conf
        indicators[spec.domain] = list(row["hits"])

    primary_spec: GapDomainSpec = best["spec"]
    secondary = [
        row["spec"].domain
        for row in scored[1:]
        if _confidence(float(row["score"] or 0.0), best_score) >= 0.38
    ][:3]

    explicit_gap = bool(token_set & _EXPLICIT_GAP_TERMS)
    gap_intent = explicit_gap or primary_spec.domain != "structural" or best_score >= 0.9

    return {
        "version": "gap-profile-v1",
        "gap_intent": bool(gap_intent),
        "primary_domain": primary_spec.domain,
        "primary_label": primary_spec.label,
        "secondary_domains": secondary,
        "domain_scores": domain_scores,
        "confidence": domain_scores.get(primary_spec.domain, 0.0),
        "method_frame": primary_spec.method_frame,
        "output_shape": primary_spec.output_shape,
        "required_metrics": list(primary_spec.metrics),
        "required_evidence": list(primary_spec.required_evidence),
        "likely_missing_data": _missing_for_query(primary_spec, text),
        "matched_indicators": indicators,
        "synthesis_rule": primary_spec.synthesis_rule,
        "calculation_policy": (
            "Do not compute quantitative metrics unless the evidence packet or "
            "user-provided data contains the required numeric inputs."
        ),
    }
