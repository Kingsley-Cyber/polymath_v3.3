from models.schemas import SourceChunk
from services.retriever.planned_fusion import (
    PlannedPool,
    dedupe_parent_finalists,
    fuse_planned_pools,
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
        PlannedPool("sticky_message", "lexical", (_chunk("sticky", "b"),), required=True),
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


def test_parent_finalist_dedupe_merges_lane_provenance():
    first = _chunk("first", "a")
    first.parent_id = "shared-parent"
    first.metadata = {"planned_lanes": ["purple_ocean"]}
    second = _chunk("second", "b")
    second.parent_id = "shared-parent"
    second.metadata = {
        "planned_lanes": ["sticky_message"],
        "corpus_memberships": ["b"],
    }

    result, dropped = dedupe_parent_finalists([first, second])

    assert dropped == 1
    assert len(result) == 1
    assert result[0].metadata["planned_lanes"] == ["purple_ocean", "sticky_message"]
    assert result[0].metadata["corpus_memberships"] == ["a", "b"]
    assert any(
        item.get("retriever") == "parent_finalist_dedupe"
        for item in result[0].provenance
    )
