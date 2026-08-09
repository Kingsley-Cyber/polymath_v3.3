from models.graphify_contracts import (
    CompletedMentionV1,
    DocumentEntityV1,
    EntityTerminalState,
    OpenIEArgumentKind,
    OpenIERawPropositionV1,
)
from services.extraction.graphify_argument_adapter import adapt_openie_arguments


def _entity() -> DocumentEntityV1:
    return DocumentEntityV1(
        entity_id="entity:harbor", document_id="doc", canonical_name="Harbor",
        entity_type="software", mention_ids=("mention:harbor",),
        state=EntityTerminalState.PROMOTED, confidence=1.0, reasons=("test",),
        reducer_release="test",
    )


def _mention() -> CompletedMentionV1:
    return CompletedMentionV1(
        mention_id="mention:harbor", entity_id="entity:harbor", document_id="doc",
        surface="Harbor", normalized_start=0, normalized_end=6,
        original_start=0, original_end=6, source="raw", completion_release="test",
    )


def _proposition(subject="Harbor", obj="12 weeks") -> OpenIERawPropositionV1:
    text = "Harbor lasted 12 weeks."
    return OpenIERawPropositionV1(
        proposition_id="prop:1", document_id="doc", unit_id="unit:1",
        evidence_text=text, evidence_start=0, evidence_end=len(text),
        rendering_sequence=0, subject=subject, relation="lasted", object=obj,
        confidence=1.0, entailment_score=1.0, extractor_release="test",
    )


def test_adapter_classifies_entity_and_literal_with_exact_offsets() -> None:
    output = adapt_openie_arguments([_proposition()], [_mention()], [_entity()])
    by_role = {item.role: item for item in output.arguments}
    assert by_role["subject"].kind == OpenIEArgumentKind.ENTITY
    assert (by_role["subject"].normalized_start, by_role["subject"].normalized_end) == (0, 6)
    assert by_role["object"].kind == OpenIEArgumentKind.LITERAL
    assert by_role["object"].literal_type == "temporal_or_metric"
    assert output.report["classification_conservation"] is True
    assert output.report["strict_entity_alignment_rate"] == 1.0


def test_pronoun_never_becomes_entity() -> None:
    output = adapt_openie_arguments([_proposition(subject="it")], [_mention()], [_entity()])
    subject = next(item for item in output.arguments if item.role == "subject")
    assert subject.kind == OpenIEArgumentKind.UNRESOLVED
    assert output.report["accepted_pronoun_entities"] == 0


def _make_entity(entity_id, name, mention_ids, entity_type="software"):
    return DocumentEntityV1(
        entity_id=entity_id, document_id="doc", canonical_name=name,
        entity_type=entity_type, mention_ids=tuple(mention_ids),
        state=EntityTerminalState.PROMOTED, confidence=1.0, reasons=("test",),
        reducer_release="test",
    )


def _make_mention(mention_id, entity_id, surface, start):
    return CompletedMentionV1(
        mention_id=mention_id, entity_id=entity_id, document_id="doc",
        surface=surface, normalized_start=start, normalized_end=start + len(surface),
        original_start=start, original_end=start + len(surface),
        source="raw", completion_release="test",
    )


def _make_proposition(text, subject, obj, relation="uses"):
    return OpenIERawPropositionV1(
        proposition_id="prop:ladder", document_id="doc", unit_id="unit:1",
        evidence_text=text, evidence_start=0, evidence_end=len(text),
        rendering_sequence=0, subject=subject, relation=relation, object=obj,
        confidence=1.0, entailment_score=1.0, extractor_release="test",
    )


def _subject_kind(text, subject, mentions, entities, obj="Qdrant"):
    output = adapt_openie_arguments(
        [_make_proposition(text, subject, obj)], mentions, entities,
    )
    return next(item for item in output.arguments if item.role == "subject")


def _worker_fixture():
    text = "The Ingestion Worker, the processing service, uses Qdrant heavily."
    entities = [
        _make_entity("entity:iw", "Ingestion Worker", ["mention:iw"]),
        _make_entity("entity:qdrant", "Qdrant", ["mention:qdrant"]),
    ]
    mentions = [
        _make_mention("mention:iw", "entity:iw", "Ingestion Worker", 4),
        _make_mention("mention:qdrant", "entity:qdrant", "Qdrant", text.find("Qdrant")),
    ]
    return text, mentions, entities


def test_ladder_positive_variants_align_to_one_entity() -> None:
    text, mentions, entities = _worker_fixture()
    for subject in (
        "Ingestion Worker",
        "The Ingestion Worker",
        "an Ingestion Worker",
        "Ingestion Worker, the processing service",
    ):
        arg = _subject_kind(text, subject, mentions, entities)
        assert arg.kind == OpenIEArgumentKind.ENTITY, subject
        assert arg.entity_id == "entity:iw", subject
        assert arg.surface == subject  # OpenIE's observation is never rewritten
        assert arg.normalized_value == "Ingestion Worker"


def test_ladder_rejects_generic_and_wrong_number_variants() -> None:
    text, mentions, entities = _worker_fixture()
    for subject in ("the worker", "the service", "another worker", "all workers"):
        arg = _subject_kind(text, subject, mentions, entities)
        assert arg.kind != OpenIEArgumentKind.ENTITY, subject


def test_ladder_appositive_containment_aligns_unique_entity() -> None:
    text = "Argus, a verifier service, consumes the Evidence Packet."
    entities = [
        _make_entity("entity:argus", "Argus", ["mention:argus"]),
        _make_entity("entity:ep", "Evidence Packet", ["mention:ep"], "artifact"),
    ]
    mentions = [
        _make_mention("mention:argus", "entity:argus", "Argus", 0),
        _make_mention("mention:ep", "entity:ep", "Evidence Packet", text.find("Evidence Packet")),
    ]
    arg = _subject_kind(text, "Argus, a verifier service", mentions, entities, obj="Evidence Packet")
    assert arg.kind == OpenIEArgumentKind.ENTITY
    assert arg.entity_id == "entity:argus"


def test_ladder_surname_variant_aligns_when_unique_and_fails_when_ambiguous() -> None:
    text = "Mira Solano defines Citation Recall for the benchmark."
    entities = [
        _make_entity("entity:mira", "Mira Solano", ["mention:mira"], "person"),
        _make_entity("entity:cr", "Citation Recall", ["mention:cr"], "concept"),
    ]
    mentions = [
        _make_mention("mention:mira", "entity:mira", "Mira Solano", 0),
        _make_mention("mention:cr", "entity:cr", "Citation Recall", text.find("Citation Recall")),
    ]
    arg = _subject_kind(text, "Solano", mentions, entities, obj="Citation Recall")
    assert arg.kind == OpenIEArgumentKind.ENTITY
    assert arg.entity_id == "entity:mira"
    assert arg.reasons == ("explicit_document_variant_short_form",)
    # A second Mira makes the short form ambiguous — must stay unresolved.
    entities.append(_make_entity("entity:mira2", "Mira Chen", ["mention:mira2"], "person"))
    mentions.append(_make_mention("mention:mira2", "entity:mira2", "Mira Chen", 60))
    arg = _subject_kind(text, "Mira", mentions, entities, obj="Citation Recall")
    assert arg.kind != OpenIEArgumentKind.ENTITY


def test_ladder_never_aligns_multi_entity_or_clausal_arguments() -> None:
    text = "Orion compares Helix-2 with Atlas-7B under identical conditions."
    entities = [
        _make_entity("entity:h2", "Helix-2", ["mention:h2"]),
        _make_entity("entity:a7", "Atlas-7B", ["mention:a7"]),
    ]
    mentions = [
        _make_mention("mention:h2", "entity:h2", "Helix-2", text.find("Helix-2")),
        _make_mention("mention:a7", "entity:a7", "Atlas-7B", text.find("Atlas-7B")),
    ]
    two = _subject_kind(text, "Helix-2 with Atlas-7B", mentions, entities)
    assert two.kind != OpenIEArgumentKind.ENTITY
    clause = _subject_kind(text, "whose records reference Helix-2", mentions, entities)
    assert clause.kind != OpenIEArgumentKind.ENTITY
