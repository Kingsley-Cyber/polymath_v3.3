from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.extraction_artifact import (
    CANDIDATE_EXTRACTION_ARTIFACT_VERSION,
    CANDIDATE_EXTRACTION_AUTHORITY,
    CANDIDATE_EXTRACTION_SCHEMA_HASH,
    CandidateEntity,
    CandidateRelation,
    OffsetSpan,
)
from services.ghost_b import (
    EntityItem,
    ExtractionFailureItem,
    ExtractionResult,
    FactItem,
    RelationItem,
)
from services.ingestion.extraction_artifacts import (
    adapt_extraction_failure,
    adapt_extraction_result,
)


TEXT = (
    "The National Aeronautics and Space Administration (NASA) uses Python. "
    "Python is a programming language. The system requires 4 GB."
)


def _result() -> ExtractionResult:
    return ExtractionResult(
        schema_version="polymath.extract.v1",
        chunk_id="chunk-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        text=TEXT,
        entities=[
            EntityItem(
                canonical_name="NASA",
                surface_form="NASA",
                entity_type="organization",
                confidence=0.95,
                query_aliases=["provider supplied alias must be ignored"],
                definitional_phrase="provider supplied definition must be ignored",
            ),
            EntityItem(
                canonical_name="Python",
                surface_form="Python",
                entity_type="concept",
                confidence=0.9,
                object_kind="programming language",
            ),
        ],
        relations=[
            RelationItem(
                subject="NASA",
                predicate="uses",
                object="Python",
                object_kind="entity",
                confidence=0.88,
                evidence_phrase=(
                    "The National Aeronautics and Space Administration (NASA) "
                    "uses Python."
                ),
                relation_cue="uses",
                validation_status="accepted",
            )
        ],
        facts=[
            FactItem(
                subject="system",
                fact_type="threshold",
                property_name="memory",
                value="4",
                unit="GB",
                condition=None,
                confidence=1.0,
                evidence_phrase="The system requires 4 GB.",
            )
        ],
        model="legacy-model",
        provider="legacy_local",
        lane=0,
        attempts=1,
    )


def _adapt(**overrides):
    kwargs = {
        "engine": "legacy_local",
        "engine_runtime_version": "legacy-runtime.1",
        "source_wire_contract_version": "polymath.extract.v1",
        "source_contract_hash": "sha256:contract",
        "model_id": "legacy-model",
        "grounded_object_kind_evidence": {
            "Python": "Python is a programming language."
        },
    }
    kwargs.update(overrides)
    return adapt_extraction_result(_result(), **kwargs)


def test_shared_candidate_contract_is_strict_and_identity_bearing() -> None:
    artifact = _adapt()

    assert artifact.schema_version == CANDIDATE_EXTRACTION_ARTIFACT_VERSION
    assert artifact.authority == CANDIDATE_EXTRACTION_AUTHORITY
    assert CANDIDATE_EXTRACTION_SCHEMA_HASH == (
        "sha256:370661b1059bb5c3e7027033d0dba91f399686eda5895bbe780dc39bb620d229"
    )
    assert artifact.provenance.shared_contract_hash == CANDIDATE_EXTRACTION_SCHEMA_HASH
    assert artifact.artifact_id.startswith("candidate-extraction:")
    assert artifact == artifact.model_validate(artifact.model_dump())
    with pytest.raises(ValidationError):
        artifact.model_validate({**artifact.model_dump(), "unexpected": True})


def test_adapter_recomputes_shared_aliases_and_definitions() -> None:
    artifact = _adapt()
    nasa, python = artifact.entities

    assert any(
        value.endswith("National Aeronautics and Space Administration")
        for value in nasa.query_aliases
    )
    assert "provider supplied alias must be ignored" not in nasa.query_aliases
    assert (
        "provider supplied definition must be ignored" not in nasa.definitional_phrase
    )
    assert python.definitional_phrase
    methods = {row.field_path: row for row in artifact.provenance.field_methods}
    assert methods["entities[*].query_aliases"].method == "shared_backend_validation"
    assert (
        methods["entities[*].definitional_phrase"].method == "shared_backend_validation"
    )


def test_offsets_are_exact_only_when_source_match_is_unique() -> None:
    artifact = _adapt()
    nasa, python = artifact.entities

    assert nasa.span.status == "exact"
    # Python appears twice, so the old offset-less ExtractionResult cannot be
    # aligned without guessing which mention the engine meant.
    assert python.span == OffsetSpan(status="unavailable")
    relation_evidence = artifact.evidence[0]
    assert relation_evidence.span.status == "exact"
    assert (
        TEXT[relation_evidence.span.char_start : relation_evidence.span.char_end]
        == relation_evidence.text
    )


def test_object_kind_and_relation_cue_require_exact_source_evidence() -> None:
    grounded = _adapt()
    python = grounded.entities[1]
    relation = grounded.relations[0]

    assert python.object_kind == "programming language"
    assert python.object_kind_evidence_ids
    assert relation.relation_cue == "uses"
    assert relation.relation_cue_evidence_ids
    assert relation.graph_promotion_eligible is True

    ungrounded = _adapt(grounded_object_kind_evidence={})
    assert ungrounded.entities[1].object_kind == ""
    assert ungrounded.entities[1].object_kind_evidence_ids == []

    row = _result()
    row.relations[0].relation_cue = "fabricated cue"
    cue_suppressed = adapt_extraction_result(
        row,
        engine="legacy_local",
        engine_runtime_version="legacy-runtime.1",
        source_wire_contract_version="polymath.extract.v1",
        source_contract_hash="sha256:contract",
    )
    assert cue_suppressed.relations[0].relation_cue == ""
    assert cue_suppressed.relations[0].relation_cue_evidence_ids == []


def test_deterministic_fact_support_is_engine_explicit_and_never_required() -> None:
    legacy = _adapt()
    cloud = _adapt(
        engine="cloud",
        engine_runtime_version="litellm.1",
        model_id="cloud-model",
    )

    assert legacy.provenance.capabilities.deterministic_facts_supported is True
    assert legacy.facts[0].deterministic is True
    assert legacy.facts[0].method == "deterministic_python"
    assert cloud.provenance.capabilities.deterministic_facts_supported is False
    assert cloud.provenance.capabilities.facts_required_for_queryability is False
    assert cloud.facts[0].deterministic is False
    assert cloud.facts[0].method == "engine_model"


def test_failure_adapter_preserves_lane_failure_and_fallback_accounting() -> None:
    failure = ExtractionFailureItem(
        chunk_id="chunk-1",
        doc_id="doc-1",
        corpus_id="corpus-1",
        model="runpod-model",
        lane=2,
        attempts=1,
        error_type="TimeoutError",
        error_message="timed out",
        provider="runpod_flash",
    )

    artifact = adapt_extraction_failure(
        failure,
        engine="runpod_flash",
        engine_runtime_version="image@sha256:abc",
        source_wire_contract_version="polymath.runpod_gliner_relex.v3",
        source_contract_hash="sha256:contract",
        source_text=TEXT,
        fallback_from=["account-a"],
    )

    assert artifact.artifact_status == "failed"
    assert artifact.failure.error_type == "TimeoutError"
    assert artifact.provenance.failure_count == 1
    assert artifact.provenance.fallback_count == 1
    assert artifact.provenance.fallback_from == ["account-a"]


def test_models_reject_ungrounded_object_kind_and_relation_cue() -> None:
    with pytest.raises(ValidationError, match="object_kind requires"):
        CandidateEntity(
            entity_id="e1",
            canonical_name="thing",
            surface_form="thing",
            entity_type="concept",
            object_kind="framework",
            confidence=0.5,
            span=OffsetSpan(status="unavailable"),
            query_aliases=[],
            definitional_phrase="",
            method="engine_model",
            object_kind_evidence_ids=[],
        )
    with pytest.raises(ValidationError, match="relation_cue requires"):
        CandidateRelation(
            relation_id="r1",
            subject="a",
            predicate="uses",
            object="b",
            object_kind="entity",
            confidence=0.5,
            evidence_ids=[],
            relation_cue="uses",
            relation_cue_evidence_ids=[],
            graph_promotion_eligible=False,
            method="engine_model",
        )
