"""#2 structural endpoint eligibility — POS-class veto, no word lists.

The polysemy pair is the contract: the same surface "can" is vetoed as an
endpoint when its syntactic head is AUX and eligible when it is a NOUN.
"""
from models.graphify_contracts import (
    CompletedMentionV1,
    DocumentEntityV1,
    EntityTerminalState,
)
from services.extraction.graphify_normalization import normalize_document
from services.extraction.graphify_relations import run_relation_fast_path
from services.extraction.graphify_survey import survey_document


def _entity(entity_id, name, mention_ids, entity_type="concept"):
    return DocumentEntityV1(
        entity_id=entity_id, document_id="doc", canonical_name=name,
        entity_type=entity_type, mention_ids=tuple(mention_ids),
        state=EntityTerminalState.PROMOTED, confidence=1.0, reasons=("test",),
        reducer_release="test",
    )


def _mention(mention_id, entity_id, surface, start):
    return CompletedMentionV1(
        mention_id=mention_id, entity_id=entity_id, document_id="doc",
        surface=surface, normalized_start=start, normalized_end=start + len(surface),
        original_start=start, original_end=start + len(surface),
        source="raw", completion_release="test",
    )


def _run(text, spec):
    document = normalize_document("doc", text)
    survey = survey_document(document)
    mentions, entities = [], []
    for index, (surface, entity_type) in enumerate(spec):
        start = text.find(surface)
        assert start >= 0, surface
        entity_id = f"entity:{index}"
        mention_id = f"mention:{index}"
        entities.append(_entity(entity_id, surface, [mention_id], entity_type))
        mentions.append(_mention(mention_id, entity_id, surface, start))
    return run_relation_fast_path([document], [survey], mentions, entities)


def test_aux_headed_span_is_flagged_and_vetoed() -> None:
    output = _run(
        "Deployment can cause outages downstream.",
        (("Deployment", "method"), ("can", "concept"), ("outages", "event")),
    )
    flagged = set(output.report["closed_class_mention_ids"])
    assert "mention:1" in flagged            # "can" (AUX head)
    assert "mention:0" not in flagged        # "Deployment" (NOUN)
    accepted = [
        r for r in output.mapped_relations if r.terminal_state.value == "accepted"
    ]
    assert not any(
        r.subject_mention_id == "mention:1" or r.object_mention_id == "mention:1"
        for r in accepted
    )


def test_noun_headed_can_stays_eligible() -> None:
    # Same surface, unambiguous NOUN context: never flagged. (In genuinely
    # parser-ambiguous contexts like "The can stores paint", the veto follows
    # the parse — abstention-safe REVIEW, observation preserved.)
    output = _run(
        "A can of paint fell from the shelf.",
        (("can", "artifact"), ("paint", "concept")),
    )
    assert "mention:0" not in set(output.report["closed_class_mention_ids"])


def test_self_referential_syntax_endpoints_reject() -> None:
    # Both mentions resolve to one entity: an edge to itself asserts nothing.
    text = "The Ledger Store stores the Ledger Store snapshot."
    document = normalize_document("doc", text)
    survey = survey_document(document)
    first = text.find("Ledger Store")
    second = text.find("Ledger Store", first + 1)
    entities = [_entity("entity:ls", "Ledger Store", ["mention:a", "mention:b"], "software")]
    mentions = [
        _mention("mention:a", "entity:ls", "Ledger Store", first),
        _mention("mention:b", "entity:ls", "Ledger Store", second),
    ]
    output = run_relation_fast_path([document], [survey], mentions, entities)
    self_ref = [
        r for r in output.mapped_relations
        if r.mapping_rule == "reject:self_referential_endpoints"
    ]
    accepted_self = [
        r for r in output.mapped_relations
        if r.terminal_state.value == "accepted"
        and r.subject_mention_id in {"mention:a", "mention:b"}
        and r.object_mention_id in {"mention:a", "mention:b"}
    ]
    assert not accepted_self
    if self_ref:
        assert all(r.terminal_state.value == "rejected" for r in self_ref)
