"""Graph-authority separation + extraction-engine lock invariants.

Owner directive 2026-08-03. Structural/shadow graph artifacts are extraction
candidates, never canonical factual evidence; the storage layer stamps the
authority vocabulary and the factual-assertion read paths fail closed.
extraction_engine is locked once a corpus holds artifacts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (BACKEND / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Write side: every inline structural writer stamps the shadow vocabulary.
# ---------------------------------------------------------------------------


def test_inline_mentions_writer_stamps_shadow_authority():
    src = _read("services/graph/neo4j_writer.py")
    # Both MENTIONS write blocks (batch writer + legacy single-entity path).
    assert src.count("m.projection_kind = 'structural_shadow'") >= 2, (
        "every MENTIONS writer must stamp projection_kind=structural_shadow"
    )
    assert src.count("m.authority = 'noncanonical'") >= 2, (
        "every MENTIONS writer must stamp authority=noncanonical"
    )


def test_inline_fact_writer_never_leaves_status_absent():
    src = _read("services/graph/neo4j_writer.py")
    assert "f.knowledge_status = coalesce(f.knowledge_status, 'candidate')" in src, (
        "inline ghost_b Facts must default knowledge_status to 'candidate'; "
        "an absent status reads as 'accepted' downstream (authority inversion)"
    )
    assert "f.projection_kind = coalesce(f.projection_kind, 'structural_shadow')" in src


def test_inline_relates_to_and_code_edges_stamped_merge_safe():
    src = _read("services/graph/neo4j_writer.py")
    # coalesce-guarded: the inline writer must never downgrade an edge that a
    # later claim promotion upgraded to canonical_candidate.
    assert "r.projection_kind = coalesce(r.projection_kind, 'structural_shadow')" in src
    assert "r.authority = coalesce(r.authority, 'noncanonical')" in src
    # graphify CALLS edges are structural too.
    assert "r.projection_kind = 'structural_shadow'," in src


def test_explains_edges_stamped_structural_shadow():
    src = _read("services/graph/neo4j_writer.py")
    assert "e.projection_kind = 'structural_shadow'" in src
    assert "e.authority = 'noncanonical'" in src


def test_claim_promotion_upgrades_authority_unconditionally():
    src = _read("services/ingestion/promote.py")
    assert "r.authority = 'canonical_candidate'" in src
    assert "r.projection_kind = 'claim_promoted'" in src


# ---------------------------------------------------------------------------
# Read side: factual-assertion routes fail closed on shadow artifacts.
# ---------------------------------------------------------------------------


def test_fact_seeding_defaults_missing_status_to_candidate():
    src = _read("services/retriever/fact_retrieval.py")
    assert "coalesce(f.knowledge_status, 'accepted')" not in src, (
        "unlabeled facts must never read as 'accepted' — that inverts "
        "authority (shadow facts above release-gated claim candidates)"
    )
    assert src.count("coalesce(f.knowledge_status, 'candidate')") >= 2


def test_answer_decoration_requires_claim_or_nonshadow_authority():
    src = _read("services/retriever/graph_decoration.py")
    assert "size(coalesce(r.claim_ids, [])) > 0" in src
    assert "coalesce(r.authority, 'noncanonical') <> 'noncanonical'" in src


# ---------------------------------------------------------------------------
# Engine lock: direct extraction_engine mutation forbidden on non-empty corpora.
# ---------------------------------------------------------------------------


def test_engine_lock_error_shape():
    from services.ingestion_service import ExtractionEngineLockedError

    exc = ExtractionEngineLockedError("relex_local", "runpod_flash", 4)
    assert exc.current_engine == "relex_local"
    assert exc.attempted_engine == "runpod_flash"
    assert exc.doc_count == 4
    assert isinstance(exc, ValueError)


def test_update_corpus_guard_rejects_engine_change_on_nonempty_corpus(monkeypatch):
    """Behavioral: the guard fires before any merge/write on a corpus with
    documents, and a no-op (same value) passes."""
    import asyncio

    from services import ingestion_service as svc

    async def _fake_get_corpus_raw(corpus_id):
        return {
            "corpus_id": corpus_id,
            "doc_count": 4,
            "user_id": "u1",
            "default_ingestion_config": {"extraction_engine": "relex_local"},
        }

    inst = svc.ingestion_service
    monkeypatch.setattr(inst, "_get_corpus_raw", _fake_get_corpus_raw)

    with pytest.raises(svc.ExtractionEngineLockedError):
        asyncio.run(
            inst.update_corpus(
                "c1",
                {"default_ingestion_config": {"extraction_engine": "runpod_flash"}},
                user_id="u1",
            )
        )


def test_router_maps_engine_lock_to_structured_409():
    src = (BACKEND / "routers/ingestion.py").read_text(encoding="utf-8")
    assert "except ExtractionEngineLockedError" in src
    assert '"error": "extraction_engine_locked"' in src
    assert '"required_action": "create_reextraction_generation"' in src


# ---------------------------------------------------------------------------
# Backfill script: idempotent labeling vocabulary matches the writer stamps.
# ---------------------------------------------------------------------------


def test_shadow_backfill_script_labels_all_structural_classes():
    src = (BACKEND / "scripts/label_structural_shadow_projections.py").read_text(
        encoding="utf-8"
    )
    for cls in (
        "mentions_shadow",
        "explains_shadow",
        "calls_shadow",
        "ghost_b_facts_candidate",
        "relates_to_shadow",
        "relates_to_claim_uplift",
    ):
        assert cls in src, f"backfill missing class {cls}"
    # Idempotence: every match clause filters on the missing label. Four
    # edge classes gate on projection_kind, ghost_b Facts gate on
    # knowledge_status, and the claim-uplift class gates on authority.
    assert src.count("projection_kind IS NULL") >= 4
    assert "f.knowledge_status IS NULL" in src
    assert "r.authority IS NULL" in src
    # Dry-run default: no --apply means no SETs execute.
    assert 'ap.add_argument("--apply", action="store_true"' in src
