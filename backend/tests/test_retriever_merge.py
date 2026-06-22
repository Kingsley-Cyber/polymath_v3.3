from models.schemas import SourceChunk
from services.retriever.merge import merge_pools


def _chunk(
    chunk_id: str,
    *,
    parent_id: str = "p1",
    score: float = 0.5,
    text: str = "text",
    source_tier: str = "chunk",
    provenance: list[dict] | None = None,
) -> SourceChunk:
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=parent_id,
        doc_id="d1",
        corpus_id="c1",
        text=text,
        score=score,
        source_tier=source_tier,
        provenance=provenance,
    )


def test_merge_pools_preserves_exact_child_identity_over_summary():
    summary = _chunk(
        "p1_summary",
        score=0.95,
        text="Parent summary about NLP.",
        source_tier="summary",
        provenance=[{"retriever": "qdrant_summary"}],
    )
    child = _chunk(
        "child_exact",
        score=0.60,
        text="NLP uses data augmentation to create training examples.",
        source_tier="chunk",
        provenance=[{"retriever": "mongo_lexical"}],
    )

    merged = merge_pools([summary], [child])

    assert len(merged) == 1
    candidate = merged[0]
    assert candidate.chunk_id == "child_exact"
    assert candidate.text.startswith("NLP uses data augmentation")
    assert candidate.score == 0.95
    assert candidate.source_tier == "summary+chunk"
    retrievers = {item["retriever"] for item in candidate.provenance or []}
    assert retrievers == {"qdrant_summary", "mongo_lexical"}
    reps = candidate.metadata["merged_parent_representatives"]
    assert {rep["chunk_id"] for rep in reps} == {"p1_summary", "child_exact"}


def test_merge_pools_keeps_best_child_among_same_parent_children():
    weak = _chunk("child_weak", score=0.20, text="weak")
    strong = _chunk("child_strong", score=0.80, text="strong")

    merged = merge_pools([weak], [strong])

    assert len(merged) == 1
    assert merged[0].chunk_id == "child_strong"
    assert merged[0].score == 0.80
