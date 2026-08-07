"""q8 (owner directive 2026-08-04) — request-scoped shadow-read seam.

Regression coverage:

* shadow read is explicit-only and default off (empty contextvar default);
* the opt-in header parses corpus UUIDs and activates the request scope;
* `_resolve_collections` swaps shadowed corpora to the candidate evidence
  collection (both funnels) while untouched corpora stay on the legacy
  family, and sets funnel_b's route-eligibility per tier;
* funnel_a/funnel_b inject route-eligibility hard-filters ONLY for evidence
  collections — legacy collections pass through byte-identical;
* summary records dual-write into the evidence collection as
  hierarchical-lane records (record_kind=parent_summary).
"""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client import models

from models.schemas import RetrievalTier
from services.retriever import RetrieverOrchestrator

# The package rebinds `funnel_a`/`funnel_b` to the singleton instances, so
# resolve the real modules through sys.modules.
funnel_a_module = importlib.import_module("services.retriever.funnel_a")
funnel_b_module = importlib.import_module("services.retriever.funnel_b")
from services.retriever.shadow_read import (
    FUNNEL_B_ELIGIBILITY,
    SHADOW_READ_CORPORA,
    apply_shadow_read_header,
    is_evidence_collection,
    shadow_read_enabled,
)
from services.storage import qdrant_writer

CORPUS_A = "c6518e7b-1327-4694-85c8-08a81542e425"
CORPUS_B = "48de0f90-0000-4000-8000-000000000000"


@pytest.fixture(autouse=True)
def _reset_shadow_state():
    """Every test starts and ends with shadow read OFF."""
    shadow_token = SHADOW_READ_CORPORA.set(frozenset())
    eligibility_token = FUNNEL_B_ELIGIBILITY.set(None)
    yield
    SHADOW_READ_CORPORA.reset(shadow_token)
    FUNNEL_B_ELIGIBILITY.reset(eligibility_token)


def _orchestrator() -> RetrieverOrchestrator:
    # `_resolve_collections` touches no instance state.
    return object.__new__(RetrieverOrchestrator)


# ── default-off + header opt-in ──────────────────────────────────────────────


def test_shadow_read_is_default_off():
    assert SHADOW_READ_CORPORA.get() == frozenset()
    assert shadow_read_enabled(CORPUS_A) is False


def test_apply_shadow_read_header_parses_corpus_ids():
    activated = apply_shadow_read_header(f" {CORPUS_A} , {CORPUS_B} ")

    assert activated == frozenset({CORPUS_A, CORPUS_B})
    assert shadow_read_enabled(CORPUS_A) is True
    assert shadow_read_enabled(CORPUS_B) is True


@pytest.mark.parametrize("header", [None, "", "   "])
def test_apply_shadow_read_header_absent_keeps_production_reads(header):
    assert apply_shadow_read_header(header) == frozenset()
    assert SHADOW_READ_CORPORA.get() == frozenset()


def test_is_evidence_collection_suffix_match():
    assert is_evidence_collection("corpus_c6518e7b_evidence") is True
    assert is_evidence_collection("corpus_c6518e7b_naive") is False
    assert is_evidence_collection("corpus_c6518e7b_hrag") is False
    assert is_evidence_collection("corpus_c6518e7b_graph") is False


# ── _resolve_collections swap ────────────────────────────────────────────────


def test_resolve_collections_without_shadow_is_byte_identical():
    orch = _orchestrator()

    a, b = orch._resolve_collections(
        RetrievalTier.qdrant_only, [CORPUS_A], None
    )
    assert a == ["corpus_c6518e7b_hrag"]
    assert b == ["corpus_c6518e7b_naive"]

    a, b = orch._resolve_collections(
        RetrievalTier.qdrant_mongo_graph, [CORPUS_A], None
    )
    assert a == ["corpus_c6518e7b_hrag"]
    assert b == ["corpus_c6518e7b_naive", "corpus_c6518e7b_graph"]
    assert FUNNEL_B_ELIGIBILITY.get() is None


def test_resolve_collections_swaps_shadowed_corpus_to_evidence():
    apply_shadow_read_header(CORPUS_A)
    orch = _orchestrator()

    a, b = orch._resolve_collections(
        RetrievalTier.qdrant_only, [CORPUS_A], None
    )

    assert a == ["corpus_c6518e7b_evidence"]
    # One candidate collection replaces naive + graph copies.
    assert b == ["corpus_c6518e7b_evidence"]
    assert FUNNEL_B_ELIGIBILITY.get() == "eligible_focused"


def test_resolve_collections_graph_tier_seeds_with_graph_seed_flag():
    apply_shadow_read_header(CORPUS_A)
    orch = _orchestrator()

    a, b = orch._resolve_collections(
        RetrievalTier.qdrant_mongo_graph, [CORPUS_A], None
    )

    assert a == ["corpus_c6518e7b_evidence"]
    assert b == ["corpus_c6518e7b_evidence"]
    assert FUNNEL_B_ELIGIBILITY.get() == "eligible_graph_seed"


def test_resolve_collections_mixed_scope_only_shadows_opted_in_corpus():
    apply_shadow_read_header(CORPUS_A)
    orch = _orchestrator()

    a, b = orch._resolve_collections(
        RetrievalTier.qdrant_mongo_graph, [CORPUS_A, CORPUS_B], None
    )

    assert a == ["corpus_c6518e7b_evidence", "corpus_48de0f90_hrag"]
    assert b == [
        "corpus_c6518e7b_evidence",
        "corpus_48de0f90_naive",
        "corpus_48de0f90_graph",
    ]


def test_resolve_collections_explicit_override_ignores_shadow():
    apply_shadow_read_header(CORPUS_A)
    orch = _orchestrator()

    a, b = orch._resolve_collections(
        RetrievalTier.qdrant_only,
        [CORPUS_A],
        ["corpus_c6518e7b_hrag", "corpus_c6518e7b_naive"],
    )

    assert a == ["corpus_c6518e7b_hrag"]
    assert b == ["corpus_c6518e7b_naive"]


# ── funnel eligibility injection ─────────────────────────────────────────────


def _must_keys(filter_: models.Filter) -> list[str]:
    return [condition.key for condition in (filter_.must or [])]


def test_funnel_b_legacy_collection_filter_passes_through_untouched():
    base = models.Filter(must=[], must_not=[])

    out = funnel_b_module._with_route_eligibility(
        base, "corpus_c6518e7b_naive"
    )

    assert out is base  # byte-identical behavior for legacy collections


def test_funnel_b_evidence_collection_gets_route_eligibility_must():
    FUNNEL_B_ELIGIBILITY.set("eligible_focused")
    base = models.Filter(
        must=[models.FieldCondition(key="chunk_type", match=models.MatchValue(value="child"))],
        must_not=[models.FieldCondition(key="chunk_kind", match=models.MatchAny(any=["toc"]))],
    )

    out = funnel_b_module._with_route_eligibility(
        base, "corpus_c6518e7b_evidence"
    )

    assert _must_keys(out) == ["chunk_type", "eligible_focused"]
    flag_condition = out.must[-1]
    assert flag_condition.match.value is True
    assert out.must_not == base.must_not
    # The base filter is not mutated in place.
    assert _must_keys(base) == ["chunk_type"]


def test_funnel_b_graph_seed_route_uses_graph_seed_flag():
    FUNNEL_B_ELIGIBILITY.set("eligible_graph_seed")
    base = models.Filter(must=[])

    out = funnel_b_module._with_route_eligibility(
        base, "corpus_c6518e7b_evidence"
    )

    assert _must_keys(out) == ["eligible_graph_seed"]


def test_funnel_b_evidence_without_eligibility_context_fails_closed_to_focused():
    base = models.Filter(must=[])

    out = funnel_b_module._with_route_eligibility(
        base, "corpus_c6518e7b_evidence"
    )

    assert _must_keys(out) == ["eligible_focused"]


def test_funnel_a_legacy_collection_filter_passes_through_untouched():
    base = models.Filter(must=[])

    out = funnel_a_module._with_route_eligibility(
        base, "corpus_c6518e7b_hrag"
    )

    assert out is base


def test_funnel_a_evidence_collection_gets_hierarchical_eligibility_must():
    base = models.Filter(
        must=[
            models.FieldCondition(
                key="chunk_type", match=models.MatchValue(value="summary")
            )
        ]
    )

    out = funnel_a_module._with_route_eligibility(
        base, "corpus_c6518e7b_evidence"
    )

    assert _must_keys(out) == ["chunk_type", "eligible_hierarchical"]
    assert out.must[-1].match.value is True


# ── summary shadow dual-write ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summary_shadow_writes_hierarchical_lane_records(monkeypatch):
    monkeypatch.setattr(
        qdrant_writer,
        "ensure_evidence_collection_for_corpus",
        AsyncMock(return_value="corpus_c6518e7b_evidence"),
    )
    upserts: list[list] = []

    async def _capture(client, *, collection_name, points, point_label):
        upserts.append(list(points))

    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", _capture)

    payload = {
        "corpus_id": CORPUS_A,
        "doc_id": "doc-1",
        "parent_id": "parent-1",
        "chunk_type": "summary",
        "summary_text": "s",
    }
    await qdrant_writer._upsert_evidence_summary_shadow(
        object(), CORPUS_A, [payload], [[0.1] * 4], [None]
    )

    assert len(upserts) == 1
    point = upserts[0][0]
    assert point.id == str(
        qdrant_writer._summary_point_id(CORPUS_A, "parent-1")
    )
    assert point.payload["record_kind"] == "parent_summary"
    assert point.payload["active"] is True
    assert point.payload["eligible_hierarchical"] is True
    assert point.payload["eligible_focused"] is False
    assert point.payload["eligible_graph_seed"] is False


@pytest.mark.asyncio
async def test_upsert_summaries_hook_respects_dual_write_flag(monkeypatch):
    monkeypatch.setattr(qdrant_writer, "_assert_collection_owner", AsyncMock())
    monkeypatch.setattr(
        qdrant_writer, "_collection_layout", AsyncMock(return_value=(True, True))
    )
    monkeypatch.setattr(qdrant_writer, "_upsert_points_batched", AsyncMock())
    summary_shadow = AsyncMock()
    monkeypatch.setattr(
        qdrant_writer, "_upsert_evidence_summary_shadow", summary_shadow
    )

    summary_payload = {
        "corpus_id": CORPUS_A,
        "doc_id": "doc-1",
        "parent_id": "parent-1",
        "summary": "text",
        "summary_model": "stub-model",
        "source_tier": "tier_a",
    }

    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE", False
    )
    await qdrant_writer.upsert_summaries(
        object(), CORPUS_A, [dict(summary_payload)], [[0.1] * 4], ["naive", "hrag"]
    )
    summary_shadow.assert_not_called()

    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE", True
    )
    monkeypatch.setattr(
        qdrant_writer.settings, "QDRANT_EVIDENCE_DUAL_WRITE_CORPUS_IDS", ""
    )
    await qdrant_writer.upsert_summaries(
        object(), CORPUS_A, [dict(summary_payload)], [[0.1] * 4], ["naive", "hrag"]
    )
    summary_shadow.assert_awaited_once()
