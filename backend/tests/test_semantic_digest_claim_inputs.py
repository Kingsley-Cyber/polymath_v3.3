"""B2 noncanonical compilation and atomic parent-packet contracts."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace

import pytest
from bson import BSON
from bson.codec_options import CodecOptions
from pydantic import ValidationError

from models.claim_record import ClaimArgumentV1, ClaimCompilationV1, ClaimRecordV1
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.semantic_artifacts import domain_hash, make_evidence_ref
from models.semantic_digest_claim_input import (
    CompiledChildCandidateExportV1,
    parse_materialized_row_document,
)
from models.semantic_parent_packet import (
    ATOMIC_PACKET_SCHEMA_VERSION,
    BOUNDED_ATOMIC_PACKET_SCHEMA_VERSION,
    BOUNDED_PACKET_MAX_UTF8_BYTES,
    SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES,
    SENTENCE_HYBRID_PACKET_SCHEMA_VERSION,
    SemanticParentPacketAtomicClaimsV1,
    SemanticParentPacketAtomicClaimsV2,
    SemanticParentPacketSentenceHybridV3,
    semantic_parent_packet_atomic_schema_hash,
    semantic_parent_packet_bounded_schema_hash,
    semantic_parent_packet_sentence_hybrid_schema_hash,
)
from models.semantic_digest import SemanticDigestV1
from models.semantic_validator import semantic_validate
from services.ingestion.claim_compiler import claim_compiler_recipe_hash
from services.ingestion.semantic_digest_claim_inputs import (
    PARSER_VERSION,
    SPACY_MODEL,
    ClaimInputError,
    PacketNotReadyError,
    _expected_observation_bundle_id,
    build_atomic_parent_packet,
    build_bounded_atomic_parent_packet,
    build_sentence_hybrid_parent_packet,
    compile_child_candidate,
    document_source_version_id,
    expand_sentence_claim_ids,
    materialize_candidate_row,
    load_bounded_selection_recipe,
    validate_candidate_against_source,
    validate_materialized_row_against_source,
)
from services.ingestion.semantic_observations import (
    load_normalization_identity,
    local_extraction_recipe_hash,
    semantic_observation_recipe_hash,
)


def _document(document_id: str = "doc:test") -> dict:
    return {
        "doc_id": document_id,
        "source_identity": {"content_sha256": "a" * 64},
    }


def _child(
    text: str = "Feedback changes the operating baseline.",
    *,
    child_id: str = "child:test",
    document_id: str = "doc:test",
) -> dict:
    return {
        "chunk_id": child_id,
        "doc_id": document_id,
        "text": text,
    }


def _claim(
    claim_id: str,
    evidence_id: str,
    *,
    document_id: str = "doc:test",
    child_id: str = "child:test",
    proposition_text: str = "Feedback changes the operating baseline.",
) -> ClaimRecordV1:
    return ClaimRecordV1(
        schema_version="claim_record.v1",
        claim_id=claim_id,
        document_id=document_id,
        child_id=child_id,
        proposition_text=proposition_text,
        canonical_proposition=f"feedback POSITIVE ASSERTED change baseline {claim_id}",
        claim_type="causal",
        predicate_observation_id=f"predicate-observation:{claim_id}",
        predicate_id=f"predicate:{claim_id}",
        predicate_surface="changes",
        predicate_lemma="change",
        normalized_predicate="INFLUENCES",
        typing_status="typed",
        arguments=[
            ClaimArgumentV1(
                role="subject",
                filler_kind="span_observation",
                filler_ref="span:subject",
                span_observation_id="span:subject",
                surface="Feedback",
                start_char=0,
                end_char=8,
                evidence_sentence_id=evidence_id,
            ),
            ClaimArgumentV1(
                role="object",
                filler_kind="span_observation",
                filler_ref="span:object",
                span_observation_id="span:object",
                surface="the operating baseline",
                start_char=17,
                end_char=39,
                evidence_sentence_id=evidence_id,
            ),
        ],
        polarity="positive",
        modality="asserted",
        assertion_mode="reported",
        conditions=[],
        exceptions=[],
        temporal_cues=[],
        evidence_sentence_ids=[evidence_id],
        source_relation_ids=[],
        scope_hash=namespace_hash("scope", {"child": child_id}),
        knowledge_status="candidate",
        validation_status="candidate",
    )


def _candidate(
    *,
    claim_count: int = 2,
    child_id: str = "child:test",
    document_id: str = "doc:test",
    text: str = "Feedback changes the operating baseline.",
) -> CompiledChildCandidateExportV1:
    document = _document(document_id)
    child = _child(text, child_id=child_id, document_id=document_id)
    source_version = document_source_version_id(document)
    evidence = make_evidence_ref(
        text=child["text"],
        start=0,
        end=len(child["text"]),
        source_version_id=source_version,
        hierarchy_node_id=child["chunk_id"],
    )
    observation_recipe = semantic_observation_recipe_hash(
        parser_id=SPACY_MODEL,
        parser_version=PARSER_VERSION,
    )
    compiler_recipe = claim_compiler_recipe_hash(observation_recipe)
    claims = [
        _claim(
            f"claim:{child_id}:{index}",
            evidence.evidence_ref_id,
            document_id=document_id,
            child_id=child_id,
            proposition_text=evidence.quote,
        )
        for index in range(claim_count)
    ]
    compilation = ClaimCompilationV1(
        schema_version="claim_compilation.v1",
        document_id=document["doc_id"],
        child_id=child["chunk_id"],
        claims=claims,
        links=[],
        rejected_relation_ids=[],
        unresolved_coreference_spans=[],
        skipped_predicate_observation_ids=[],
        same_sentence_repeated_claim_count=max(0, claim_count - 1),
        cross_sentence_candidate_count=0,
        cross_sentence_rejected_count=0,
        compiler_recipe_hash=compiler_recipe,
    )
    source_text_hash = domain_hash("normalized-text", child["text"])
    return CompiledChildCandidateExportV1(
        schema_version="semantic_digest_claim_compilation_export.v1",
        corpus_id="corpus:test",
        document_id=document["doc_id"],
        source_version_id=source_version,
        child_id=child["chunk_id"],
        source_text_hash=source_text_hash,
        observation_bundle_id=_expected_observation_bundle_id(
            source_version_id_=source_version,
            child_id=child["chunk_id"],
            source_text_hash=source_text_hash,
            observation_recipe_hash=observation_recipe,
        ),
        observation_recipe_hash=observation_recipe,
        local_extraction_recipe_hash=local_extraction_recipe_hash(),
        normalization_registry_hash=load_normalization_identity()["hash"],
        compiler_version="claim_compiler.v2",
        compiler_recipe_hash=compiler_recipe,
        spacy_library_version="3.8.14",
        spacy_model="en_core_web_sm",
        spacy_model_version="3.8.0",
        parser_version=PARSER_VERSION,
        evidence_refs=[evidence],
        compilation=compilation,
    )


def _row(
    *,
    claim_count: int = 2,
    child_id: str = "child:test",
    document_id: str = "doc:test",
    text: str = "Feedback changes the operating baseline.",
):
    return materialize_candidate_row(
        _candidate(
            claim_count=claim_count,
            child_id=child_id,
            document_id=document_id,
            text=text,
        ),
        corpus_id="corpus:test",
        document=_document(document_id),
        child=_child(text, child_id=child_id, document_id=document_id),
        run_id="run:test",
        now=dt.datetime(2026, 7, 14, 20, 0, tzinfo=dt.timezone.utc),
    )


def _row_with_context(
    context_text: str = "Context preserves continuity.",
    *,
    claim_count: int = 2,
):
    document = _document()
    claim_text = "Feedback changes the operating baseline."
    text = f"{claim_text} {context_text}"
    child = _child(text)
    source_version = document_source_version_id(document)
    claim_ref = make_evidence_ref(
        text=text,
        start=0,
        end=len(claim_text),
        source_version_id=source_version,
        hierarchy_node_id=child["chunk_id"],
    )
    context_ref = make_evidence_ref(
        text=text,
        start=len(claim_text) + 1,
        end=len(text),
        source_version_id=source_version,
        hierarchy_node_id=child["chunk_id"],
    )
    observation_recipe = semantic_observation_recipe_hash(
        parser_id=SPACY_MODEL,
        parser_version=PARSER_VERSION,
    )
    compiler_recipe = claim_compiler_recipe_hash(observation_recipe)
    claims = [
        _claim(
            f"claim:child:test:{index}",
            claim_ref.evidence_ref_id,
            proposition_text=claim_ref.quote,
        )
        for index in range(claim_count)
    ]
    compilation = ClaimCompilationV1(
        schema_version="claim_compilation.v1",
        document_id=document["doc_id"],
        child_id=child["chunk_id"],
        claims=claims,
        links=[],
        rejected_relation_ids=[],
        unresolved_coreference_spans=[],
        skipped_predicate_observation_ids=[],
        same_sentence_repeated_claim_count=max(0, claim_count - 1),
        cross_sentence_candidate_count=0,
        cross_sentence_rejected_count=0,
        compiler_recipe_hash=compiler_recipe,
    )
    source_text_hash = domain_hash("normalized-text", text)
    candidate = CompiledChildCandidateExportV1(
        schema_version="semantic_digest_claim_compilation_export.v1",
        corpus_id="corpus:test",
        document_id=document["doc_id"],
        source_version_id=source_version,
        child_id=child["chunk_id"],
        source_text_hash=source_text_hash,
        observation_bundle_id=_expected_observation_bundle_id(
            source_version_id_=source_version,
            child_id=child["chunk_id"],
            source_text_hash=source_text_hash,
            observation_recipe_hash=observation_recipe,
        ),
        observation_recipe_hash=observation_recipe,
        local_extraction_recipe_hash=local_extraction_recipe_hash(),
        normalization_registry_hash=load_normalization_identity()["hash"],
        compiler_version="claim_compiler.v2",
        compiler_recipe_hash=compiler_recipe,
        spacy_library_version="3.8.14",
        spacy_model="en_core_web_sm",
        spacy_model_version="3.8.0",
        parser_version=PARSER_VERSION,
        evidence_refs=[claim_ref, context_ref],
        compilation=compilation,
    )
    return materialize_candidate_row(
        candidate,
        corpus_id="corpus:test",
        document=document,
        child=child,
        run_id="run:test",
        now=dt.datetime(2026, 7, 14, 20, 0, tzinfo=dt.timezone.utc),
    )


def _parent(*, child_ids: list[str] | None = None) -> dict:
    return {
        "parent_id": "parent:test",
        "doc_id": "doc:test",
        "text": "## Context\n" + "Feedback changes the operating baseline. " * 8,
        "validation_status": "valid",
        "child_ids": child_ids or ["child:test"],
    }


def _extraction(child_id: str = "child:test") -> dict:
    return {
        "chunk_id": child_id,
        "status": "ok",
        "schema_version": "polymath.extract.v1",
        "entities": [
            {
                "canonical_name": "Operating Baseline",
                "entity_type": "CONCEPT",
                "surface_form": "baseline",
                "query_aliases": ["reference", "reference"],
                "confidence": 0.91,
            }
        ],
    }


def test_materialized_row_is_typed_immutable_and_noncanonical() -> None:
    row = _row()

    assert row.row_id == row.envelope.artifact_revision_id
    assert row.canonical_write is False
    assert row.status == "candidate"
    assert row.envelope.artifact_state == "candidate"
    assert row.envelope.knowledge_status is None
    assert row.envelope.body.schema_version == "claim_compilation.v1"
    validate_materialized_row_against_source(
        row,
        corpus_id="corpus:test",
        document=_document(),
        child=_child(),
    )


def test_materialized_row_survives_strict_bson_json_round_trip() -> None:
    row = _row()
    encoded = BSON.encode(row.model_dump(mode="python", by_alias=True))
    default_bson_shape = BSON(encoded).decode()
    aware_bson_shape = BSON(encoded).decode(codec_options=CodecOptions(tz_aware=True))

    with pytest.raises(ValueError, match="naive datetime"):
        parse_materialized_row_document(default_bson_shape)
    replay = parse_materialized_row_document(aware_bson_shape)

    assert replay == row
    assert replay.envelope.lifecycle.created_at.utcoffset() == dt.timedelta(0)
    assert isinstance(replay.envelope.provenance.raw_artifact_ids, tuple)
    assert isinstance(replay.envelope.validation.errors, tuple)


def test_atomic_packet_deduplicates_quote_but_preserves_distinct_claims() -> None:
    row = _row(claim_count=2)
    built = build_atomic_parent_packet(
        corpus_id="corpus:test",
        corpus_name="test",
        parent=_parent(),
        compilation_rows={"child:test": row},
        extraction_rows=[_extraction()],
        max_entities=40,
    )
    packet = built.packet

    assert packet.packet_schema_version == ATOMIC_PACKET_SCHEMA_VERSION
    assert len(packet.claims) == 2
    assert len(packet.evidence_sentences) == 1
    assert "proposition_text" not in packet.claims[0].model_dump()
    assert packet.evidence_sentences[0].text == _child()["text"]
    assert packet.evidence_contract.claims_interim is False
    assert packet.evidence_contract.relation_disposition == (
        "relations_remain_observation_only"
    )
    assert packet.evidence_contract.source_compilation_revision_ids == [row.row_id]
    assert [scope.claim_id for scope in built.context.claims] == [
        item.claim_id for item in packet.claims
    ]
    restored = [
        item.to_claim_record(proposition_text=packet.evidence_sentences[0].text)
        for item in packet.claims
    ]
    assert restored == row.envelope.body.claims
    assert "interim-claim:" not in packet.model_dump_json()


def test_packet_schema_hash_and_cross_process_json_round_trip_are_stable() -> None:
    packet = build_atomic_parent_packet(
        corpus_id="corpus:test",
        corpus_name="test",
        parent=_parent(),
        compilation_rows={"child:test": _row()},
        extraction_rows=[_extraction()],
        max_entities=40,
    ).packet

    replay = SemanticParentPacketAtomicClaimsV1.model_validate_json(
        packet.model_dump_json()
    )
    assert replay == packet
    assert semantic_parent_packet_atomic_schema_hash().startswith("sha256:")


def test_bounded_packet_is_claim_only_and_validator_scope_is_emitted_only() -> None:
    built = build_bounded_atomic_parent_packet(
        corpus_id="corpus:test",
        corpus_name="test",
        parent=_parent(),
        compilation_rows={"child:test": _row()},
        extraction_rows=[_extraction()],
        max_entities=40,
    )
    packet = built.packet
    values = packet.model_dump(mode="python")

    assert packet.packet_schema_version == BOUNDED_ATOMIC_PACKET_SCHEMA_VERSION
    assert "parent_text" not in values
    assert "evidence_sentences" not in values
    assert set(values["claims"][0]) == {
        "claim_id",
        "canonical_claim_text",
        "typing_status",
        "polarity",
        "evidence_sentence_id",
    }
    assert (
        len(packet.model_dump_json().encode("utf-8")) <= BOUNDED_PACKET_MAX_UTF8_BYTES
    )
    assert {item.claim_id for item in built.context.claims} == {
        item.claim_id for item in packet.claims
    }
    assert {item.claim_id for item in built.emitted_claim_records} == {
        item.claim_id for item in packet.claims
    }
    assert not built.excluded_claim_records
    assert packet.selection_manifest.proposal_space_disposition == (
        "bounded_to_emitted_claims_excluded_claims_remain_local"
    )


def test_bounded_packet_cap_is_deterministic_and_exclusions_are_manifested() -> None:
    row = _row(claim_count=180)
    kwargs = {
        "corpus_id": "corpus:test",
        "corpus_name": "test",
        "parent": _parent(),
        "compilation_rows": {"child:test": row},
        "extraction_rows": [_extraction()],
        "max_entities": 40,
    }

    first = build_bounded_atomic_parent_packet(**kwargs)
    second = build_bounded_atomic_parent_packet(**kwargs)
    manifest = first.packet.selection_manifest

    assert first.packet == second.packet
    assert manifest.cap_applied is True
    assert 0 < manifest.emitted_claim_count < manifest.source_claim_count
    assert manifest.excluded_claim_count == len(first.excluded_claim_records)
    assert {item.claim_id for item in first.excluded_claim_byte_decisions} == {
        item.claim_id for item in first.excluded_claim_records
    }
    assert first.excluded_claim_byte_decisions == second.excluded_claim_byte_decisions
    assert all(
        item.first_attempted_packet_utf8_bytes > item.max_packet_utf8_bytes
        and item.last_attempted_packet_utf8_bytes > item.max_packet_utf8_bytes
        and item.rejection_attempt_count >= 1
        for item in first.excluded_claim_byte_decisions
    )
    assert len(first.packet.model_dump_json().encode("utf-8")) <= 20_000
    assert manifest.source_claim_count == (
        manifest.emitted_claim_count + manifest.excluded_claim_count
    )


def test_bounded_packet_fails_when_a_source_child_has_no_atomic_claim() -> None:
    with pytest.raises(ClaimInputError, match="source_child_without_atomic_claim"):
        build_bounded_atomic_parent_packet(
            corpus_id="corpus:test",
            corpus_name="test",
            parent=_parent(child_ids=["child:test", "child:empty"]),
            compilation_rows={
                "child:test": _row(),
                "child:empty": _row(
                    claim_count=0,
                    child_id="child:empty",
                    text="A quiet section without compiled claims.",
                ),
            },
            extraction_rows=[_extraction()],
            max_entities=40,
        )


def test_bounded_recipe_and_schema_identity_are_frozen() -> None:
    recipe, recipe_hash = load_bounded_selection_recipe()

    assert recipe.recipe_version == "atomic_claim_packet_selection.v2"
    assert recipe.max_packet_utf8_bytes == 20_000
    assert recipe.priority_lanes == ["typed", "negative", "nuanced", "ordinary"]
    assert recipe_hash.startswith("sha256:")
    assert semantic_parent_packet_bounded_schema_hash().startswith("sha256:")


def test_bounded_packet_rejects_raw_parent_or_quote_body_fields() -> None:
    packet = build_bounded_atomic_parent_packet(
        corpus_id="corpus:test",
        corpus_name="test",
        parent=_parent(),
        compilation_rows={"child:test": _row()},
        extraction_rows=[_extraction()],
        max_entities=40,
    ).packet
    values = packet.model_dump(mode="python")
    values["parent_text"] = "must stay local"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        SemanticParentPacketAtomicClaimsV2.model_validate(values)


def _sentence_hybrid_build():
    row = _row_with_context()
    return build_sentence_hybrid_parent_packet(
        corpus_id="corpus:test",
        corpus_name="test",
        parent=_parent(),
        compilation_rows={"child:test": row},
        extraction_rows=[_extraction()],
        max_entities=40,
    )


def _digest_citing(parent_id: str, claim_id: str) -> SemanticDigestV1:
    return SemanticDigestV1(
        schema_version="semantic_digest.v1",
        parent_id=parent_id,
        summary="Grounded summary.",
        central_thesis="Grounded thesis.",
        underlying_meanings=[
            {
                "text": "Grounded meaning.",
                "supporting_claim_ids": [claim_id],
            }
        ],
        domain_proposals=[],
        frame_proposals=[],
        latent_concepts=[],
        motif_proposals=[],
        conditions=[],
        exceptions=[],
        unresolved_interpretations=[],
    )


def test_sentence_hybrid_packet_preserves_all_ordered_prose_and_discloses_mapping() -> (
    None
):
    built = _sentence_hybrid_build()
    packet = built.packet
    values = packet.provider_payload()

    assert packet.packet_schema_version == SENTENCE_HYBRID_PACKET_SCHEMA_VERSION
    assert [item.text for item in packet.sentence_units] == [
        "Feedback changes the operating baseline.",
        "Context preserves continuity.",
    ]
    assert "claim_id" in values["sentence_units"][0]
    assert "claim_id" not in values["sentence_units"][1]
    assert "object_kind" not in values["extraction_entities"][0]
    assert packet.sentence_counts.mapped == 1
    assert packet.sentence_counts.unmapped == 1
    assert packet.evidence_contract.claims_interim is True
    assert "claims" not in values
    assert "provider_atomic_claims_visible" not in values["evidence_contract"]
    assert [item.claim_id for item in built.context.claims] == [
        built.ordered_evidence_sentence_ids[0]
    ]
    assert built.packet_utf8_bytes == len(canonical_json_v1(values).encode("utf-8"))
    assert built.packet_utf8_bytes <= SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES


def test_sentence_hybrid_order_replays_parent_child_then_source_offsets() -> None:
    rows = {
        "child:z": _row(
            claim_count=1,
            child_id="child:z",
            text="Zeta evidence closes first.",
        ),
        "child:a": _row(
            claim_count=1,
            child_id="child:a",
            text="Alpha evidence closes second.",
        ),
    }
    kwargs = {
        "corpus_id": "corpus:test",
        "corpus_name": "test",
        "parent": _parent(child_ids=["child:z", "child:a", "child:z"]),
        "compilation_rows": rows,
        "extraction_rows": [_extraction("child:z"), _extraction("child:a")],
        "max_entities": 40,
    }

    first = build_sentence_hybrid_parent_packet(**kwargs)
    replay = build_sentence_hybrid_parent_packet(**kwargs)

    assert first.packet == replay.packet
    assert first.source_child_ids == ("child:z", "child:a")
    assert [item.text for item in first.packet.sentence_units] == [
        "Zeta evidence closes first.",
        "Alpha evidence closes second.",
    ]
    assert first.ordered_evidence_sentence_ids == (
        rows["child:z"].evidence_refs[0].evidence_ref_id,
        rows["child:a"].evidence_refs[0].evidence_ref_id,
    )
    assert first.source_child_order_hash == replay.source_child_order_hash
    assert first.sentence_order_hash == replay.sentence_order_hash


def test_context_only_sentence_is_absent_from_validator_scope() -> None:
    built = _sentence_hybrid_build()
    context_only_id = built.ordered_evidence_sentence_ids[1]

    errors = semantic_validate(
        _digest_citing(built.parent_id, context_only_id),
        built.context,
    )

    assert any("unknown claim_id" in error for error in errors)


def test_sentence_expansion_preserves_direct_ids_and_records_sorted_atomic_union() -> (
    None
):
    built = _sentence_hybrid_build()
    sentence_id = built.ordered_evidence_sentence_ids[0]

    expanded = expand_sentence_claim_ids(
        built,
        [sentence_id],
        expected_parent_id=built.parent_id,
        expected_source_compilation_revision_ids=(
            built.source_compilation_revision_ids
        ),
    )

    assert expanded.sentence_claim_ids == (sentence_id,)
    assert expanded.supporting_atomic_claim_ids == (
        "claim:child:test:0",
        "claim:child:test:1",
    )
    assert expanded.mapping_cardinalities == (2,)
    assert expanded.disposition == "deterministic_expansion_not_semantic_selection"


def test_sentence_expansion_fails_on_context_stale_or_cross_closure_mapping() -> None:
    built = _sentence_hybrid_build()
    mapped_id, context_id = built.ordered_evidence_sentence_ids
    kwargs = {
        "expected_parent_id": built.parent_id,
        "expected_source_compilation_revision_ids": (
            built.source_compilation_revision_ids
        ),
    }

    with pytest.raises(ClaimInputError, match="context-only or outside closure"):
        expand_sentence_claim_ids(built, [context_id], **kwargs)
    with pytest.raises(ClaimInputError, match="revisions are stale"):
        expand_sentence_claim_ids(
            built,
            [mapped_id],
            expected_parent_id=built.parent_id,
            expected_source_compilation_revision_ids=["revision:stale"],
        )

    cross_parent = replace(
        built,
        sentence_atomic_expansions=(
            replace(built.sentence_atomic_expansions[0], parent_id="parent:other"),
        ),
    )
    with pytest.raises(ClaimInputError, match="crosses parent closure"):
        expand_sentence_claim_ids(cross_parent, [mapped_id], **kwargs)

    empty_map = replace(
        built,
        sentence_atomic_expansions=(
            replace(built.sentence_atomic_expansions[0], atomic_claim_ids=()),
        ),
    )
    with pytest.raises(ClaimInputError, match="empty or unstable"):
        expand_sentence_claim_ids(empty_map, [mapped_id], **kwargs)


def test_sentence_hybrid_cap_fails_closed_without_dropping_context() -> None:
    row = _row_with_context("x" * SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES)

    with pytest.raises(
        PacketNotReadyError,
        match="sentence_hybrid_packet_exceeds_byte_bound",
    ):
        build_sentence_hybrid_parent_packet(
            corpus_id="corpus:test",
            corpus_name="test",
            parent=_parent(),
            compilation_rows={"child:test": row},
            extraction_rows=[_extraction()],
            max_entities=40,
        )


def test_sentence_hybrid_model_rejects_count_tampering() -> None:
    values = _sentence_hybrid_build().packet.model_dump(mode="python")
    values["sentence_counts"]["unmapped"] = 0

    with pytest.raises(ValidationError, match="unmapped count disagrees"):
        SemanticParentPacketSentenceHybridV3.model_validate(values)


def test_sentence_hybrid_schema_hash_is_frozen() -> None:
    assert semantic_parent_packet_sentence_hybrid_schema_hash() == (
        "sha256:5c600d3047807541a09be38d01933b6e048f5a3f730de1b5e2cf6c48991f2e40"
    )


def test_packet_fails_closed_instead_of_using_parent_fallback() -> None:
    with pytest.raises(ClaimInputError, match="zero_atomic_claims"):
        build_atomic_parent_packet(
            corpus_id="corpus:test",
            corpus_name="test",
            parent=_parent(),
            compilation_rows={"child:test": _row(claim_count=0)},
            extraction_rows=[_extraction()],
            max_entities=40,
        )


def test_packet_fails_closed_on_missing_child_or_extraction_entity() -> None:
    with pytest.raises(ClaimInputError, match="close over child IDs"):
        build_atomic_parent_packet(
            corpus_id="corpus:test",
            corpus_name="test",
            parent=_parent(),
            compilation_rows={},
            extraction_rows=[_extraction()],
            max_entities=40,
        )
    with pytest.raises(ClaimInputError, match="no_accepted_extraction_entity"):
        build_atomic_parent_packet(
            corpus_id="corpus:test",
            corpus_name="test",
            parent=_parent(),
            compilation_rows={"child:test": _row()},
            extraction_rows=[{**_extraction(), "entities": []}],
            max_entities=40,
        )


def test_candidate_revalidation_detects_source_drift() -> None:
    candidate = _candidate()
    validate_candidate_against_source(
        candidate,
        corpus_id="corpus:test",
        document=_document(),
        child=_child(),
    )
    with pytest.raises(ClaimInputError, match="source text hash drifted"):
        validate_candidate_against_source(
            candidate,
            corpus_id="corpus:test",
            document=_document(),
            child=_child("Feedback no longer changes the baseline."),
        )


def test_packet_model_rejects_unknown_evidence() -> None:
    packet = build_atomic_parent_packet(
        corpus_id="corpus:test",
        corpus_name="test",
        parent=_parent(),
        compilation_rows={"child:test": _row()},
        extraction_rows=[_extraction()],
        max_entities=40,
    ).packet
    values = packet.model_dump(mode="python")
    values["evidence_sentences"] = []
    with pytest.raises(ValidationError, match="missing or cross-child"):
        SemanticParentPacketAtomicClaimsV1.model_validate(values)


def test_trained_spacy_compiles_a_real_atomic_candidate() -> None:
    spacy = pytest.importorskip("spacy")
    try:
        nlp = spacy.load("en_core_web_sm")
    except OSError:
        pytest.skip("trained spaCy model is not installed")
    candidate = compile_child_candidate(
        corpus_id="corpus:test",
        document=_document(),
        child=_child("Discounts decrease reference prices."),
        nlp=nlp,
        spacy_library_version=str(spacy.__version__),
    )

    assert candidate.compiler_version == "claim_compiler.v2"
    assert candidate.compilation.claims
    assert all(
        item.knowledge_status == "candidate" for item in candidate.compilation.claims
    )
