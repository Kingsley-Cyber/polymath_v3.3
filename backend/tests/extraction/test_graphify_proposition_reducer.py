from models.graphify_contracts import (
    AdaptedOpenIEArgumentV1,
    OpenIEArgumentKind,
    OpenIERawPropositionV1,
)
from services.extraction.graphify_proposition_reducer import reduce_openie_propositions


def _prop(identifier: str, relation: str, *, chain=()) -> OpenIERawPropositionV1:
    text = "Harbor depends on SQLite."
    return OpenIERawPropositionV1(
        proposition_id=identifier, document_id="doc", unit_id="unit:1",
        evidence_text=text, evidence_start=0, evidence_end=len(text),
        rendering_sequence=int(identifier[-1]), subject="Harbor", relation=relation,
        object="SQLite", confidence=1.0, entailment_score=1.0,
        asserter_chain=chain, extractor_release="test",
    )


def _args(identifier: str):
    return [
        AdaptedOpenIEArgumentV1(
            argument_id=f"arg:{identifier}:s", proposition_id=identifier,
            document_id="doc", unit_id="unit:1", role="subject", surface="Harbor",
            kind=OpenIEArgumentKind.DESCRIPTION, normalized_value="harbor",
            reasons=("test",), adapter_release="test",
        ),
        AdaptedOpenIEArgumentV1(
            argument_id=f"arg:{identifier}:o", proposition_id=identifier,
            document_id="doc", unit_id="unit:1", role="object", surface="SQLite",
            kind=OpenIEArgumentKind.DESCRIPTION, normalized_value="sqlite",
            reasons=("test",), adapter_release="test",
        ),
    ]


def test_equivalent_renderings_collapse_and_conserve_ids() -> None:
    props = [_prop("prop:0", "depends on"), _prop("prop:1", "depended on")]
    output = reduce_openie_propositions(props, [*_args("prop:0"), *_args("prop:1")])
    assert len(output.families) == 1
    assert set(output.families[0].rendering_ids) == {"prop:0", "prop:1"}
    assert output.report["conservation"] is True
    assert output.report["collapsed_renderings"] == 1


def test_attributed_rendering_does_not_merge_with_direct_fact() -> None:
    props = [_prop("prop:0", "depends on"), _prop("prop:1", "depends on", chain=("Maya",))]
    output = reduce_openie_propositions(props, [*_args("prop:0"), *_args("prop:1")])
    assert len(output.families) == 2
    assert {item.attribution for item in output.families} == {"direct", "attributed:Maya"}


def test_incorrect_nominal_claim_qualifies_openie_family() -> None:
    proposition = _prop("prop:0", "depends on").model_copy(update={
        "evidence_text": "The incorrect claim that Harbor depends on SQLite passed review.",
        "evidence_end": len("The incorrect claim that Harbor depends on SQLite passed review."),
    })
    output = reduce_openie_propositions([proposition], _args("prop:0"))
    family = output.families[0]
    assert family.polarity == "denied"
    assert family.attribution == "nominal:claim"


def test_nominal_cue_does_not_contaminate_openie_direct_fact() -> None:
    proposition = _prop("prop:0", "depends on").model_copy(update={
        "evidence_text": "The claim was archived. Harbor depends on SQLite.",
        "evidence_end": len("The claim was archived. Harbor depends on SQLite."),
    })
    output = reduce_openie_propositions([proposition], _args("prop:0"))
    family = output.families[0]
    assert family.polarity == "positive"
    assert family.attribution == "direct"
