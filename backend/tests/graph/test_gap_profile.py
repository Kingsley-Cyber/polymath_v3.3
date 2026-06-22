"""Deterministic GapProfile routing for graph Gap mode."""

from services.gap_profile import build_gap_profile
from services.graph.orchestrator import _render_packet_user_prompt


def test_gap_profile_routes_prediction_query():
    profile = build_gap_profile(
        "Why did my forecast miss last quarter and what are the RMSE drivers?"
    )

    assert profile["primary_domain"] == "prediction"
    assert profile["gap_intent"] is True
    assert "RMSE" in profile["required_metrics"]
    assert "Forecast diagnostic" in profile["method_frame"]
    assert "actual outcomes" in profile["likely_missing_data"]


def test_gap_profile_routes_business_query():
    profile = build_gap_profile(
        "Why are we below revenue target compared with competitors?"
    )

    assert profile["primary_domain"] == "business"
    assert "KPI variance" in profile["required_metrics"]
    assert profile["output_shape"] == "strategy gap ledger"


def test_gap_profile_routes_stock_query_without_prediction_advice():
    profile = build_gap_profile(
        "Analyze NVDA stock valuation gap, chart gap fill risk, and DCF assumptions."
    )

    assert profile["primary_domain"] == "stocks"
    assert "DCF sensitivity" in profile["required_metrics"]
    assert "Do not make price predictions" in profile["synthesis_rule"]


def test_gap_profile_routes_process_query():
    profile = build_gap_profile(
        "Why is process cycle time too high and what DMAIC gap should we close?"
    )

    assert profile["primary_domain"] == "process"
    assert "Cp/Cpk" in profile["required_metrics"]
    assert profile["output_shape"] == "DMAIC gap ledger"


def test_gap_profile_routes_market_query():
    profile = build_gap_profile(
        "Where is the market white space for editable Power Apps grids?"
    )

    assert profile["primary_domain"] == "market"
    assert "TAM/SAM/SOM" in profile["required_metrics"]
    assert profile["output_shape"] == "market white-space ledger"


def test_gap_profile_defaults_to_structural_for_corpus_connection_query():
    profile = build_gap_profile(
        "What does my corpus fail to connect about ontology and RAG?"
    )

    assert profile["primary_domain"] == "structural"
    assert "topology similarity" in profile["required_metrics"]
    assert "Graph structural gap analysis" in profile["method_frame"]


def test_gap_profile_keeps_mixed_prediction_business_lenses():
    profile = build_gap_profile("business prediction gap revenue forecast")

    assert profile["primary_domain"] == "prediction"
    assert "business" in profile["secondary_domains"]
    assert profile["domain_scores"]["prediction"] == profile["domain_scores"]["business"]


def test_gap_prompt_includes_profile_for_gap_mode_only():
    profile = build_gap_profile("Why did my forecast miss and what RMSE drivers matter?")
    packet = {
        "query": "Why did my forecast miss and what RMSE drivers matter?",
        "gap_profile": profile,
        "evidence": [],
        "gaps": [],
        "fragile_bridges": [],
        "weak_links": [],
        "bridges": [],
        "analogies": [],
        "transfers": [],
        "tensions": [],
        "communities": [],
        "signals": [],
    }

    gap_prompt = _render_packet_user_prompt(packet, synthesis_mode="gap")
    research_prompt = _render_packet_user_prompt(packet, synthesis_mode="research")

    assert "Gap analysis profile:" in gap_prompt
    assert "Metrics to look for, not invent:" in gap_prompt
    assert "actual outcomes" in gap_prompt
    assert "Gap analysis profile:" not in research_prompt
