"""Unit tests for the Retrieval Layer v4 deterministic curation stage.

Covers ``services.retriever.curation.build_packet`` (spec §2.5) and the
pool-relative low-confidence gate that shadows the legacy absolute threshold.
Every test runs against pure in-memory ``SourceChunk`` lists — no I/O, no
stores — so the suite is fast and deterministic.
"""

from __future__ import annotations

from models.schemas import RetrievalTier, SourceChunk
from services.retriever import (
    _should_drop_low_confidence_rerank,
    _top_is_relative_standout,
)
from services.retriever.curation import (
    FLOOR_RATIO,
    MIN_KEEP,
    PER_DOC_CAP,
    build_packet,
)
from services.retriever.intent_policy import infer_retrieval_intent

_INTENT = infer_retrieval_intent("compare weighted regression and option pricing")


def _chunk(
    chunk_id: str,
    *,
    score: float,
    parent_id: str | None = None,
    doc_id: str | None = None,
    corpus_id: str = "corpus-1",
    text: str | None = None,
    source_tier: str = "chunk",
    metadata: dict | None = None,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=parent_id or f"parent-{chunk_id}",
        doc_id=doc_id or f"doc-{chunk_id}",
        corpus_id=corpus_id,
        text=text or f"distinct evidence passage number {chunk_id} about the topic",
        score=score,
        source_tier=source_tier,
        metadata=metadata or {},
    )


def _ids(packet) -> list[str]:
    return [c.chunk_id for c in packet.items]


# ── Stage 1: relative floor (keep-best-4) ─────────────────────────────────
def test_floor_drops_low_relative_scores_but_keeps_best_four():
    ranked = [
        _chunk("c1", score=1.0),
        _chunk("c2", score=0.9),
        _chunk("c3", score=0.8),
        _chunk("c4", score=0.7),
        # Below FLOOR_RATIO * top (0.35) AND outside the keep-best-4 window.
        _chunk("c5", score=0.2),
        _chunk("c6", score=0.1),
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert _ids(packet) == ["c1", "c2", "c3", "c4"]
    assert packet.diagnostics["floor"]["dropped"] == 2
    assert packet.diagnostics["floor"]["bounded"] is True


def test_floor_keep_best_four_even_when_all_weak():
    # A uniformly weak bounded pool still yields MIN_KEEP candidates.
    ranked = [_chunk(f"c{i}", score=0.05 * (6 - i)) for i in range(6)]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert len(packet.items) == MIN_KEEP


def test_floor_skipped_for_unbounded_logit_pool():
    # Raw-logit pools have no ratio semantics; the floor must not delete by
    # ratio there (only the keep-best-4 / allocation bounds apply).
    ranked = [
        _chunk("c1", score=-1.0),
        _chunk("c2", score=-2.0),
        _chunk("c3", score=-3.0),
        _chunk("c4", score=-4.0),
        _chunk("c5", score=-9.0),
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert packet.diagnostics["floor"]["bounded"] is False
    assert packet.diagnostics["floor"]["dropped"] == 0
    assert len(packet.items) == 5


# ── Stage 2: parent coalesce + sibling_hits ───────────────────────────────
def test_parent_coalesce_keeps_best_child_and_records_sibling_hits():
    ranked = [
        _chunk("c1", score=0.9, parent_id="shared-parent", doc_id="doc-A"),
        _chunk("c2", score=0.6, parent_id="shared-parent", doc_id="doc-A"),
        _chunk("c3", score=0.5, parent_id="shared-parent", doc_id="doc-A"),
        _chunk("c4", score=0.4, parent_id="other-parent", doc_id="doc-B"),
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert packet.diagnostics["coalesce"]["sibling_hits"] == 2
    # Best child of the shared parent survives, scored at the member max.
    survivors = [c for c in packet.items if c.parent_id == "shared-parent"]
    assert len(survivors) == 1
    assert survivors[0].chunk_id == "c1"
    assert survivors[0].score == 0.9
    assert survivors[0].metadata.get("curation_sibling_hits") == 2


# ── Stage 3: near-duplicate drop ──────────────────────────────────────────
def test_near_duplicate_drop_removes_lower_scored_copy():
    same_text = (
        "the quick brown fox jumps over the lazy dog near the river bank today"
    )
    ranked = [
        _chunk("c1", score=0.9, parent_id="p1", doc_id="d1", text=same_text),
        _chunk("c2", score=0.8, parent_id="p2", doc_id="d2", text=same_text),
        _chunk("c3", score=0.7, parent_id="p3", doc_id="d3"),
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert packet.diagnostics["near_dup"]["dropped"] == 1
    assert "c1" in _ids(packet)
    assert "c2" not in _ids(packet)


# ── Stage 4a: per-document cap on the fill pass ───────────────────────────
def test_per_document_cap_limits_fill_seats():
    # Six distinct-parent chunks from ONE document; fill must cap at PER_DOC_CAP.
    ranked = [
        _chunk(f"c{i}", score=0.9 - 0.05 * i, parent_id=f"p{i}", doc_id="doc-A")
        for i in range(6)
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert len(packet.items) == PER_DOC_CAP
    assert packet.diagnostics["allocation"]["doc_counts"]["doc-a"] == PER_DOC_CAP


# ── Stage 4b: side-guarantee seats are cap-exempt ─────────────────────────
def test_side_guarantee_seats_low_ranked_side_chunk():
    ranked = [
        _chunk("c1", score=0.95, doc_id="doc-A"),
        _chunk("c2", score=0.90, doc_id="doc-A"),
        _chunk("c3", score=0.85, doc_id="doc-A"),
        _chunk("c4", score=0.80, doc_id="doc-A"),
        # Low-ranked chunk from another doc, but it is the only evidence for
        # query side "alpha" — it must still earn a guaranteed seat.
        _chunk(
            "c5",
            score=0.40,
            doc_id="doc-B",
            metadata={"query_grounding": {"matched": ["alpha"]}},
        ),
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=3,
    )
    assert packet.diagnostics["allocation"]["side_seats"] >= 1
    assert "c5" in _ids(packet)


# ── Stage 4c: corpus reservation guarantees each requested corpus ─────────
def test_corpus_reservation_seats_each_requested_corpus():
    ranked = [
        _chunk("c1", score=0.95, corpus_id="corpus-1"),
        _chunk("c2", score=0.90, corpus_id="corpus-1"),
        _chunk("c3", score=0.85, corpus_id="corpus-1"),
        # corpus-2 best candidate clears the reservation bound (>= 0.30 of top).
        _chunk("c4", score=0.50, corpus_id="corpus-2"),
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=2,
        multi_corpus=True,
        corpus_ids=["corpus-1", "corpus-2"],
    )
    assert packet.diagnostics["allocation"]["corpus_seats"] >= 1
    assert "c4" in _ids(packet)


# ── Stage 5: ordering ─────────────────────────────────────────────────────
def test_documents_ordered_by_best_score_then_rerank_within_doc():
    ranked = [
        _chunk("a-high", score=0.9, parent_id="pa1", doc_id="doc-A"),
        _chunk("b-mid", score=0.7, parent_id="pb1", doc_id="doc-B"),
        _chunk("a-low", score=0.5, parent_id="pa2", doc_id="doc-A"),
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    # doc-A (best 0.9) precedes doc-B (best 0.7); within doc-A rerank order.
    assert _ids(packet) == ["a-high", "a-low", "b-mid"]


# ── Stage 6: emit / determinism ───────────────────────────────────────────
def test_packet_hash_is_deterministic_for_identical_input():
    def make() -> list[SourceChunk]:
        return [
            _chunk("c1", score=0.9, doc_id="doc-A"),
            _chunk("c2", score=0.7, doc_id="doc-B"),
            _chunk("c3", score=0.5, doc_id="doc-C"),
        ]

    packet_a = build_packet(
        make(),
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    packet_b = build_packet(
        make(),
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert packet_a.packet_hash
    assert packet_a.packet_hash == packet_b.packet_hash
    assert _ids(packet_a) == _ids(packet_b)


def test_packet_hash_changes_with_different_selection():
    base = [
        _chunk("c1", score=0.9, doc_id="doc-A"),
        _chunk("c2", score=0.7, doc_id="doc-B"),
    ]
    other = base + [_chunk("c3", score=0.5, doc_id="doc-C")]
    packet_a = build_packet(
        base,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    packet_b = build_packet(
        other,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert packet_a.packet_hash != packet_b.packet_hash


def test_empty_input_yields_empty_packet():
    packet = build_packet(
        [],
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    assert packet.items == []
    assert packet.packet_hash == ""


def test_ledger_records_role_for_every_item():
    ranked = [
        _chunk("c1", score=0.9, doc_id="doc-A"),
        _chunk("c2", score=0.7, doc_id="doc-B"),
    ]
    packet = build_packet(
        ranked,
        query="q",
        intent=_INTENT,
        tier=RetrievalTier.qdrant_mongo,
        final_top_k=8,
    )
    ledger = packet.diagnostics["ledger"]
    assert len(ledger) == len(packet.items)
    assert all(entry["role"] for entry in ledger)
    assert all("p" in entry and "rerank_idx" in entry for entry in ledger)


# ── Relative low-confidence gate (shadows the absolute threshold) ─────────
def test_relative_standout_detects_clear_winner():
    winner = [
        _chunk("c1", score=-1.0),
        _chunk("c2", score=-8.0),
        _chunk("c3", score=-9.0),
        _chunk("c4", score=-10.0),
    ]
    assert _top_is_relative_standout(winner) is True


def test_relative_standout_rejects_flat_junk_pool():
    flat = [
        _chunk("c1", score=-8.0),
        _chunk("c2", score=-8.1),
        _chunk("c3", score=-8.2),
        _chunk("c4", score=-8.3),
    ]
    assert _top_is_relative_standout(flat) is False


def test_relative_gate_off_keeps_legacy_absolute_behavior():
    # Unrelated pool, low absolute top score, no term overlap -> legacy drops.
    ranked = [
        SourceChunk(
            chunk_id="c1",
            parent_id="p1",
            doc_id="d1",
            corpus_id="corpus",
            text="weighted regression and out-of-the-money option glossary",
            score=-2.841,
            source_tier="chunk",
        )
    ]
    assert _should_drop_low_confidence_rerank(
        ranked,
        "what is chldani",
        rerank_enabled=True,
        score_scale="logit",
        low_confidence_threshold=-2.5,
    )
    # relative_gate=False is the default and must match the legacy result.
    assert _should_drop_low_confidence_rerank(
        ranked,
        "what is chldani",
        rerank_enabled=True,
        score_scale="logit",
        low_confidence_threshold=-2.5,
        relative_gate=False,
    )


def test_relative_gate_on_uses_pool_relative_decision():
    # A clear relative winner with no lexical overlap is KEPT under the
    # relative gate (the absolute path would drop purely on the constant).
    ranked = [
        _chunk("c1", score=-1.0, text="completely unrelated semantic content here"),
        _chunk("c2", score=-8.0, text="more unrelated filler text for the pool"),
        _chunk("c3", score=-9.0, text="even more unrelated filler text today"),
        _chunk("c4", score=-10.0, text="final unrelated filler passage ends"),
    ]
    assert (
        _should_drop_low_confidence_rerank(
            ranked,
            "what is chldani",
            rerank_enabled=True,
            score_scale="logit",
            low_confidence_threshold=-2.5,
            relative_gate=True,
        )
        is False
    )


def test_relative_gate_still_drops_flat_unrelated_pool():
    ranked = [
        _chunk("c1", score=-8.0, text="completely unrelated semantic content here"),
        _chunk("c2", score=-8.1, text="more unrelated filler text for the pool"),
        _chunk("c3", score=-8.2, text="even more unrelated filler text today"),
        _chunk("c4", score=-8.3, text="final unrelated filler passage ends"),
    ]
    assert (
        _should_drop_low_confidence_rerank(
            ranked,
            "what is chldani",
            rerank_enabled=True,
            score_scale="logit",
            low_confidence_threshold=-2.5,
            relative_gate=True,
        )
        is True
    )
