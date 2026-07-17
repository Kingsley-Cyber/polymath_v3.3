from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from models.schemas import RetrievalTier
from services.chat_orchestrator import (
    _build_chat_query_plan,
    _build_librarian_plan_trace,
)
from services.retriever.librarian_planner import build_query_plan_v1


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
