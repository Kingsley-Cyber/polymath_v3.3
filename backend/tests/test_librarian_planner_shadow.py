from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import services.retriever.librarian_planner as planner_module
from models.schemas import RetrievalTier
from services.chat_orchestrator import (
    _build_chat_query_plan,
    _build_librarian_plan_trace,
    _format_chat_query_plan_trace,
    _librarian_refusal_signals_for_answerability,
)
from services.retriever.librarian_planner import (
    LibrarianPlanner,
    QueryPlanReplayCache,
    build_query_plan_v1,
)


class _ExplosivePlanner:
    async def build(self, *_args, **_kwargs):
        raise AssertionError("planner must not run while both flags are OFF")


class _FakePlanner:
    def __init__(self, plan):
        self.plan = plan
        self.calls = 0

    async def build(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(
            plan=self.plan,
            diagnostics={
                "status": "simple_bypass",
                "shortlist_calls": 0,
                "query_embedding_calls": 0,
                "provider_calls": 0,
            },
        )


class _CapturingPlanner(_FakePlanner):
    def __init__(self, plan):
        super().__init__(plan)
        self.kwargs = None

    async def build(self, *_args, **kwargs):
        self.kwargs = kwargs
        return await super().build(*_args, **kwargs)


async def _embedding_config(_corpus_ids):
    return {"embedding_model_id": "test"}


async def _forbidden_embedding_config(_corpus_ids):
    raise AssertionError("simple shadow must not load an embedding contract")


@pytest.mark.asyncio
async def test_both_flags_off_do_not_enter_planner_path():
    result = await _build_librarian_plan_trace(
        query="What is narrative directing?",
        corpus_ids=["c"],
        requested_tier=RetrievalTier.qdrant_mongo,
        enabled=False,
        shadow=False,
        planner_service=_ExplosivePlanner(),
        db=object(),
        embedding_config_loader=_embedding_config,
    )

    assert result is None


@pytest.mark.asyncio
async def test_shadow_records_plan_without_behavior_activation():
    raw_query = "  What is narrative directing and why is it useful?  "
    plan = build_query_plan_v1(
        raw_query,
        corpus_id="c",
        corpus_doc_version="sha256:" + "a" * 64,
        requested_tier=RetrievalTier.qdrant_mongo,
    )
    fake = _FakePlanner(plan)

    result = await _build_librarian_plan_trace(
        query=raw_query,
        corpus_ids=["c"],
        requested_tier=RetrievalTier.qdrant_mongo,
        enabled=False,
        shadow=True,
        planner_service=fake,
        db=object(),
        embedding_config_loader=_forbidden_embedding_config,
    )

    assert fake.calls == 1
    assert result["mode"] == "shadow"
    assert result["behavior_applied"] is False
    assert result["plan"]["plan_hash"] == plan.plan_hash
    assert result["plan"]["subqueries"][0]["text"] == raw_query
    assert result["diagnostics"]["shortlist_calls"] == 0
    assert result["diagnostics"]["query_embedding_calls"] == 0
    assert result["diagnostics"]["provider_calls"] == 0


@pytest.mark.asyncio
async def test_trace_passes_user_and_decomposer_authority_to_planner():
    query = "What is narrative directing and why is it useful?"
    plan = build_query_plan_v1(
        query,
        corpus_id="c",
        corpus_doc_version="sha256:" + "b" * 64,
        requested_tier=RetrievalTier.qdrant_mongo,
        allow_llm_escalation=True,
    )
    fake = _CapturingPlanner(plan)

    result = await _build_librarian_plan_trace(
        query=query,
        corpus_ids=["c"],
        requested_tier=RetrievalTier.qdrant_mongo,
        enabled=True,
        shadow=False,
        planner_service=fake,
        db=object(),
        embedding_config_loader=_embedding_config,
        user_id="user-1",
        llm_decomposer_enabled=True,
    )

    assert result["mode"] == "enabled"
    assert fake.kwargs["user_id"] == "user-1"
    assert fake.kwargs["llm_decomposer_enabled"] is True


@pytest.mark.asyncio
async def test_enabled_timeout_applies_deterministic_d3b_shape_without_provider(
    monkeypatch,
):
    planner = LibrarianPlanner(cache=QueryPlanReplayCache())
    version = "sha256:" + "c" * 64

    async def fake_version(_db, _corpus_ids):
        return version

    async def timed_out_shortlist(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(
        planner_module,
        "build_tier0_shortlist",
        timed_out_shortlist,
    )

    query = "What exact stages does the VES handbook define for a VFX shot's pipeline?"
    result = await _build_librarian_plan_trace(
        query=query,
        corpus_ids=["corpus"],
        requested_tier=RetrievalTier.qdrant_mongo_graph,
        enabled=True,
        shadow=False,
        planner_service=planner,
        db=object(),
        embedding_config_loader=_embedding_config,
        user_id="user-1",
        llm_decomposer_enabled=True,
    )

    assert result["mode"] == "enabled_degraded"
    # W1-D2 (2026-07-19): a degraded plan informs the trace but never takes
    # behavior — the acceptance re-run proved degraded planned retrieval can
    # seat nothing and replace baseline evidence with less. Fail-open.
    assert result["behavior_applied"] is False
    assert result["plan"]["shape"] == "enumerative_trace"
    assert result["plan"]["planner"] == "rule:enumerative_trace"
    assert result["plan"]["corpus_doc_version"] == version
    assert result["diagnostics"]["status"] == "degraded_deterministic_fallback"
    assert result["diagnostics"]["reason"] == "TimeoutError: "
    assert result["diagnostics"]["silent_fallback_count"] == 1
    assert result["diagnostics"]["fallback_signal"] == "librarian_degraded_fallback"


@pytest.mark.asyncio
async def test_enabled_timeout_keeps_simple_llm_eligible_query_bypassed(
    monkeypatch,
):
    planner = LibrarianPlanner(cache=QueryPlanReplayCache())
    version = "sha256:" + "d" * 64

    async def fake_version(_db, _corpus_ids):
        return version

    async def timed_out_shortlist(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(planner_module, "corpus_doc_set_version", fake_version)
    monkeypatch.setattr(
        planner_module,
        "build_tier0_shortlist",
        timed_out_shortlist,
    )

    result = await _build_librarian_plan_trace(
        query="What is narrative directing and why is it useful?",
        corpus_ids=["corpus"],
        requested_tier=RetrievalTier.qdrant_mongo,
        enabled=True,
        shadow=False,
        planner_service=planner,
        db=object(),
        embedding_config_loader=_embedding_config,
        user_id="user-1",
        llm_decomposer_enabled=True,
    )

    assert result["mode"] == "enabled_degraded"
    assert result["behavior_applied"] is False
    assert result["plan"] is None
    assert result["diagnostics"]["provider_calls"] == 0


def _legacy_trace_plan(**kwargs):
    return _build_chat_query_plan(
        query="What is narrative directing?",
        retrieval_query="What is narrative directing?",
        requested_tier=RetrievalTier.qdrant_mongo,
        corpus_ids=["c"],
        collections=None,
        profile_cfg={
            "query_profile": "balanced",
            "retrieval_k": 40,
            "top_k_summary": 20,
            "rerank_enabled": True,
            "rerank_top_n": 40,
            "final_top_k": 8,
            "source_cap": 8,
        },
        search_mode="local",
        hyde_applied=False,
        **kwargs,
    )


def test_shadow_metadata_is_additive_to_legacy_query_plan_bytes():
    shadow = {
        "mode": "shadow",
        "behavior_applied": False,
        "plan": {"shape": "simple", "plan_hash": "sha256:" + "a" * 64},
        "diagnostics": {"provider_calls": 0},
    }
    baseline = _legacy_trace_plan()
    augmented = _legacy_trace_plan(librarian_plan=shadow)
    added = augmented.pop("librarian_query_plan")

    assert augmented == baseline
    assert added == shadow


def test_compose_has_default_off_passthrough_for_both_runtime_services():
    candidates = (
        Path(__file__).resolve().parents[2] / "docker-compose.yml",
        Path("/workspace/docker-compose.yml"),
    )
    compose_path = next(path for path in candidates if path.is_file())
    compose = compose_path.read_text(encoding="utf-8")

    assert (
        compose.count("LIBRARIAN_PLANNER_ENABLED: ${LIBRARIAN_PLANNER_ENABLED:-false}")
        == 2
    )
    assert (
        compose.count("LIBRARIAN_PLANNER_SHADOW: ${LIBRARIAN_PLANNER_SHADOW:-false}")
        == 2
    )
    assert (
        compose.count(
            "LIBRARIAN_LLM_DECOMPOSER_ENABLED: "
            "${LIBRARIAN_LLM_DECOMPOSER_ENABLED:-false}"
        )
        == 2
    )


def test_trace_formatter_reports_applied_behavior_truthfully():
    trace = _legacy_trace_plan(
        librarian_plan={
            "mode": "enabled",
            "behavior_applied": True,
            "plan": {
                "shape": "complex",
                "plan_hash": "sha256:" + "a" * 64,
            },
            "diagnostics": {"provider_calls": 1},
        }
    )

    assert "behavior=applied" in _format_chat_query_plan_trace(trace)


def test_shadow_refusal_signals_remain_zero_behavior():
    signals = {
        "shortlist_empty": False,
        "named_source_missing": True,
        "planner_llm_unavailable": False,
    }
    shadow = {"mode": "shadow", "plan": {"refusal_signals": signals}}
    enabled = {"mode": "enabled", "plan": {"refusal_signals": signals}}

    assert _librarian_refusal_signals_for_answerability(shadow) == {}
    assert _librarian_refusal_signals_for_answerability(enabled) == signals
