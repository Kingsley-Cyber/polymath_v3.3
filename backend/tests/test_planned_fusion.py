from models.schemas import SourceChunk
from services.retriever.planned_fusion import (
    PlannedPool,
    annotate_planned_lane_grounding,
    dedupe_document_lane_finalists,
    dedupe_parent_finalists,
    filter_grounded_planned_candidates,
    fuse_planned_pools,
    grounded_planned_lane_ids,
    limit_candidates_per_document,
    planned_lane_grounding,
    reserve_planned_finalists,
)


def _chunk(chunk_id: str, corpus_id: str, score: float = 0.5) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"p-{chunk_id}",
        doc_id=f"d-{chunk_id}",
        corpus_id=corpus_id,
        text=f"text {chunk_id}",
        score=score,
        source_tier="vector",
    )


def test_fusion_reserves_required_lanes_and_corpora():
    pools = [
        PlannedPool("original", "dense", tuple(_chunk(f"a{i}", "a") for i in range(8))),
        PlannedPool("purple_ocean", "dense", (_chunk("purple", "a"),), required=True),
        PlannedPool(
            "sticky_message", "lexical", (_chunk("sticky", "b"),), required=True
        ),
    ]

    result, diagnostics = fuse_planned_pools(
        pools,
        max_candidates=4,
        corpus_ids=["a", "b"],
    )
    ids = {chunk.chunk_id for chunk in result}

    assert {"purple", "sticky"} <= ids
    assert {chunk.corpus_id for chunk in result} == {"a", "b"}
    assert diagnostics["selected_candidates"] == 4


def test_fusion_excludes_pipeline_status_reports_from_evidence():
    report = _chunk("report", "a")
    report.doc_name = "ocr-completion-report.md"
    evidence = _chunk("evidence", "a")
    evidence.doc_name = "cinematography.md"

    result, diagnostics = fuse_planned_pools(
        [PlannedPool("ai", "lexical", (report, evidence), required=True)],
        max_candidates=4,
    )

    assert [chunk.chunk_id for chunk in result] == ["evidence"]
    assert diagnostics["excluded_operational_artifacts"] == 1


def test_grounding_filter_fails_open_for_short_acronym_lane():
    grounded = _chunk("grounded", "a")
    grounded.metadata = {"planned_lane_grounding": {"ai": 1.0}}
    semantic = _chunk("semantic", "a")

    result, diagnostics = filter_grounded_planned_candidates(
        [grounded, semantic], ["ai"]
    )

    assert result == [grounded, semantic]
    assert diagnostics["applied"] is False
    assert diagnostics["reason"] == "short_acronym_lane_fail_open"


def test_pre_rerank_document_cap_preserves_order_and_unknown_identity():
    first = _chunk("a-1", "a")
    first.doc_id = "doc-a"
    duplicate = _chunk("a-2", "a")
    duplicate.doc_id = "doc-a"
    overflow = _chunk("a-3", "a")
    overflow.doc_id = "doc-a"
    second = _chunk("b-1", "a")
    second.doc_id = "doc-b"
    unknown = _chunk("unknown", "a")
    unknown.doc_id = ""

    result, dropped = limit_candidates_per_document(
        [first, duplicate, overflow, second, unknown],
        max_candidates=5,
        max_per_document=2,
    )

    assert [chunk.chunk_id for chunk in result] == ["a-1", "a-2", "b-1", "unknown"]
    assert dropped == 1


def test_fusion_dedupes_same_chunk_across_retrievers():
    duplicate = _chunk("same", "a")
    result, diagnostics = fuse_planned_pools(
        [
            PlannedPool("original", "dense", (duplicate,)),
            PlannedPool("original", "lexical", (duplicate.model_copy(),)),
        ],
        max_candidates=8,
    )

    assert [chunk.chunk_id for chunk in result] == ["same"]
    assert diagnostics["input_candidates"] == 2
    assert diagnostics["unique_candidates"] == 1


def test_required_lane_prefers_lexical_evidence_over_dense_neighbor():
    result, _ = fuse_planned_pools(
        [
            PlannedPool(
                "sticky_message",
                "dense",
                (_chunk("semantic-neighbor", "a"),),
                required=True,
            ),
            PlannedPool(
                "sticky_message",
                "lexical",
                (_chunk("sticky-exact", "a"),),
                required=True,
            ),
        ],
        max_candidates=1,
    )

    assert [chunk.chunk_id for chunk in result] == ["sticky-exact"]


def test_required_lane_prefers_grounded_named_concept_over_generic_lexical_hit():
    generic = _chunk("generic-stick", "a")
    generic.text = "A wooden stick rotates around its center of gravity."
    exact = _chunk("made-to-stick", "a")
    exact.doc_name = "Chip Heath and Dan Heath - Made to Stick"
    exact.text = "The six principles make ideas understandable and memorable."

    result, _ = fuse_planned_pools(
        [
            PlannedPool(
                "made_to_stick_principles",
                "lexical",
                (generic, exact),
                required=True,
                anchor_phrase="Made to Stick principles",
                anchor_terms=("made", "stick", "principles"),
            )
        ],
        max_candidates=1,
    )

    assert [chunk.chunk_id for chunk in result] == ["made-to-stick"]
    assert grounded_planned_lane_ids(
        result,
        ["made_to_stick_principles"],
    ) == ["made_to_stick_principles"]


def test_provenance_only_lane_does_not_count_as_grounded_coverage():
    generic = _chunk("generic", "a")
    generic.metadata = {
        "planned_lanes": ["made_to_stick_principles"],
        "planned_lane_grounding": {"made_to_stick_principles": 0.3333},
    }

    assert (
        grounded_planned_lane_ids(
            [generic],
            ["made_to_stick_principles"],
        )
        == []
    )


def test_multi_side_filter_removes_generic_fillers_after_full_coverage():
    purple = _chunk("purple", "a")
    purple.metadata = {
        "planned_lane_grounding": {"purple_ocean": 3.0},
    }
    sticky = _chunk("sticky", "b")
    sticky.metadata = {
        "planned_lane_grounding": {"made_to_stick": 3.0},
    }
    generic = _chunk("generic", "b")
    generic.metadata = {
        "planned_lanes": ["made_to_stick"],
        "planned_lane_grounding": {"made_to_stick": 0.3333},
    }

    result, diagnostics = filter_grounded_planned_candidates(
        [generic, purple, sticky],
        ["purple_ocean", "made_to_stick"],
    )

    assert [chunk.chunk_id for chunk in result] == ["purple", "sticky"]
    assert diagnostics["applied"] is True
    assert diagnostics["dropped"] == 1


def test_multi_side_filter_preserves_validated_graph_bridge():
    purple = _chunk("purple", "a")
    purple.metadata = {"planned_lane_grounding": {"purple_ocean": 3.0}}
    sticky = _chunk("sticky", "b")
    sticky.metadata = {"planned_lane_grounding": {"made_to_stick": 3.0}}
    bridge = _chunk("bridge", "b")
    bridge.text = "This relation connects sticky ideas to memorable messaging."
    bridge.provenance = [
        {
            "retriever": "graph",
            "entity": "memorable messaging",
            "predicate": "supports",
        }
    ]
    annotate_planned_lane_grounding(
        [bridge],
        lane_id="made_to_stick",
        anchor_phrase="Made to Stick principles",
        anchor_phrases=("sticky ideas", "memorable messaging"),
        anchor_terms=("made", "stick", "principles"),
    )

    result, _ = filter_grounded_planned_candidates(
        [purple, sticky, bridge],
        ["purple_ocean", "made_to_stick"],
    )

    assert [chunk.chunk_id for chunk in result] == ["purple", "sticky", "bridge"]


def test_multi_side_filter_degrades_open_when_one_side_is_missing():
    purple = _chunk("purple", "a")
    purple.metadata = {"planned_lane_grounding": {"purple_ocean": 3.0}}
    generic = _chunk("generic", "b")

    result, diagnostics = filter_grounded_planned_candidates(
        [purple, generic],
        ["purple_ocean", "made_to_stick"],
    )

    assert result == [purple]
    assert diagnostics["applied"] is True
    assert diagnostics["reason"] == "partial_grounded_coverage"


def test_single_named_side_filter_removes_token_overlap_fillers():
    exact = _chunk("exact", "a")
    exact.metadata = {"planned_lane_grounding": {"made_to_stick": 3.0}}
    generic = _chunk("generic", "a")
    generic.metadata = {"planned_lane_grounding": {"made_to_stick": 0.3333}}

    result, diagnostics = filter_grounded_planned_candidates(
        [generic, exact],
        ["made_to_stick"],
    )

    assert result == [exact]
    assert diagnostics["applied"] is True
    assert diagnostics["dropped"] == 1


def test_two_word_lane_requires_an_exact_span_or_multiword_alias():
    separated = _chunk("separated", "a")
    separated.text = "A product improves results. The page contains details."
    exact = _chunk("exact", "a")
    exact.text = "The product page should communicate one clear promise."

    fused, _ = fuse_planned_pools(
        [
            PlannedPool(
                "product_page",
                "dense",
                (separated, exact),
                anchor_phrase="product page",
                anchor_terms=("product", "page"),
            )
        ],
        max_candidates=4,
    )
    by_id = {chunk.chunk_id: chunk for chunk in fused}

    assert planned_lane_grounding(by_id["separated"], "product_page") < 0.75
    assert planned_lane_grounding(by_id["exact"], "product_page") > 2.0


def test_document_lane_dedupe_merges_membership_and_provenance():
    first = _chunk("first", "a")
    first.doc_id = "same-doc"
    first.metadata = {
        "planned_lane_grounding": {"purple_ocean": 3.0},
        "corpus_memberships": ["a"],
    }
    first.provenance = [{"retriever": "dense"}]
    second = _chunk("second", "b")
    second.doc_id = "same-doc"
    second.metadata = {
        "planned_lane_grounding": {"purple_ocean": 3.0},
        "corpus_memberships": ["b"],
    }
    second.provenance = [{"retriever": "lexical"}]

    result, duplicates = dedupe_document_lane_finalists([first, second])

    assert result == [first]
    assert duplicates == 1
    assert first.metadata["corpus_memberships"] == ["a", "b"]
    assert {item["retriever"] for item in first.provenance} == {
        "dense",
        "lexical",
    }


def test_finalist_reservations_survive_reranker_order():
    purple = _chunk("purple", "a", 0.9)
    purple.metadata = {
        "planned_lanes": ["purple_ocean"],
        "planned_lane_grounding": {"purple_ocean": 3.0},
    }
    sticky = _chunk("sticky", "b", 0.4)
    sticky.metadata = {
        "planned_lanes": ["sticky_message"],
        "planned_lane_grounding": {"sticky_message": 3.0},
    }
    generic = [_chunk(f"generic-{index}", "a", 1.0 - index / 10) for index in range(4)]
    ranked = [*generic, purple, sticky]

    result, diagnostics = reserve_planned_finalists(
        ranked,
        preferred=generic[:3],
        required_lane_ids=["purple_ocean", "sticky_message"],
        corpus_ids=["a", "b"],
        max_candidates=4,
    )
    ids = {chunk.chunk_id for chunk in result}

    assert {"purple", "sticky"} <= ids
    assert {chunk.corpus_id for chunk in result} == {"a", "b"}
    assert diagnostics["lane_reservations"] == {
        "purple_ocean": "purple",
        "sticky_message": "sticky",
    }


def test_finalist_reservations_reject_weak_or_ungrounded_lane_fillers():
    strong = _chunk("strong", "a", 1.0)
    strong.metadata = {
        "planned_lanes": ["ecommerce"],
        "planned_lane_grounding": {"ecommerce": 2.0},
    }
    weak = _chunk("weak", "a", 0.1)
    weak.metadata = {
        "planned_lanes": ["ai"],
        "planned_lane_grounding": {"ai": 1.0},
    }
    ungrounded = _chunk("ungrounded", "a", 0.9)
    ungrounded.metadata = {"planned_lanes": ["ai"]}

    result, diagnostics = reserve_planned_finalists(
        [strong, ungrounded, weak],
        preferred=[strong],
        required_lane_ids=["ecommerce", "ai"],
        corpus_ids=["a"],
        max_candidates=4,
    )

    assert [chunk.chunk_id for chunk in result] == ["strong"]
    assert diagnostics["lane_reservations"] == {"ecommerce": "strong"}


def test_finalist_reservations_do_not_refill_a_complete_diversity_pack():
    purple = _chunk("purple", "a", 0.9)
    purple.metadata = {
        "planned_lanes": ["purple_ocean"],
        "planned_lane_grounding": {"purple_ocean": 3.0},
    }
    sticky = _chunk("sticky", "b", 0.8)
    sticky.metadata = {
        "planned_lanes": ["made_to_stick"],
        "planned_lane_grounding": {"made_to_stick": 3.0},
    }
    second_purple = _chunk("purple-2", "a", 0.7)
    second_purple.metadata = {
        "planned_lanes": ["purple_ocean"],
        "planned_lane_grounding": {"purple_ocean": 3.0},
    }

    result, _ = reserve_planned_finalists(
        [purple, sticky, second_purple],
        preferred=[purple, sticky],
        required_lane_ids=["purple_ocean", "made_to_stick"],
        corpus_ids=["a", "b"],
        max_candidates=8,
    )

    assert [chunk.chunk_id for chunk in result] == ["purple", "sticky"]


def test_finalist_reservations_respect_broad_document_cap():
    first = _chunk("doc-a-1", "a", 0.9)
    first.doc_id = "doc-a"
    first.metadata = {
        "planned_lanes": ["lane-a", "lane-b"],
        "planned_lane_grounding": {"lane-a": 1.0, "lane-b": 1.0},
    }
    duplicate = _chunk("doc-a-2", "a", 0.8)
    duplicate.doc_id = "doc-a"
    duplicate.metadata = {
        "planned_lanes": ["lane-b"],
        "planned_lane_grounding": {"lane-b": 1.0},
    }
    second = _chunk("doc-b-1", "a", 0.7)
    second.doc_id = "doc-b"
    second.metadata = {
        "planned_lanes": ["lane-b"],
        "planned_lane_grounding": {"lane-b": 1.0},
    }

    result, diagnostics = reserve_planned_finalists(
        [first, duplicate, second],
        preferred=[first, duplicate, second],
        required_lane_ids=["lane-a", "lane-b"],
        corpus_ids=["a"],
        max_candidates=3,
        max_per_document=1,
    )

    assert [chunk.chunk_id for chunk in result] == ["doc-a-1", "doc-b-1"]
    assert diagnostics["max_per_document"] == 1


def test_finalist_reservations_keep_bounded_routed_document_evidence():
    global_top = _chunk("global", "a", 1.0)
    routed_a = _chunk("routed-a", "a", 0.7)
    routed_a.doc_id = "doc-routed-a"
    routed_a.metadata = {
        "planned_lanes": ["books"],
        "planned_lane_grounding": {"books": 2.0},
        "document_route_lanes": {"books": 0.91},
    }
    routed_b = _chunk("routed-b", "a", 0.6)
    routed_b.doc_id = "doc-routed-b"
    routed_b.metadata = {
        "planned_lanes": ["books"],
        "planned_lane_grounding": {"books": 2.0},
        "document_route_lanes": {"books": 0.87},
    }

    result, diagnostics = reserve_planned_finalists(
        [global_top, routed_a, routed_b],
        preferred=[global_top],
        required_lane_ids=["books"],
        corpus_ids=["a"],
        max_candidates=3,
        routed_document_budget=2,
    )

    assert {chunk.chunk_id for chunk in result} == {"global", "routed-a", "routed-b"}
    assert diagnostics["routed_documents_selected"] == [
        "doc-routed-a",
        "doc-routed-b",
    ]
    assert set(diagnostics["routed_document_reservations"]) == {"doc-routed-b"}


def test_parent_finalist_dedupe_merges_lane_provenance():
    first = _chunk("first", "a")
    first.parent_id = "shared-parent"
    first.metadata = {
        "planned_lanes": ["purple_ocean"],
        "planned_lane_grounding": {"purple_ocean": 2.5},
    }
    second = _chunk("second", "b")
    second.parent_id = "shared-parent"
    second.metadata = {
        "planned_lanes": ["sticky_message"],
        "planned_lane_grounding": {"sticky_message": 2.0},
        "corpus_memberships": ["b"],
    }

    result, dropped = dedupe_parent_finalists([first, second])

    assert dropped == 1
    assert len(result) == 1
    assert result[0].metadata["planned_lanes"] == ["purple_ocean", "sticky_message"]
    assert result[0].metadata["corpus_memberships"] == ["a", "b"]
    assert result[0].metadata["planned_lane_grounding"] == {
        "purple_ocean": 2.5,
        "sticky_message": 2.0,
    }
    assert any(
        item.get("retriever") == "parent_finalist_dedupe"
        for item in result[0].provenance
    )
