from models.graphify_contracts import (
    AdaptedOpenIEArgumentV1, EntityTerminalState, OpenIEArgumentKind,
    OpenIEPropositionFamilyV1, OpenIERawPropositionV1,
)
from services.extraction.graphify_predicate_compiler import compile_openie_predicates


def _case(surface: str, lemma: str, *, qualified: bool = False):
    raw = OpenIERawPropositionV1(
        proposition_id="p1", document_id="d1", unit_id="u1",
        evidence_text=f"Alpha {surface} Beta.", evidence_start=0,
        evidence_end=len(f"Alpha {surface} Beta."), rendering_sequence=0,
        subject="Alpha", relation=surface, object="Beta", confidence=0.9,
        entailment_score=1.0,
        extractor_release="test",
    )
    args = []
    for role, surface_value, entity_id, start in (
        ("subject", "Alpha", "e1", 0), ("object", "Beta", "e2", len(raw.evidence_text) - 5),
    ):
        args.append(AdaptedOpenIEArgumentV1(
            argument_id="a-" + role, proposition_id="p1", document_id="d1",
            unit_id="u1", role=role, surface=surface_value,
            kind=OpenIEArgumentKind.ENTITY, normalized_start=start,
            normalized_end=start + len(surface_value), mention_id="m-" + role,
            entity_id=entity_id, entity_type="software",
            entity_state=EntityTerminalState.PROMOTED,
            normalized_value=surface_value, reasons=("test",), adapter_release="test",
        ))
    family = OpenIEPropositionFamilyV1(
        family_id="f1", document_id="d1", unit_id="u1",
        subject_key="entity:e1", subject_kind=OpenIEArgumentKind.ENTITY,
        relation_lemma=lemma, object_key="entity:e2", object_kind=OpenIEArgumentKind.ENTITY,
        polarity="negative" if qualified else "positive", modality="asserted",
        attribution="direct", representative_proposition_id="p1",
        rendering_ids=("p1",), surface_relations=(surface,), evidence_start=0,
        evidence_end=len(raw.evidence_text), max_confidence=0.9, family_release="test",
    )
    return compile_openie_predicates([family], [raw], args).candidates[0]


def test_maps_bounded_predicate_without_forced_fallback():
    row = _case("depends on", "depend on")
    assert row.canonical_predicate == "depends_on"
    assert row.mapping_status == "MAPPED"


def test_unmapped_surface_is_preserved_and_never_related_to():
    row = _case("powers", "power")
    assert row.mapping_status == "STORE_UNMAPPED_SURFACE_RELATION"
    assert row.canonical_predicate is None


def test_passive_acquisition_swaps_direction():
    row = _case("was acquired by", "acquire by")
    assert row.canonical_predicate == "owns"
    assert row.subject_key == "entity:e2"
    assert row.object_key == "entity:e1"
    assert row.direction_rule == "passive_by_swap_to_semantic_agent"


def test_qualification_metadata_is_retained():
    row = _case("uses", "use", qualified=True)
    assert row.polarity == "negative"


def test_declared_occurred_in_normalizes_to_related_to():
    row = _case("occurred in", "occur")
    assert row.canonical_predicate == "related_to"
    assert row.mapping_status == "MAPPED"
    assert "declared_closed_ontology" in row.mapping_rule
