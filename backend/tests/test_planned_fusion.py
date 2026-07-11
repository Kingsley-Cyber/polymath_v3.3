from models.schemas import SourceChunk
from services.retriever.planned_fusion import (
    PlannedPool,
    annotate_planned_lane_grounding,
    dedupe_document_lane_finalists,
    dedupe_parent_finalists,
    filter_grounded_planned_candidates,
    fuse_planned_pools,
    grounded_planned_lane_ids,
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
    purple.metadata = {"planned_lanes": ["purple_ocean"]}
    sticky = _chunk("sticky", "b", 0.1)
    sticky.metadata = {"planned_lanes": ["sticky_message"]}
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
