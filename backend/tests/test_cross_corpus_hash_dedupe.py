from models.schemas import SourceChunk
from services.retriever.hydrate import dedupe_cross_corpus_evidence


def _chunk(*, chunk_id: str, corpus_id: str, doc_id: str, text: str, score: float):
    return SourceChunk(
        chunk_id=chunk_id,
        parent_id=f"parent-{chunk_id}",
        corpus_id=corpus_id,
        doc_id=doc_id,
        text=text,
        score=score,
        source_tier="vector",
        metadata={
            "source_file_hash": "same-book-hash",
            "corpus_memberships": [corpus_id],
        },
        provenance=[{"retriever": "test", "corpus_id": corpus_id}],
    )


def test_duplicate_book_passages_collapse_and_preserve_memberships():
    first = _chunk(
        chunk_id="a",
        corpus_id="commerce",
        doc_id="book-a",
        text="A sticky message is concrete and unexpected.",
        score=0.7,
    )
    duplicate = _chunk(
        chunk_id="b",
        corpus_id="transcripts",
        doc_id="book-b",
        text="  A sticky message is concrete and unexpected.  ",
        score=0.9,
    )
    distinct = _chunk(
        chunk_id="c",
        corpus_id="transcripts",
        doc_id="book-b",
        text="A different passage from the same book remains useful.",
        score=0.6,
    )

    result, dropped = dedupe_cross_corpus_evidence([first, duplicate, distinct])

    assert dropped == 1
    assert len(result) == 2
    assert result[0].score == 0.9
    assert result[0].metadata["corpus_memberships"] == ["commerce", "transcripts"]
    assert any(
        item.get("retriever") == "cross_corpus_hash_dedupe"
        for item in result[0].provenance
    )
