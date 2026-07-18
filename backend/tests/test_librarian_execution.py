from __future__ import annotations

import hashlib

from models.librarian_query_plan import LibrarianShortlistItemV1
from pydantic import ValidationError
import pytest
from models.schemas import SourceChunk
from services.retriever.librarian_planner import (
    apply_librarian_execution_plan,
    build_query_plan_v1,
)
from services.retriever.planned_fusion import (
    PlannedPool,
    apply_librarian_two_lane_allocation,
    cap_planned_candidates_by_affinity,
    fuse_planned_pools,
    reserve_planned_finalists,
)
from services.retriever.query_plan import build_query_plan_v2


def _version() -> str:
    return "sha256:" + hashlib.sha256(b"l3-state").hexdigest()


def _shortlist():
    return (
        LibrarianShortlistItemV1(
            corpus_id="c",
            doc_id="story",
            title="Story Craft",
            summary="Directing, narrative, emotion, and editing.",
            score=0.95,
        ),
        LibrarianShortlistItemV1(
            corpus_id="c",
            doc_id="camera",
            title="Camera Craft",
            summary="Camera direction, lenses, framing, and movement.",
            score=0.90,
        ),
    )


def _chunk(
    chunk_id: str,
    doc_id: str,
    score: float,
    lanes: tuple[str, ...],
    *,
    corpus_id: str = "c",
):
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"parent-{chunk_id}",
        doc_id=doc_id,
        corpus_id=corpus_id,
        doc_name=f"{doc_id}.md",
        text=f"Grounded evidence for {doc_id}",
        score=score,
        source_tier="child",
        metadata={
            "planned_lanes": list(lanes),
            "planned_lane_grounding": {lane: 1.0 for lane in lanes},
        },
    )


def test_simple_plan_is_byte_parity_noop():
    base = build_query_plan_v2("What is story craft?", corpus_ids=["c"])
    librarian = build_query_plan_v1(
        "What is story craft?",
        corpus_id="c",
        corpus_doc_version=_version(),
    )

    overlaid, policy = apply_librarian_execution_plan(base, librarian)

    assert overlaid is base
    assert policy.active is False
    assert policy.lane_seat_quotas == {}


def test_relationship_plan_compiles_into_one_required_lane_set():
    base = build_query_plan_v2(
        "Compare narrative directing with camera optics.",
        corpus_ids=["c"],
    )
    librarian = build_query_plan_v1(
        "Compare narrative directing with camera optics.",
        corpus_id="c",
        corpus_doc_version=_version(),
        shortlist=_shortlist(),
    )

    overlaid, policy = apply_librarian_execution_plan(base, librarian)

    assert policy.active is True
    assert overlaid.budget_obligation_count == len(
        [probe for probe in base.probes if probe.required]
    )
    assert len(overlaid.lanes) == 3
    assert overlaid.lanes[0].role == "original"
    core_lanes = [lane for lane in overlaid.lanes if lane.role == "core"]
    assert len(core_lanes) == 2
    assert all(lane.required for lane in core_lanes)
    assert list(policy.lane_seat_quotas.values()) == [4, 4]
    assert sum(policy.lane_seat_quotas.values()) == 8
    assert {
        hint["doc_id"]
        for values in policy.document_route_hints.values()
        for hint in values
    } == {"story", "camera"}


def test_query_plan_rejects_total_seat_budget_drift():
    plan = build_query_plan_v1(
        "Compare narrative directing with camera optics.",
        corpus_id="c",
        corpus_doc_version=_version(),
        shortlist=_shortlist(),
    )
    payload = plan.model_dump(mode="json")
    payload["subqueries"][0]["seat_quota"] = 3

    with pytest.raises(ValidationError, match="total budget"):
        type(plan).model_validate(payload)


def test_generalized_quota_reservation_prevents_winner_take_all():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    ranked = [
        _chunk("a1", "story", 1.0, (lane_a,)),
        _chunk("a2", "story", 0.9, (lane_a,)),
        _chunk("a3", "story-extra", 0.8, (lane_a,)),
        _chunk("b1", "camera", 0.75, (lane_b,)),
        _chunk("b2", "camera-extra", 0.70, (lane_b,)),
    ]

    selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked[:4],
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=["c"],
        max_candidates=4,
        max_per_document=2,
        lane_seat_quotas={lane_a: 2, lane_b: 2},
    )

    assert len(selected) == 4
    assert receipt["lane_quota_fulfilled"] == {lane_a: 2, lane_b: 2}
    assert receipt["lane_quota_spillover"] == {lane_a: 0, lane_b: 0}
    selected_ids = {chunk.chunk_id for chunk in selected}
    assert {"b1", "b2"} <= selected_ids


def test_unfilled_quota_spills_without_weakening_support_gate():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    ranked = [
        _chunk("a1", "story", 1.0, (lane_a,)),
        _chunk("a2", "story-extra", 0.9, (lane_a,)),
        _chunk("b1", "camera", 0.8, (lane_b,)),
        _chunk("x1", "noise", 0.7, ()),
    ]

    selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked,
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=["c"],
        max_candidates=4,
        lane_seat_quotas={lane_a: 2, lane_b: 2},
    )

    assert len(selected) == 4
    assert receipt["lane_quota_fulfilled"][lane_b] == 1
    assert receipt["lane_quota_spillover"][lane_b] == 1


def test_one_candidate_cannot_fill_two_subquery_seats():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    ranked = [
        _chunk("shared", "shared", 1.0, (lane_a, lane_b)),
        _chunk("a1", "story", 0.9, (lane_a,)),
        _chunk("b1", "camera", 0.8, (lane_b,)),
    ]

    _selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked,
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=["c"],
        max_candidates=3,
        lane_seat_quotas={lane_a: 1, lane_b: 1},
    )

    reserved = receipt["lane_quota_reservations"]
    assert reserved[lane_a][0] != reserved[lane_b][0]


def test_oversubscribed_quotas_are_fair_and_receipt_matches_output():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    ranked = [
        *[
            _chunk(f"a{index}", f"story-{index}", 1.0 - index / 100, (lane_a,))
            for index in range(1, 5)
        ],
        *[
            _chunk(f"b{index}", f"camera-{index}", 0.9 - index / 100, (lane_b,))
            for index in range(1, 5)
        ],
    ]

    selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked[:4],
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=["c"],
        max_candidates=4,
        max_per_document=2,
        lane_seat_quotas={lane_a: 4, lane_b: 4},
    )

    assert len(selected) == 4
    assert receipt["lane_seat_quotas_requested"] == {lane_a: 4, lane_b: 4}
    assert receipt["lane_seat_quotas"] == {lane_a: 2, lane_b: 2}
    assert receipt["lane_quota_fulfilled"] == {lane_a: 2, lane_b: 2}
    output_ids = {chunk.chunk_id for chunk in selected}
    receipt_ids = {
        chunk_id
        for values in receipt["lane_quota_reservations"].values()
        for chunk_id in values
    }
    assert receipt_ids == output_ids


def test_three_subquery_quota_allocation_is_stable_and_exact():
    lanes = [
        "librarian_1_main",
        "librarian_2_facet",
        "librarian_3_hop",
    ]
    quotas = dict(zip(lanes, (3, 3, 2)))
    ranked = [
        _chunk(f"{lane}-{index}", f"{lane}-doc-{index}", score, (lane,))
        for lane, base in zip(lanes, (1.0, 0.9, 0.8))
        for index, score in enumerate((base, base - 0.01, base - 0.02), 1)
    ]

    first, first_receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked[:8],
        required_lane_ids=lanes,
        corpus_ids=["c"],
        max_candidates=8,
        max_per_document=2,
        lane_seat_quotas=quotas,
    )
    second, second_receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked[:8],
        required_lane_ids=lanes,
        corpus_ids=["c"],
        max_candidates=8,
        max_per_document=2,
        lane_seat_quotas=quotas,
    )

    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert first_receipt["lane_quota_fulfilled"] == quotas
    assert (
        second_receipt["lane_quota_reservations"]
        == first_receipt["lane_quota_reservations"]
    )


def test_multi_corpus_overflow_receipts_only_returned_reservations():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    ranked = [
        *[
            _chunk(f"a{index}", f"a-doc-{index}", 1.0 - index / 100, (lane_a,))
            for index in range(1, 5)
        ],
        *[
            _chunk(f"b{index}", f"b-doc-{index}", 0.9 - index / 100, (lane_b,))
            for index in range(1, 5)
        ],
        _chunk("c2", "other", 0.7, (), corpus_id="c2"),
    ]

    selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked[:8],
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=["c", "c2"],
        max_candidates=8,
        max_per_document=2,
        lane_seat_quotas={lane_a: 4, lane_b: 4},
    )

    assert len(selected) == 8
    assert receipt["lane_quota_fulfilled"] == {lane_a: 4, lane_b: 4}
    assert "c2" not in receipt["corpus_reservations"]
    assert "c2" in receipt["skipped_corpus_reservations"]
    assert receipt["corpus_reservation_details"]["c2"]["outcome"] == (
        "displaced_by_librarian_seat_budget"
    )


def test_two_lane_allocation_stays_inside_each_librarian_subquery():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    ranked = [
        _chunk("a-expansion", "a-expansion", 1.0, (lane_a,)),
        _chunk("a-anchor", "story", 0.95, (lane_a,)),
        _chunk("b-expansion", "b-expansion", 0.90, (lane_b,)),
        _chunk("b-anchor", "camera", 0.85, (lane_b,)),
    ]
    selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=[ranked[0], ranked[2]],
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=[],
        max_candidates=2,
        lane_seat_quotas={lane_a: 1, lane_b: 1},
    )

    allocated, diagnostics, reconciled = apply_librarian_two_lane_allocation(
        selected,
        ranked,
        query="How do story and camera relate?",
        required_lane_ids=[lane_a, lane_b],
        lane_seat_quotas={lane_a: 1, lane_b: 1},
        reservation_receipt=receipt,
        anchor_ratio=1.0,
        anchor_threshold=0.0,
        expansion_threshold=0.0,
    )

    assert diagnostics["scope"] == "within_librarian_subquery_seats"
    assert {row["side"] for row in diagnostics["selected"]} == {lane_a, lane_b}
    assert {chunk.doc_id for chunk in allocated} == {"story", "camera"}
    assert reconciled["lane_quota_fulfilled"] == {lane_a: 1, lane_b: 1}


def test_exact_query_lane_is_protected_before_librarian_quota_budgeting():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    exact = _chunk("exact", "exact-doc", 0.1, ("original",))
    ranked = [
        *[
            _chunk(f"a{index}", f"a-doc-{index}", 1.0 - index / 100, (lane_a,))
            for index in range(1, 5)
        ],
        *[
            _chunk(f"b{index}", f"b-doc-{index}", 0.9 - index / 100, (lane_b,))
            for index in range(1, 5)
        ],
        exact,
    ]

    selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked[:8],
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=[],
        max_candidates=8,
        max_per_document=2,
        protected_lane_ids=["original"],
        lane_seat_quotas={lane_a: 4, lane_b: 4},
    )

    assert {chunk.chunk_id for chunk in selected} >= {"exact"}
    assert receipt["protected_lane_reservations"] == {"original": "exact"}
    assert receipt["displaced_protected_lane_ids"] == []
    assert sum(receipt["lane_seat_quotas"].values()) == 7
    assert sum(receipt["lane_quota_fulfilled"].values()) == 7


def test_two_lane_does_not_promote_supported_spillover_to_reserved_seat():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    ranked = [
        _chunk("a1", "a1", 1.0, (lane_a,)),
        _chunk("b1", "b1", 0.9, (lane_b,)),
        _chunk("a-spill", "a-spill", 0.8, (lane_a,)),
        _chunk("neutral", "neutral", 0.7, ()),
    ]
    selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked,
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=[],
        max_candidates=4,
        lane_seat_quotas={lane_a: 1, lane_b: 1},
    )

    _allocated, _diagnostics, reconciled = apply_librarian_two_lane_allocation(
        selected,
        ranked,
        query="How do a and b relate?",
        required_lane_ids=[lane_a, lane_b],
        lane_seat_quotas={lane_a: 1, lane_b: 1},
        reservation_receipt=receipt,
        anchor_ratio=0.5,
        anchor_threshold=0.0,
        expansion_threshold=0.0,
    )

    assert receipt["lane_quota_fulfilled"] == {lane_a: 1, lane_b: 1}
    assert reconciled["lane_quota_fulfilled"] == {lane_a: 1, lane_b: 1}


def test_exact_query_overlap_counts_inside_equal_side_quota():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    exact_a = _chunk("exact-a", "a-exact", 1.0, ("original", lane_a))
    ranked = [
        exact_a,
        *[
            _chunk(f"a{index}", f"a-doc-{index}", 0.99 - index / 100, (lane_a,))
            for index in range(1, 5)
        ],
        *[
            _chunk(f"b{index}", f"b-doc-{index}", 0.90 - index / 100, (lane_b,))
            for index in range(1, 5)
        ],
    ]

    selected, receipt = reserve_planned_finalists(
        ranked,
        preferred=ranked[:8],
        required_lane_ids=[lane_a, lane_b],
        corpus_ids=[],
        max_candidates=8,
        max_per_document=2,
        protected_lane_ids=["original"],
        lane_seat_quotas={lane_a: 4, lane_b: 4},
    )

    assert len(selected) == 8
    assert receipt["protected_lane_reservations"] == {"original": "exact-a"}
    assert receipt["lane_seat_quotas"] == {lane_a: 4, lane_b: 4}
    assert receipt["lane_quota_fulfilled"] == {lane_a: 4, lane_b: 4}
    assert len(receipt["lane_quota_reservations"][lane_a]) == 4
    assert len(receipt["lane_quota_reservations"][lane_b]) == 4


def test_fusion_keeps_all_lanes_and_deterministic_max_affinity_tag():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    first = _chunk("shared", "shared-doc", 0.7, (lane_a,))
    first.metadata["planned_lane_grounding"] = {lane_a: 0.7}
    first.metadata["document_route_lanes"] = {lane_a: 0.8}
    second = _chunk("shared", "shared-doc", 0.9, (lane_b,))
    second.metadata["planned_lane_grounding"] = {lane_b: 0.7}
    second.metadata["document_route_lanes"] = {lane_b: 0.9}

    fused, _diagnostics = fuse_planned_pools(
        [
            PlannedPool(lane_a, "dense", (first,)),
            PlannedPool(lane_b, "summary", (second,)),
        ],
        max_candidates=4,
    )

    assert len(fused) == 1
    assert fused[0].metadata["planned_lanes"] == [lane_a, lane_b]
    assert set(fused[0].metadata["planned_lane_affinity"]) == {lane_a, lane_b}
    assert fused[0].metadata["planned_max_affinity_lane"] == lane_b
    assert fused[0].metadata["planned_max_affinity"] == 0.9


def test_fusion_equal_affinity_tie_is_stable_under_reversed_pool_order():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    first = _chunk("shared", "shared-doc", 0.7, (lane_a,))
    first.metadata["planned_lane_grounding"] = {lane_a: 0.8}
    second = _chunk("shared", "shared-doc", 0.9, (lane_b,))
    second.metadata["planned_lane_grounding"] = {lane_b: 0.8}
    pools = [
        PlannedPool(lane_a, "dense", (first,)),
        PlannedPool(lane_b, "summary", (second,)),
    ]

    forward, _ = fuse_planned_pools(pools, max_candidates=4)
    reverse, _ = fuse_planned_pools(list(reversed(pools)), max_candidates=4)

    assert forward[0].metadata["planned_lane_affinity"] == (
        reverse[0].metadata["planned_lane_affinity"]
    )
    assert forward[0].metadata["planned_max_affinity_lane"] == lane_a
    assert reverse[0].metadata["planned_max_affinity_lane"] == lane_a


def test_graph_pool_keeps_strongest_librarian_subquery_attribution():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    graph = _chunk("graph-shared", "bridge-doc", 0.95, (lane_a, lane_b))
    graph.metadata["planned_lane_grounding"] = {lane_a: 0.8, lane_b: 0.9}

    fused, _ = fuse_planned_pools(
        [PlannedPool("graph", "graph", (graph,))],
        max_candidates=4,
    )

    assert fused[0].metadata["planned_lanes"] == sorted([lane_a, lane_b, "graph"])
    assert fused[0].metadata["planned_max_affinity_lane"] == lane_b
    assert fused[0].metadata["planned_max_affinity"] == 0.9


def test_per_subquery_rerank_caps_bound_union_deterministically():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    chunks = [
        _chunk(f"a{index}", f"a-doc-{index}", 1.0, (lane_a,)) for index in range(1, 4)
    ] + [_chunk(f"b{index}", f"b-doc-{index}", 0.9, (lane_b,)) for index in range(1, 4)]
    for chunk in chunks:
        lane_id = (chunk.metadata or {})["planned_lanes"][0]
        chunk.metadata["planned_max_affinity_lane"] = lane_id
        chunk.metadata["planned_max_affinity"] = 1.0

    capped, diagnostics = cap_planned_candidates_by_affinity(
        chunks,
        lane_rerank_caps={lane_a: 4, lane_b: 4},
        global_rerank_cap=4,
    )
    replay, replay_diagnostics = cap_planned_candidates_by_affinity(
        chunks,
        lane_rerank_caps={lane_a: 4, lane_b: 4},
        global_rerank_cap=4,
    )

    assert [chunk.chunk_id for chunk in capped] == ["a1", "a2", "b1", "b2"]
    assert [chunk.chunk_id for chunk in replay] == [chunk.chunk_id for chunk in capped]
    assert diagnostics["effective"] == {lane_a: 2, lane_b: 2}
    assert diagnostics["effective_sum"] <= diagnostics["global_rerank_cap"]
    assert diagnostics["assigned_counts"] == {lane_a: 2, lane_b: 2}
    assert replay_diagnostics == diagnostics


def test_per_subquery_caps_bound_original_graph_and_unassigned_union():
    lane_a = "librarian_1_side_a"
    lane_b = "librarian_2_side_b"
    original = _chunk("original", "original-doc", 1.0, ("original",))
    original.metadata["planned_max_affinity_lane"] = "original"
    graph = _chunk("graph", "graph-doc", 0.99, ("graph",))
    graph.metadata["planned_max_affinity_lane"] = "graph"
    unassigned = _chunk("unassigned", "unknown-doc", 0.98, ())
    chunks = [original, graph, unassigned]
    for lane_id, prefix in ((lane_a, "a"), (lane_b, "b")):
        for index in range(20):
            chunk = _chunk(
                f"{prefix}{index}",
                f"{prefix}-doc-{index}",
                0.9 - index / 100,
                (lane_id,),
            )
            chunk.metadata["planned_max_affinity_lane"] = lane_id
            chunk.metadata["planned_max_affinity"] = 1.0
            chunks.append(chunk)

    capped, diagnostics = cap_planned_candidates_by_affinity(
        chunks,
        lane_rerank_caps={lane_a: 16, lane_b: 16},
        global_rerank_cap=38,
    )

    selected_ids = {chunk.chunk_id for chunk in capped}
    assert "original" in selected_ids
    assert any(chunk_id.startswith("a") for chunk_id in selected_ids)
    assert any(chunk_id.startswith("b") for chunk_id in selected_ids)
    assert len(capped) <= 32
    assert len(capped) == diagnostics["output_candidates"]
    assert diagnostics["union_limit"] == diagnostics["effective_sum"] == 32
    assert diagnostics["assigned_counts"][lane_a] <= 16
    assert diagnostics["assigned_counts"][lane_b] <= 16
