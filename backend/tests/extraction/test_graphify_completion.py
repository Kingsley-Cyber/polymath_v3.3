from __future__ import annotations

from models.graphify_contracts import DocumentEntityV1, EntityTerminalState
from services.extraction.graphify_completion import complete_document_mentions
from services.extraction.graphify_normalization import normalize_document


def entity(document_id: str, name: str, aliases=(), state=EntityTerminalState.PROMOTED, confidence=0.95):
    return DocumentEntityV1(
        entity_id=f"entity:{document_id}:{name}", document_id=document_id,
        canonical_name=name, entity_type="software", aliases=tuple(aliases),
        mention_ids=(), state=state, confidence=confidence,
        reasons=("test",), reducer_release="test-reducer",
    )


def test_completion_recovers_every_exact_occurrence_with_offsets() -> None:
    text = "MongoDB stores data. MongoDB supports queries."
    document = normalize_document("doc", text)
    output = complete_document_mentions(document, [entity("doc", "MongoDB")])
    assert [item.surface for item in output.mentions] == ["MongoDB", "MongoDB"]
    assert [item.normalized_start for item in output.mentions] == [0, 21]
    assert all(text[item.original_start:item.original_end] == item.surface for item in output.mentions)


def test_alias_and_acronym_patterns_are_distinct_mentions() -> None:
    text = "Retrieval-Augmented Generation (RAG) uses RAG."
    document = normalize_document("doc", text)
    output = complete_document_mentions(
        document, [entity("doc", "Retrieval-Augmented Generation", aliases=("RAG",))],
    )
    assert [item.surface for item in output.mentions] == [
        "Retrieval-Augmented Generation", "RAG", "RAG",
    ]
    assert [item.source for item in output.mentions] == ["exact_name", "acronym", "acronym"]


def test_ambiguous_names_require_named_context_and_exact_case() -> None:
    text = (
        "Go is a named programming language. Operators go home. "
        "Apple owns Orion Lab. The apple stained a table. "
        "Python implements a parser. A python crossed the road. "
        "Rust supports a service. The rust caused damage. "
        "Oracle supports billing. The oracle predicted rain."
    )
    document = normalize_document("doc", text)
    entities = [entity("doc", name) for name in ("Go", "Apple", "Python", "Rust", "Oracle")]
    output = complete_document_mentions(document, entities)
    assert [item.surface for item in output.mentions] == ["Go", "Apple", "Python", "Rust", "Oracle"]
    assert all(item.context_rule == "ambiguous_named_context" for item in output.mentions)


def test_longest_pattern_wins_over_nested_candidate() -> None:
    document = normalize_document("doc", "Atlas Dataset is durable.")
    output = complete_document_mentions(
        document, [entity("doc", "Atlas"), entity("doc", "Atlas Dataset")],
    )
    assert [item.surface for item in output.mentions] == ["Atlas Dataset"]
    assert output.report["overlap_rejections"] == 1


def test_repeatability_and_review_exclusion() -> None:
    document = normalize_document("doc", "MongoDB and Qdrant are systems.")
    entities = [entity("doc", "MongoDB"), entity("doc", "Qdrant", state=EntityTerminalState.REVIEW)]
    first = complete_document_mentions(document, entities)
    second = complete_document_mentions(document, entities)
    assert first == second
    assert [item.surface for item in first.mentions] == ["MongoDB"]
    assert first.report["deterministic_ids"] is True


def test_book_scale_document_completes_mentions() -> None:
    # O5 soak finding: spaCy E088 rejected full-document tokenization past
    # 1M chars, terminally failing every big book. The completion pipeline
    # is tokenizer-only, so the guard must scale with the document.
    filler = ("The archive committee reviewed the quarterly registry notes. " * 200 + "\n")
    text = ("Graphify uses MongoDB. " + filler * 90)[:1_100_000] + " Graphify uses MongoDB."
    assert len(text) > 1_000_000
    document = normalize_document("doc-book", text)
    output = complete_document_mentions(document, [entity("doc-book", "Graphify")])
    assert any(m.entity_id == "entity:doc-book:Graphify" for m in output.mentions)
