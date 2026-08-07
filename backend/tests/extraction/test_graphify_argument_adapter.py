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
