"""Deterministic, provenance-closed T9.1 document routing profiles."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from models.claim_record import ClaimArgumentV1, ClaimCompilationV1, ClaimRecordV1
from models.document_semantic_profile import T91DocumentProfileV1
from models.local_extraction import (
    EntityMention,
    LocalExtractionV1,
    PredicateMention,
)
from scripts.materialize_t91_document_profiles import _document_ordinals
from services.ingestion.document_semantic_profile import (
    DocumentProfileCompilationError,
    compile_document_profile,
)
from services.retriever.four_lane_router import (
    DocumentProfile,
    apply_t91_document_profiles,
)


SOURCE_HASH = "a" * 64


def _document() -> dict:
    return {
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "original_filename": "Directing the Story.md",
        "source_key": f"sha256:{SOURCE_HASH}",
        "source_identity": {
            "content_sha256": SOURCE_HASH,
            "source_key": f"sha256:{SOURCE_HASH}",
        },
        "doc_profile": {
            "summary": "Narrative craft coordinates visual choices.",
            "concepts": ["Narrative craft", "the"],
            "section_ids": ["section:1"],
        },
    }


def _claim() -> ClaimRecordV1:
    return ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id="claim:story",
        document_id="doc:test",
        child_id="child:1",
        proposition_text="Storytelling influences AI video prompts.",
        canonical_proposition="storytelling influence ai video prompt",
        claim_type="causal",
        predicate_observation_id="predicate-observation:story",
        predicate_id="predicate:story",
        predicate_surface="influences",
        predicate_lemma="influence",
        normalized_predicate="INFLUENCES",
        typing_status="typed",
        arguments=[
            ClaimArgumentV1(
                role="subject",
                filler_kind="entity_mention",
                filler_ref="mention:story",
                span_observation_id="span:story",
                surface="Storytelling",
                start_char=0,
                end_char=12,
                evidence_sentence_id="sentence:1",
            ),
            ClaimArgumentV1(
                role="object",
                filler_kind="entity_mention",
                filler_ref="mention:prompt",
                span_observation_id="span:prompt",
                surface="AI video prompts",
                start_char=24,
                end_char=40,
                evidence_sentence_id="sentence:1",
            ),
        ],
        polarity="positive",
        modality="asserted",
        assertion_mode="reported",
        conditions=[],
        exceptions=[],
        temporal_cues=[],
        evidence_sentence_ids=["sentence:1"],
        source_relation_ids=[],
        scope_hash="sha256:profile-test-scope",
        knowledge_status="candidate",
        validation_status="candidate",
    )


def _extraction_row() -> dict:
    local = LocalExtractionV1(
        schema_version="local_extraction.v1",
        document_id="doc:test",
        child_id="child:1",
        sentence_ids=["sentence:1"],
        entities=[
            EntityMention(
                mention_id="mention:story",
                text="Storytelling",
                entity_type="CONCEPT",
                start_char=0,
                end_char=12,
                canonical_label="Storytelling",
                confidence=1.0,
            ),
            EntityMention(
                mention_id="mention:prompt",
                text="AI video prompts",
                entity_type="METHOD",
                start_char=24,
                end_char=40,
                canonical_label="AI video prompting",
                confidence=1.0,
            ),
        ],
        predicates=[
            PredicateMention(
                predicate_id="predicate:story",
                surface_text="influences",
                lemma="influence",
                normalized_predicate="INFLUENCES",
                start_char=13,
                end_char=23,
                negated=False,
                modality="asserted",
                confidence=1.0,
            )
        ],
        relations=[],
        unresolved_spans=[],
    )
    compilation = ClaimCompilationV1(
        schema_version="claim_compilation.v1",
        document_id="doc:test",
        child_id="child:1",
        claims=[_claim()],
        links=[],
        rejected_relation_ids=[],
        unresolved_coreference_spans=[],
        skipped_predicate_observation_ids=[],
        same_sentence_repeated_claim_count=0,
        cross_sentence_candidate_count=0,
        cross_sentence_rejected_count=0,
        compiler_recipe_hash="sha256:claim-compiler-fixture",
    )
    return {
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "chunk_id": "child:1",
        "status": "ok",
        "_document_ordinal": 0,
        "_parent_id": "parent:1",
        "stage_identity": {
            "identity_version": "stage_identity.v1",
            "source_file_hash": SOURCE_HASH,
            "source_key": f"sha256:{SOURCE_HASH}",
            "chunk_hash": "sha256:chunk",
            "extraction_contract_hash": "sha256:contract",
        },
        "local_extraction": local.model_dump(mode="python"),
        "claim_compilation": compilation.model_dump(mode="python"),
    }


def _compile() -> T91DocumentProfileV1:
    return compile_document_profile(
        document=_document(),
        parent_rows=[
            {
                "parent_id": "parent:1",
                "heading_path": ["Film"],
                "summary": "Directing choices support a coherent scene.",
                "child_ids": ["child:1"],
            }
        ],
        extraction_rows=[_extraction_row()],
    )


def _part_of_extraction_row(
    ordinal: int,
    source: str,
    target: str,
) -> dict:
    child_id = f"child:motif:{ordinal}"
    sentence_id = f"sentence:motif:{ordinal}"
    source_id = f"mention:motif:{ordinal}:source"
    target_id = f"mention:motif:{ordinal}:target"
    predicate_id = f"predicate:motif:{ordinal}"
    proposition = f"{source} is part of {target}."
    target_start = len(source) + 12
    local = LocalExtractionV1(
        schema_version="local_extraction.v1",
        document_id="doc:test",
        child_id=child_id,
        sentence_ids=[sentence_id],
        entities=[
            EntityMention(
                mention_id=source_id,
                text=source,
                entity_type="CONCEPT",
                start_char=0,
                end_char=len(source),
                canonical_label=source,
                confidence=1.0,
            ),
            EntityMention(
                mention_id=target_id,
                text=target,
                entity_type="CONCEPT",
                start_char=target_start,
                end_char=target_start + len(target),
                canonical_label=target,
                confidence=1.0,
            ),
        ],
        predicates=[
            PredicateMention(
                predicate_id=predicate_id,
                surface_text="is part of",
                lemma="part",
                normalized_predicate="PART_OF",
                start_char=len(source) + 1,
                end_char=target_start - 1,
                negated=False,
                modality="asserted",
                confidence=1.0,
            )
        ],
        relations=[],
        unresolved_spans=[],
    )
    claim = ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id=f"claim:motif:{ordinal}",
        document_id="doc:test",
        child_id=child_id,
        proposition_text=proposition,
        canonical_proposition=f"{source.lower()} part of {target.lower()}",
        claim_type="association",
        predicate_observation_id=f"predicate-observation:motif:{ordinal}",
        predicate_id=predicate_id,
        predicate_surface="is part of",
        predicate_lemma="part",
        normalized_predicate="PART_OF",
        typing_status="typed",
        arguments=[
            ClaimArgumentV1(
                role="subject",
                filler_kind="entity_mention",
                filler_ref=source_id,
                span_observation_id=f"span:motif:{ordinal}:source",
                surface=source,
                start_char=0,
                end_char=len(source),
                evidence_sentence_id=sentence_id,
            ),
            ClaimArgumentV1(
                role="object",
                filler_kind="entity_mention",
                filler_ref=target_id,
                span_observation_id=f"span:motif:{ordinal}:target",
                surface=target,
                start_char=target_start,
                end_char=target_start + len(target),
                evidence_sentence_id=sentence_id,
            ),
        ],
        polarity="positive",
        modality="asserted",
        assertion_mode="reported",
        conditions=[],
        exceptions=[],
        temporal_cues=[],
        evidence_sentence_ids=[sentence_id],
        source_relation_ids=[],
        scope_hash=f"sha256:motif-scope:{ordinal}",
        knowledge_status="candidate",
        validation_status="candidate",
    )
    compilation = ClaimCompilationV1(
        schema_version="claim_compilation.v1",
        document_id="doc:test",
        child_id=child_id,
        claims=[claim],
        links=[],
        rejected_relation_ids=[],
        unresolved_coreference_spans=[],
        skipped_predicate_observation_ids=[],
        same_sentence_repeated_claim_count=0,
        cross_sentence_candidate_count=0,
        cross_sentence_rejected_count=0,
        compiler_recipe_hash="sha256:claim-compiler-fixture",
    )
    return {
        "corpus_id": "corpus:test",
        "doc_id": "doc:test",
        "chunk_id": child_id,
        "status": "ok",
        "_document_ordinal": ordinal,
        "_parent_id": "parent:motif",
        "stage_identity": {
            "identity_version": "stage_identity.v1",
            "source_file_hash": SOURCE_HASH,
            "source_key": f"sha256:{SOURCE_HASH}",
            "chunk_hash": f"sha256:chunk:{ordinal}",
            "extraction_contract_hash": "sha256:contract",
        },
        "local_extraction": local.model_dump(mode="python"),
        "claim_compilation": compilation.model_dump(mode="python"),
    }


def test_profile_compiles_t91_domains_frames_and_grounded_concepts():
    profile = _compile()

    assert profile.domain_ids == ["D13"]
    assert profile.superframe_ids == ["MF04"]
    assert profile.motif_ids == []
    assert {"storytelling", "video", "prompting"} <= set(profile.concept_terms)
    assert "the" not in profile.concept_terms
    assert profile.domain_evidence[0].assignment_role == "dominant"
    assert profile.frame_evidence[0].source_claim_id == "claim:story"
    assert [item.source_kind for item in profile.source_slices] == [
        "document",
        "extraction_rows",
        "parent_chunks",
    ]
    assert profile.assignment_state == "candidate"
    assert profile.canonical_write is False
    assert profile.llm_call_count == 0
    assert profile.provider_spend_usd == 0.0


def test_profile_compiles_strict_role_threaded_motif_within_parent_boundary():
    rows = [
        _part_of_extraction_row(0, "Scene", "Sequence"),
        _part_of_extraction_row(1, "Sequence", "Narrative"),
        _part_of_extraction_row(2, "Narrative", "Story"),
    ]

    profile = compile_document_profile(
        document=_document(),
        parent_rows=[
            {
                "parent_id": "parent:motif",
                "heading_path": ["Film"],
                "summary": "A structural chain.",
                "child_ids": [row["chunk_id"] for row in rows],
            }
        ],
        extraction_rows=rows,
    )

    assert "M09" in profile.motif_ids
    motif = next(item for item in profile.motif_evidence if item.motif_id == "M09")
    assert motif.frame_ids == ["MF16"]
    assert motif.matcher_disposition == "confirmed_candidate"
    assert len(motif.source_claim_ids) == 3


def test_profile_replay_is_byte_stable_and_hash_closed():
    first = _compile()
    second = _compile()

    assert first.model_dump_json() == second.model_dump_json()
    payload = first.model_dump(mode="python")
    payload["concept_terms"] = sorted([*payload["concept_terms"], "tampered"])
    with pytest.raises(ValidationError, match="concept evidence does not close"):
        T91DocumentProfileV1.model_validate(payload)


def test_profile_rejects_stale_extraction_source_identity():
    row = _extraction_row()
    row["stage_identity"]["source_file_hash"] = "b" * 64

    with pytest.raises(
        DocumentProfileCompilationError,
        match="source identity is stale",
    ):
        compile_document_profile(
            document=_document(),
            parent_rows=[],
            extraction_rows=[row],
        )


def test_materializer_ordinals_preserve_parent_child_order_without_writes():
    ordinals = _document_ordinals(
        [
            {"parent_id": "p2", "child_ids": ["c3"]},
            {"parent_id": "p1", "child_ids": ["c2", "c1"]},
        ],
        [
            {"chunk_id": "c1"},
            {"chunk_id": "c2"},
            {"chunk_id": "c3"},
            {"chunk_id": "orphan"},
        ],
    )

    assert ordinals == {
        "c2": (0, "p1"),
        "c1": (1, "p1"),
        "c3": (2, "p2"),
        "orphan": (3, ""),
    }


def test_router_attachment_requires_current_source_and_recipe_closure():
    durable = _compile()
    target = DocumentProfile(corpus_id="corpus:test", doc_id="doc:test")
    profiles = {("corpus:test", "doc:test"): target}

    stale = apply_t91_document_profiles(
        profiles=profiles,
        rows=[durable.model_dump(mode="python")],
        current_source_versions={("corpus:test", "doc:test"): "stale"},
        expected_profile_recipe_hash=durable.profile_recipe_hash,
    )
    assert stale == {"seen": 1, "valid": 1, "current": 0, "applied": 0}
    assert target.has_ontology is False

    current = apply_t91_document_profiles(
        profiles=profiles,
        rows=[durable.model_dump(mode="python")],
        current_source_versions={
            ("corpus:test", "doc:test"): durable.source_version_id
        },
        expected_profile_recipe_hash=durable.profile_recipe_hash,
    )
    assert current == {"seen": 1, "valid": 1, "current": 1, "applied": 1}
    assert target.domains == {"D13"}
    assert target.frames == {"MF04"}
    assert "storytelling" in target.concept_terms
