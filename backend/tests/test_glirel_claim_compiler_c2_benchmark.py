from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_glirel_claim_compiler_c2 import (  # noqa: E402
    FROZEN_SPEC_SHA256,
    bind_relation_candidates,
    canonical_line_sha256,
    evaluate_gate,
    load_json,
    select_gliner_mentions,
    sha256_path,
)
from models.local_extraction import (  # noqa: E402
    EntityMention,
    LocalExtractionV1,
    PredicateMention,
)
from models.semantic_artifacts import (  # noqa: E402
    ObservationBundle,
    PredicateObservation,
    SpanObservation,
    make_evidence_ref,
)
from services.ingestion.claim_compiler import compile_claim_records_v1  # noqa: E402

SPEC_PATH = ROOT / "backend/evals/glirel_claim_compiler_c2_gate_v1.json"


def test_frozen_gate_refuses_legacy_relation_crosswalk() -> None:
    spec = load_json(SPEC_PATH)
    assert sha256_path(SPEC_PATH) == FROZEN_SPEC_SHA256
    assert spec["candidate_binding"]["relation_labels"].startswith(
        "exact owner PredicateType"
    )
    assert "relation_type_crosswalk" not in spec
    assert "related_to" not in spec["model_contract"]["glirel"]["labels"]
    assert spec["arms"]["decisive"]["decision_weight"] is True
    assert spec["arms"]["diagnostic"]["decision_weight"] is False
    assert canonical_line_sha256(["CAUSES", "SIGNALS"]) == (
        "1fbb647c50e1138545cbc2ae9836625221d2a3f45267f45edcdd65007621a1e3"
    )


def test_gliner_selection_is_controlled_confidence_first_and_nonoverlapping() -> None:
    text = "Prices signal quality."
    mentions, counts = select_gliner_mentions(
        sample_id="selection",
        text=text,
        raw_entities=[
            {
                "start": 0,
                "end": 6,
                "text": "Prices",
                "label": "CONCEPT",
                "score": 0.90,
            },
            {
                "start": 0,
                "end": 6,
                "text": "Prices",
                "label": "SIGNAL",
                "score": 0.80,
            },
            {
                "start": 0,
                "end": 13,
                "text": "Prices signal",
                "label": "PROCESS",
                "score": 0.50,
            },
            {
                "start": 14,
                "end": 21,
                "text": "quality",
                "label": "QUALITY",
                "score": 0.85,
            },
            {
                "start": 14,
                "end": 21,
                "text": "quality",
                "label": "legacy-open-label",
                "score": 0.99,
            },
        ],
        controlled_types=["CONCEPT", "SIGNAL", "PROCESS", "QUALITY"],
    )
    assert [(item.text, item.entity_type) for item in mentions] == [
        ("Prices", "CONCEPT"),
        ("quality", "QUALITY"),
    ]
    assert counts == {
        "raw": 5,
        "same_span_dropped": 1,
        "label_violations": 1,
        "overlap_dropped": 1,
        "selected": 2,
    }


def _signal_fixture() -> tuple[ObservationBundle, LocalExtractionV1]:
    text = "Prices signal quality."
    evidence = make_evidence_ref(
        text=text,
        start=0,
        end=len(text),
        source_version_id="source-version:c2-test",
        hierarchy_node_id="child:c2-test",
    )
    spans = [
        SpanObservation(
            observation_id="span:subject",
            kind="subject",
            label="noun",
            text="Prices",
            start=0,
            end=6,
            producer="spacy:test",
        ),
        SpanObservation(
            observation_id="span:predicate",
            kind="predicate",
            label="signal",
            text="signal",
            start=7,
            end=13,
            producer="spacy:test",
        ),
        SpanObservation(
            observation_id="span:object",
            kind="object",
            label="noun",
            text="quality",
            start=14,
            end=21,
            producer="spacy:test",
        ),
    ]
    bundle = ObservationBundle(
        bundle_id="observation-bundle:c2-test",
        source_version_id="source-version:c2-test",
        hierarchy_node_id="child:c2-test",
        text_hash="sha256:test",
        text_length=len(text),
        producer="spacy:test",
        producer_version="test.v1",
        recipe_hash="sha256:recipe",
        spans=spans,
        predicates=[
            PredicateObservation(
                observation_id="predicate-observation:signal",
                predicate_span_id="span:predicate",
                predicate_lemma="signal",
                subject_span_ids=["span:subject"],
                object_span_ids=["span:object"],
                evidence_ref_id=evidence.evidence_ref_id,
                producer="spacy:test",
            )
        ],
        qualifiers=[],
        evidence_refs=[evidence],
    )
    source = EntityMention(
        mention_id="mention:prices",
        text="Prices",
        entity_type="CONCEPT",
        start_char=0,
        end_char=6,
        canonical_label="prices",
        confidence=0.9,
    )
    target = EntityMention(
        mention_id="mention:quality",
        text="quality",
        entity_type="QUALITY",
        start_char=14,
        end_char=21,
        canonical_label="quality",
        confidence=0.9,
    )
    extraction = LocalExtractionV1(
        schema_version="local_extraction.v1",
        document_id="doc:c2-test",
        child_id="child:c2-test",
        sentence_ids=[evidence.evidence_ref_id],
        entities=[source, target],
        predicates=[
            PredicateMention(
                predicate_id="predicate:signal",
                surface_text="signal",
                lemma="signal",
                normalized_predicate="SIGNALS",
                start_char=7,
                end_char=13,
                negated=False,
                modality="asserted",
                confidence=1.0,
            )
        ],
        relations=[],
        unresolved_spans=[],
    )
    return bundle, extraction


def test_binding_refuses_open_label_and_leaves_direction_to_compiler() -> None:
    bundle, extraction = _signal_fixture()
    valid_edges = [
        {
            "sub": "prices",
            "pred": "SIGNALS",
            "obj": "quality",
            "score": 0.9,
            "ev": "Prices signal quality.",
        }
    ]
    candidates, counts = bind_relation_candidates(
        sample_id="signal",
        edges=valid_edges,
        mentions=extraction.entities,
        extraction=extraction,
        bundle=bundle,
        controlled_predicates={"SIGNALS"},
    )
    assert len(candidates) == 1
    assert counts == {"raw_proposals": 1, "bound_candidates": 1}
    with_relation = LocalExtractionV1.model_validate(
        {**extraction.model_dump(), "relations": [candidates[0].model_dump()]}
    )
    compiled = compile_claim_records_v1(bundle=bundle, extraction=with_relation)
    assert compiled.claims[0].source_relation_ids == [candidates[0].relation_id]

    reversed_edge = [{**valid_edges[0], "sub": "quality", "obj": "prices"}]
    reversed_candidates, _ = bind_relation_candidates(
        sample_id="signal-reversed",
        edges=reversed_edge,
        mentions=extraction.entities,
        extraction=extraction,
        bundle=bundle,
        controlled_predicates={"SIGNALS"},
    )
    reversed_extraction = LocalExtractionV1.model_validate(
        {
            **extraction.model_dump(),
            "relations": [reversed_candidates[0].model_dump()],
        }
    )
    rejected = compile_claim_records_v1(bundle=bundle, extraction=reversed_extraction)
    assert rejected.claims[0].source_relation_ids == []
    assert rejected.rejected_relation_ids == [reversed_candidates[0].relation_id]

    open_label_candidates, open_counts = bind_relation_candidates(
        sample_id="signal-open",
        edges=[{**valid_edges[0], "pred": "represents"}],
        mentions=extraction.entities,
        extraction=extraction,
        bundle=bundle,
        controlled_predicates={"SIGNALS"},
    )
    assert open_label_candidates == []
    assert open_counts == {
        "raw_proposals": 1,
        "controlled_label_violations": 1,
    }


def _passing_decisive() -> dict:
    return {
        "without": {
            "accepted_support": {
                "f1": 0.0,
                "precision": 0.0,
                "decision_base_typed_compiled_gold_claims": 6,
                "decision_base_distinct_samples": 6,
                "decision_base_distinct_predicate_types": 4,
            }
        },
        "with": {
            "accepted_support": {
                "f1": 0.5,
                "precision": 1.0,
                "decision_base_typed_compiled_gold_claims": 6,
                "decision_base_distinct_samples": 6,
                "decision_base_distinct_predicate_types": 4,
            }
        },
        "invariants": {
            "core_claim_material_equal": True,
            "core_quality_equal": True,
            "accepted_label_predicate_conflicts": 0,
            "evidence_round_trip_errors": 0,
            "claim_conservation_errors": 0,
            "relation_reference_errors": 0,
            "controlled_label_violations": 0,
        },
    }


def test_verdict_precedence_defaults_to_insufficient_then_without() -> None:
    spec = load_json(SPEC_PATH)
    decisive = _passing_decisive()
    assert evaluate_gate(spec=spec, decisive=decisive)["verdict"] == "with_wins"

    thin = deepcopy(decisive)
    thin["with"]["accepted_support"]["decision_base_typed_compiled_gold_claims"] = 4
    assert evaluate_gate(spec=spec, decisive=thin)["verdict"] == (
        "insufficient_evidence"
    )

    no_improvement = deepcopy(decisive)
    no_improvement["with"]["accepted_support"]["f1"] = 0.0
    assert evaluate_gate(spec=spec, decisive=no_improvement)["verdict"] == (
        "without_wins"
    )
