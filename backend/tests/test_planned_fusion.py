from models.schemas import SourceChunk
from services.retriever.planned_fusion import (
    PlannedPool,
    annotate_planned_lane_grounding,
    dedupe_document_lane_finalists,
    dedupe_enumeration_finalists,
    dedupe_parent_finalists,
    filter_grounded_planned_candidates,
    fuse_planned_pools,
    grounded_planned_lane_ids,
    limit_candidates_per_document,
    order_enumeration_finalists,
    planned_lane_grounding,
    planned_lane_supported,
    prioritize_enumeration_candidates,
    propagate_grounded_lane_aliases,
    reserved_required_lane_ids,
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


def test_corpus_reservation_is_annotated_for_late_context_gates():
    corpus_a = _chunk("corpus-a", "a", score=0.9)
    corpus_b = _chunk("corpus-b", "b", score=0.4)

    selected, diagnostics = reserve_planned_finalists(
        [corpus_a, corpus_b],
        [corpus_a],
        required_lane_ids=[],
        corpus_ids=["a", "b"],
        max_candidates=2,
    )

    by_id = {chunk.chunk_id: chunk for chunk in selected}
    assert diagnostics["corpus_reservations"] == {
        "a": "corpus-a",
        "b": "corpus-b",
    }
    assert by_id["corpus-a"].metadata["planned_corpus_reservations"] == ["a"]
    assert by_id["corpus-b"].metadata["planned_corpus_reservations"] == ["b"]


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


def test_pre_rerank_document_cap_preserves_exact_query_lane():
    generic = [
        _chunk(f"generic-{index}", "a", score=1.0 - index / 10) for index in range(4)
    ]
    exact = _chunk("exact-query-hit", "a", score=0.1)
    exact.metadata = {"planned_lanes": ["original"]}

    result, _ = limit_candidates_per_document(
        [*generic, exact],
        max_candidates=3,
        max_per_document=1,
        protected_lane_ids=["original"],
    )

    assert "exact-query-hit" in {chunk.chunk_id for chunk in result}


def test_pre_rerank_document_cap_preserves_required_lane_beyond_doc_cap():
    first = _chunk("a-1", "a", score=0.9)
    first.doc_id = "doc-a"
    second = _chunk("a-2", "a", score=0.8)
    second.doc_id = "doc-a"
    third = _chunk("a-3", "a", score=0.7)
    third.doc_id = "doc-a"
    required = _chunk("sticky", "a", score=0.2)
    required.doc_id = "doc-a"
    required.metadata = {
        "planned_lanes": ["sticky_message"],
        "document_route_lanes": {"sticky_message": 0.8},
    }
    other = _chunk("b-1", "a", score=0.6)
    other.doc_id = "doc-b"

    result, dropped = limit_candidates_per_document(
        [first, second, third, required, other],
        max_candidates=4,
        max_per_document=2,
        required_lane_ids=["sticky_message"],
    )

    assert [chunk.chunk_id for chunk in result] == ["a-1", "sticky", "b-1"]
    assert dropped == 2


def test_pre_rerank_cap_preserves_strongest_answer_object_document_route():
    exact_doc = _chunk("exact-list", "a", score=0.2)
    exact_doc.doc_id = "exact-doc"
    exact_doc.metadata = {
        "planned_lanes": ["books"],
        "document_route_lanes": {"books": 0.82},
    }
    generic_doc = _chunk("generic-list", "a", score=0.9)
    generic_doc.doc_id = "generic-doc"
    generic_doc.metadata = {
        "planned_lanes": ["books", "books_justification"],
        "document_route_lanes": {
            "books": 0.55,
            "books_justification": 0.75,
        },
    }
    fillers = [_chunk(f"filler-{index}", "a", score=0.8) for index in range(4)]

    result, _dropped = limit_candidates_per_document(
        [generic_doc, *fillers, exact_doc],
        max_candidates=3,
        max_per_document=2,
        required_lane_ids=["books", "books_justification"],
        preferred_route_lane_ids=["books"],
    )

    assert "exact-list" in {chunk.chunk_id for chunk in result}


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


def test_semantic_document_route_plus_lane_descent_counts_as_support():
    routed = _chunk("consumer-desire", "a")
    routed.text = "Existing desire and awareness determine persuasive response."
    routed.metadata = {
        "planned_lanes": ["audience_response"],
        "planned_lane_grounding": {"audience_response": 0.25},
        "document_route_lanes": {"audience_response": 0.47},
    }
    weak_route = routed.model_copy(deep=True)
    weak_route.metadata["document_route_lanes"] = {"audience_response": 0.29}

    assert planned_lane_supported(routed, "audience_response")
    assert not planned_lane_supported(weak_route, "audience_response")
    assert grounded_planned_lane_ids([routed], ["audience_response"]) == [
        "audience_response"
    ]


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


def test_grounded_translation_propagates_to_originating_required_lane_only():
    grounded = _chunk("made-to-stick", "a")
    grounded.metadata = {
        "planned_lanes": ["translation_lexicon"],
        "planned_lane_grounding": {"translation_lexicon": 2.5},
    }
    ungrounded = _chunk("generic", "a")
    ungrounded.metadata = {
        "planned_lanes": ["translation_lexicon"],
        "planned_lane_grounding": {"translation_lexicon": 0.3},
        "document_route_lanes": {"translation_lexicon": 0.9},
    }

    diagnostics = propagate_grounded_lane_aliases(
        [grounded, ungrounded],
        {"translation_lexicon": ["sticky_message"]},
    )

    assert planned_lane_supported(grounded, "sticky_message") is True
    assert planned_lane_grounding(grounded, "sticky_message") == 2.5
    assert grounded.metadata["planned_lane_grounding_sources"] == {
        "sticky_message": ["translation_lexicon"]
    }
    assert planned_lane_supported(ungrounded, "sticky_message") is False
    assert "sticky_message" not in ungrounded.metadata["planned_lanes"]
    assert diagnostics["propagated_candidate_count"] == 1
    assert diagnostics["propagated_pairs"] == [
        {
            "source_lane_id": "translation_lexicon",
            "target_lane_id": "sticky_message",
        }
    ]


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


def test_multi_corpus_filter_preserves_best_candidate_for_downstream_floor():
    corpus_b_best = _chunk("b-best", "b", score=0.7)
    corpus_b_other = _chunk("b-other", "b", score=0.6)
    corpus_a_grounded = _chunk("a-grounded", "a", score=0.8)
    corpus_a_grounded.metadata = {"planned_lane_grounding": {"audience": 3.0}}

    result, diagnostics = filter_grounded_planned_candidates(
        [corpus_a_grounded, corpus_b_best, corpus_b_other],
        ["audience"],
        selected_corpus_ids=["a", "b"],
    )

    assert [chunk.chunk_id for chunk in result] == ["a-grounded", "b-best"]
    assert diagnostics["corpus_floor_candidates_preserved"] == ["b"]


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


def test_grounding_filter_preserves_exact_query_recall_lane():
    exact = _chunk("exact-query-hit", "a")
    exact.metadata = {"planned_lanes": ["original"]}
    required = _chunk("required", "a")
    required.metadata = {
        "planned_lanes": ["purple_ocean"],
        "planned_lane_grounding": {"purple_ocean": 3.0},
    }
    unrelated = _chunk("unrelated", "a")

    result, diagnostics = filter_grounded_planned_candidates(
        [unrelated, required, exact],
        ["purple_ocean"],
        protected_lane_ids=["original"],
    )

    assert [chunk.chunk_id for chunk in result] == ["required", "exact-query-hit"]
    assert diagnostics["protected_lane_candidates_preserved"] == 1


def test_single_side_filter_preserves_literal_vocabulary_translation():
    # NOTE: the single named side deliberately avoids the reserved synthetic
    # fallback id (query_plan.FALLBACK_PROBE_ID == "primary"), which now fails
    # open by contract (P0.4). Named single-side filtering stays supported.
    required = _chunk("required", "a")
    required.metadata = {"planned_lane_grounding": {"movement_notation": 2.0}}
    translated = _chunk("translated", "a")
    translated.metadata = {
        "planned_lanes": ["translation_facs"],
        "planned_lane_grounding": {"translation_facs": 2.5},
    }
    route_only = _chunk("route-only", "a")
    route_only.metadata = {
        "planned_lanes": ["translation_facs"],
        "planned_lane_grounding": {"translation_facs": 0.0},
        "document_route_lanes": {"translation_facs": 0.9},
    }

    result, diagnostics = filter_grounded_planned_candidates(
        [route_only, required, translated],
        ["movement_notation"],
    )

    assert [chunk.chunk_id for chunk in result] == ["required", "translated"]
    assert diagnostics["grounded_translation_candidates_preserved"] == 1


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


def test_document_lane_dedupe_keeps_same_content_in_distinct_corpora():
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

    assert result == [first, second]
    assert duplicates == 0


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
    assert reserved_required_lane_ids(
        next(chunk for chunk in result if chunk.chunk_id == "purple"),
        ["purple_ocean", "sticky_message"],
    ) == ["purple_ocean"]
    assert reserved_required_lane_ids(
        next(chunk for chunk in result if chunk.chunk_id == "sticky"),
        ["purple_ocean", "sticky_message"],
    ) == ["sticky_message"]


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


def test_finalist_reservations_keep_low_global_score_from_strong_semantic_route():
    dominant = _chunk("dominant", "a", 1.0)
    dominant.metadata = {
        "planned_lanes": ["audience"],
        "planned_lane_grounding": {"audience": 2.0},
    }
    prompt = _chunk("prompt", "a", 0.001)
    prompt.metadata = {
        "planned_lanes": ["opening_prompt"],
        "planned_lane_grounding": {"opening_prompt": 0.75},
        "document_route_lanes": {"opening_prompt": 0.49},
    }

    result, diagnostics = reserve_planned_finalists(
        [dominant, prompt],
        preferred=[dominant],
        required_lane_ids=["audience", "opening_prompt"],
        corpus_ids=["a"],
        max_candidates=2,
    )

    assert [chunk.chunk_id for chunk in result] == ["dominant", "prompt"]
    assert diagnostics["lane_reservations"]["opening_prompt"] == "prompt"


def test_required_lane_replaces_selected_route_only_tail_with_reranked_candidate():
    strong = _chunk("strong-sticky", "a", 0.95)
    strong.doc_id = "strong-doc"
    strong.metadata = {
        "planned_lanes": ["sticky_message"],
        "planned_lane_grounding": {"sticky_message": 0.4},
        "document_route_lanes": {"sticky_message": 0.5},
    }
    weak_selected = _chunk("weak-sticky", "a", 0.001)
    weak_selected.doc_id = "weak-doc"
    weak_selected.metadata = {
        "planned_lanes": ["sticky_message"],
        "planned_lane_grounding": {"sticky_message": 0.4},
        "document_route_lanes": {"sticky_message": 0.6},
    }

    result, diagnostics = reserve_planned_finalists(
        [strong, weak_selected],
        preferred=[weak_selected],
        required_lane_ids=["sticky_message"],
        corpus_ids=["a"],
        max_candidates=1,
    )

    assert [chunk.chunk_id for chunk in result] == ["strong-sticky"]
    assert diagnostics["lane_reservations"] == {"sticky_message": "strong-sticky"}


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


def test_required_lane_replaces_unprotected_same_document_winner():
    generic = _chunk("generic", "a", 1.0)
    generic.doc_id = "doc-a"
    performance = _chunk("performance", "a", 0.9)
    performance.doc_id = "doc-b"
    performance.metadata = {
        "planned_lanes": ["performance"],
        "planned_lane_grounding": {"performance": 2.0},
    }
    visual = _chunk("visual", "a", 0.4)
    visual.doc_id = "doc-a"
    visual.metadata = {
        "planned_lanes": ["visual"],
        "planned_lane_grounding": {"visual": 2.0},
        "document_route_lanes": {"visual": 0.8},
    }

    result, diagnostics = reserve_planned_finalists(
        [generic, performance, visual],
        preferred=[generic, performance],
        required_lane_ids=["visual", "performance"],
        corpus_ids=["a"],
        max_candidates=2,
        max_per_document=1,
    )

    assert [chunk.chunk_id for chunk in result] == ["performance", "visual"]
    assert diagnostics["lane_reservations"] == {
        "visual": "visual",
        "performance": "performance",
    }


def test_required_lane_can_share_document_when_no_other_grounded_source_exists():
    purple = _chunk("purple", "a", 0.99)
    purple.doc_id = "shared-doc"
    purple.metadata = {
        "planned_lanes": ["purple"],
        "planned_lane_grounding": {"purple": 2.0},
        "document_route_lanes": {"purple": 0.8},
    }
    sticky = _chunk("sticky", "a", 0.98)
    sticky.doc_id = "shared-doc"
    sticky.metadata = {
        "planned_lanes": ["sticky"],
        "planned_lane_grounding": {"sticky": 2.0},
        "document_route_lanes": {"sticky": 0.8},
    }

    result, diagnostics = reserve_planned_finalists(
        [purple, sticky],
        preferred=[purple],
        required_lane_ids=["purple", "sticky"],
        corpus_ids=["a"],
        max_candidates=2,
        max_per_document=1,
    )

    assert [chunk.chunk_id for chunk in result] == ["purple", "sticky"]
    assert diagnostics["lane_reservations"] == {
        "purple": "purple",
        "sticky": "sticky",
    }


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


def test_finalist_reservations_keep_exact_query_lane_without_counting_it_required():
    required = _chunk("required", "a", 0.9)
    required.metadata = {
        "planned_lanes": ["purple_ocean"],
        "planned_lane_grounding": {"purple_ocean": 3.0},
    }
    exact = _chunk("exact-query-hit", "a", 0.2)
    exact.metadata = {"planned_lanes": ["original"]}

    result, diagnostics = reserve_planned_finalists(
        [required, exact],
        preferred=[required],
        required_lane_ids=["purple_ocean"],
        protected_lane_ids=["original"],
        corpus_ids=["a"],
        max_candidates=2,
    )

    assert [chunk.chunk_id for chunk in result] == ["required", "exact-query-hit"]
    assert diagnostics["required_lane_ids"] == ["purple_ocean"]
    assert diagnostics["protected_lane_reservations"] == {"original": "exact-query-hit"}


def test_finalist_route_reservations_can_be_scoped_to_answer_lane():
    answer = _chunk("book", "a", 0.8)
    answer.doc_id = "book-doc"
    answer.metadata = {
        "planned_lanes": ["books"],
        "planned_lane_grounding": {"books": 2.0},
        "document_route_lanes": {"books": 0.9},
    }
    support = _chunk("support", "a", 0.9)
    support.doc_id = "support-doc"
    support.metadata = {
        "planned_lanes": ["dropshipping"],
        "planned_lane_grounding": {"dropshipping": 2.0},
        "document_route_lanes": {"dropshipping": 0.95},
    }

    result, diagnostics = reserve_planned_finalists(
        [support, answer],
        preferred=[],
        required_lane_ids=["books", "dropshipping"],
        corpus_ids=["a"],
        max_candidates=2,
        routed_document_budget=1,
        preferred_route_lane_ids=["books"],
    )

    assert {chunk.chunk_id for chunk in result} == {"book", "support"}
    assert diagnostics["preferred_route_lane_ids"] == ["books"]
    assert diagnostics["routed_documents_selected"] == ["book-doc", "support-doc"]


def test_enumeration_priority_allocates_answer_items_before_support_prose():
    books = []
    for index in range(4):
        chunk = _chunk(f"book-{index}", "a", 1.0 - index * 0.05)
        chunk.doc_id = "book-doc"
        chunk.parent_id = f"book-parent-{index // 2}"
        chunk.metadata = {
            "planned_lanes": ["books"],
            "planned_lane_grounding": {"books": 2.0},
        }
        books.append(chunk)
    support = []
    for index in range(4):
        chunk = _chunk(f"support-{index}", "a", 0.95 - index * 0.05)
        chunk.doc_id = "support-doc"
        chunk.metadata = {
            "planned_lanes": ["dropshipping"],
            "planned_lane_grounding": {"dropshipping": 2.0},
        }
        support.append(chunk)

    selected, diagnostics = prioritize_enumeration_candidates(
        [books[0], *support, *books[1:]],
        preferred=support,
        answer_lane_ids=["books"],
        required_lane_ids=["books", "dropshipping"],
        max_candidates=6,
    )

    assert sum(chunk.chunk_id.startswith("book-") for chunk in selected) == 4
    assert sum(chunk.chunk_id.startswith("support-") for chunk in selected) == 1
    assert diagnostics["answer_candidates"] == 4


def test_enumeration_dedupe_preserves_answer_siblings_and_caps_support_doc():
    answer_a = _chunk("book-a", "a")
    answer_a.doc_id = "book-doc"
    answer_a.parent_id = "book-parent"
    answer_a.metadata = {"planned_lane_grounding": {"books": 2.0}}
    answer_b = answer_a.model_copy(deep=True)
    answer_b.chunk_id = "book-b"
    support_a = _chunk("support-a", "a")
    support_a.doc_id = "support-doc"
    support_a.metadata = {"planned_lane_grounding": {"dropshipping": 2.0}}
    support_b = support_a.model_copy(deep=True)
    support_b.chunk_id = "support-b"

    selected, dropped = dedupe_enumeration_finalists(
        [answer_a, answer_b, support_a, support_b],
        answer_lane_ids=["books"],
    )

    assert [chunk.chunk_id for chunk in selected] == [
        "book-a",
        "book-b",
        "support-a",
    ]
    assert dropped == 1


def test_enumeration_output_places_answer_objects_before_support_context():
    support = _chunk("support", "a", 1.0)
    support.metadata = {"planned_lane_grounding": {"dropshipping": 2.0}}
    answer = _chunk("answer", "a", 0.8)
    answer.metadata = {"planned_lane_grounding": {"books": 2.0}}

    ordered = order_enumeration_finalists(
        [support, answer],
        answer_lane_ids=["books"],
    )

    assert [chunk.chunk_id for chunk in ordered] == ["answer", "support"]


def test_parent_finalist_dedupe_keeps_same_parent_id_in_distinct_corpora():
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

    assert dropped == 0
    assert result == [first, second]


def test_parent_dedupe_preserves_route_backed_required_reservation():
    original = _chunk("original", "a")
    original.parent_id = "shared-parent"
    original.metadata = {"planned_lanes": ["original"]}
    required = _chunk("audience", "a")
    required.parent_id = "shared-parent"
    required.metadata = {
        "planned_lanes": ["target_audience"],
        "planned_lane_grounding": {"target_audience": 0.6667},
        "document_route_lanes": {"target_audience": 0.5971},
        "planned_required_lane_reservations": ["target_audience"],
    }

    result, dropped = dedupe_parent_finalists([original, required])

    assert dropped == 1
    assert result[0].metadata["document_route_lanes"] == {"target_audience": 0.5971}
    assert reserved_required_lane_ids(result[0], ["target_audience"]) == [
        "target_audience"
    ]
