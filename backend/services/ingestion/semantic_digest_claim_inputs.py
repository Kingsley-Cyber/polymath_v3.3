"""Compile, validate, materialize, and packetize deterministic atomic claims.

All durable rows produced here are candidate-only and explicitly
``canonical_write=false``. This module has no provider, projection, graph, or
vector side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from models.artifact_envelope import (
    ArtifactLifecycle,
    ArtifactOwnership,
    ArtifactProvenance,
    ArtifactValidation,
    ProducerKind,
    make_artifact_envelope,
)
from models.claim_record import ClaimCompilationV1, ClaimRecordV1
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.identifier_recipes import source_version_id, work_id
from models.local_extraction import LocalExtractionV1
from models.semantic_artifacts import EvidenceRef, domain_hash, make_evidence_ref
from models.semantic_digest_claim_input import (
    COMPILATION_ARTIFACT_TYPE,
    ClaimCompilationMaterializationRowV1,
    CompiledChildCandidateExportV1,
    evidence_set_hash,
)
from models.semantic_parent_packet import (
    ATOMIC_PACKET_SCHEMA_VERSION,
    BOUNDED_ATOMIC_PACKET_SCHEMA_VERSION,
    BOUNDED_PACKET_MAX_UTF8_BYTES,
    BOUNDED_SELECTION_RECIPE_VERSION,
    AtomicClaimPacketSelectionRecipeV2,
    PacketAtomicClaimV1,
    PacketAtomicClaimV2,
    PacketClaimLinkV2,
    PacketEvidenceContractV1,
    PacketEvidenceContractV2,
    PacketEvidenceSentenceV1,
    PacketExtractionEntityV1,
    PacketCitableSentenceUnitV3,
    PacketEvidenceContractV3,
    PacketSentenceCountsV3,
    PacketSentenceUnitV3,
    PacketSelectionManifestV2,
    SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES,
    SENTENCE_HYBRID_PACKET_SCHEMA_VERSION,
    SelectionLaneAccountingV2,
    SemanticParentPacketAtomicClaimsV1,
    SemanticParentPacketAtomicClaimsV2,
    SemanticParentPacketSentenceHybridV3,
)
from models.semantic_validator import ClaimScope, SemanticValidationContext
from services.ingestion.claim_compiler import (
    COMPILER_VERSION,
    claim_compiler_recipe_hash,
    compile_claim_records_v1,
)
from services.ingestion.semantic_observations import (
    build_spacy_observation_bundle,
    compile_local_extraction_v1,
    load_normalization_identity,
    local_extraction_recipe_hash,
    semantic_observation_recipe_hash,
    validate_evidence_round_trip,
)
from services.ingestion.semantic_parent_eligibility import (
    classify_parent_text_v2,
    parent_eligibility_recipe_hash,
)

SPACY_LIBRARY_VERSION = "3.8.14"
SPACY_MODEL = "en_core_web_sm"
SPACY_MODEL_VERSION = "3.8.0"
PARSER_VERSION = "spacy:3.8.14;model:3.8.0"
MATERIALIZATION_VALIDATOR_VERSION = "semantic_digest_claim_input_validator.v1"
SELECTION_RECIPE_PATH = (
    Path(__file__).resolve().parents[2]
    / "registries"
    / "atomic_claim_packet_selection.v2.json"
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ClaimInputError(ValueError):
    """A deterministic claim input or its source closure is invalid."""


class PacketNotReadyError(ClaimInputError):
    """A B1-eligible parent has one explicit non-packet-ready reason."""

    def __init__(self, reason: str, **details: Any) -> None:
        self.reason = reason
        self.details = dict(details)
        super().__init__(reason)


@dataclass(frozen=True)
class AtomicParentPacketBuild:
    packet: SemanticParentPacketAtomicClaimsV1
    context: SemanticValidationContext
    parent_id: str
    doc_id: str
    entity_count: int
    source_child_count: int
    claim_count: int
    link_count: int
    evidence_count: int


@dataclass(frozen=True)
class BoundedClaimByteExclusion:
    claim_id: str
    first_attempted_packet_utf8_bytes: int
    last_attempted_packet_utf8_bytes: int
    rejection_attempt_count: int
    max_packet_utf8_bytes: int


@dataclass(frozen=True)
class BoundedAtomicParentPacketBuild:
    packet: SemanticParentPacketAtomicClaimsV2
    context: SemanticValidationContext
    parent_id: str
    doc_id: str
    entity_count: int
    source_child_count: int
    source_claim_count: int
    emitted_claim_count: int
    excluded_claim_count: int
    source_link_count: int
    emitted_link_count: int
    excluded_link_count: int
    emitted_claim_records: tuple[ClaimRecordV1, ...]
    excluded_claim_records: tuple[ClaimRecordV1, ...]
    excluded_claim_byte_decisions: tuple[BoundedClaimByteExclusion, ...]


@dataclass(frozen=True)
class SentenceAtomicExpansionV3:
    sentence_claim_id: str
    parent_id: str
    child_id: str
    source_version_id: str
    source_compilation_revision_id: str
    atomic_claim_ids: tuple[str, ...]


@dataclass(frozen=True)
class SentenceHybridParentPacketBuild:
    packet: SemanticParentPacketSentenceHybridV3
    context: SemanticValidationContext
    parent_id: str
    doc_id: str
    entity_count: int
    source_child_ids: tuple[str, ...]
    source_compilation_revision_ids: tuple[str, ...]
    ordered_evidence_sentence_ids: tuple[str, ...]
    sentence_atomic_expansions: tuple[SentenceAtomicExpansionV3, ...]
    source_child_order_hash: str
    source_compilation_set_hash: str
    sentence_order_hash: str
    packet_utf8_bytes: int


@dataclass(frozen=True)
class ExpandedSentenceClaimsV3:
    sentence_claim_ids: tuple[str, ...]
    supporting_atomic_claim_ids: tuple[str, ...]
    mapping_cardinalities: tuple[int, ...]
    source_compilation_set_hash: str
    disposition: str = "deterministic_expansion_not_semantic_selection"


def load_bounded_selection_recipe() -> tuple[AtomicClaimPacketSelectionRecipeV2, str]:
    raw = json.loads(SELECTION_RECIPE_PATH.read_text(encoding="utf-8"))
    recipe = AtomicClaimPacketSelectionRecipeV2.model_validate(raw)
    return recipe, namespace_hash("registry", recipe.model_dump(mode="python"))


def _canonical_source_content_hash(document: Mapping[str, Any]) -> str:
    source_identity = document.get("source_identity")
    if not isinstance(source_identity, Mapping):
        raise ClaimInputError("document source identity is missing")
    value = str(source_identity.get("content_sha256") or "").strip().lower()
    if _HEX64.fullmatch(value):
        return "sha256:" + value
    if value.startswith("sha256:") and _HEX64.fullmatch(value[7:]):
        return value
    raise ClaimInputError("document source content hash is not canonical SHA-256")


def document_source_version_id(document: Mapping[str, Any]) -> str:
    document_id = str(document.get("doc_id") or "").strip()
    if not document_id:
        raise ClaimInputError("document ID is missing")
    return source_version_id(document_id, _canonical_source_content_hash(document))


def _expected_observation_bundle_id(
    *,
    source_version_id_: str,
    child_id: str,
    source_text_hash: str,
    observation_recipe_hash: str,
) -> str:
    identity = {
        "source_version_id": source_version_id_,
        "hierarchy_node_id": child_id,
        "text_hash": source_text_hash,
        "recipe_hash": observation_recipe_hash,
    }
    return (
        "observation-bundle:"
        + domain_hash("observation-bundle", identity).split(":", 1)[1]
    )


def compile_child_candidate(
    *,
    corpus_id: str,
    document: Mapping[str, Any],
    child: Mapping[str, Any],
    nlp: Any,
    spacy_library_version: str,
) -> CompiledChildCandidateExportV1:
    """Compile one current child with the certified pinned spaCy lane."""

    if spacy_library_version != SPACY_LIBRARY_VERSION:
        raise ClaimInputError("spaCy library version is not pinned")
    if str(nlp.meta.get("version") or "") != SPACY_MODEL_VERSION:
        raise ClaimInputError("spaCy model version is not pinned")
    document_id = str(document.get("doc_id") or "").strip()
    child_id = str(child.get("chunk_id") or "").strip()
    child_document_id = str(child.get("doc_id") or "").strip()
    text = child.get("text")
    if not document_id or child_document_id != document_id:
        raise ClaimInputError("child/document ownership does not close")
    if not child_id or not isinstance(text, str) or not text.strip():
        raise ClaimInputError("child identity or text is missing")
    source_version = document_source_version_id(document)
    bundle = build_spacy_observation_bundle(
        text=text,
        nlp=nlp,
        source_version_id=source_version,
        hierarchy_node_id=child_id,
        parser_id=SPACY_MODEL,
        parser_version=PARSER_VERSION,
    )
    round_trip_errors = validate_evidence_round_trip(bundle, text)
    if round_trip_errors:
        raise ClaimInputError("spaCy evidence failed exact source round trip")
    local = compile_local_extraction_v1(
        bundle,
        document_id=document_id,
        child_id=child_id,
    )
    compilation = compile_claim_records_v1(
        bundle=bundle,
        extraction=local.extraction,
    )
    return CompiledChildCandidateExportV1(
        schema_version="semantic_digest_claim_compilation_export.v1",
        corpus_id=corpus_id,
        document_id=document_id,
        source_version_id=source_version,
        child_id=child_id,
        source_text_hash=bundle.text_hash,
        observation_bundle_id=bundle.bundle_id,
        observation_recipe_hash=bundle.recipe_hash,
        local_extraction_recipe_hash=local.recipe_hash,
        normalization_registry_hash=local.normalization_registry_hash,
        compiler_version=COMPILER_VERSION,
        compiler_recipe_hash=compilation.compiler_recipe_hash,
        spacy_library_version=SPACY_LIBRARY_VERSION,
        spacy_model=SPACY_MODEL,
        spacy_model_version=SPACY_MODEL_VERSION,
        parser_version=PARSER_VERSION,
        evidence_refs=bundle.evidence_refs,
        compilation=compilation,
    )


def compile_existing_child_candidate(
    *,
    corpus_id: str,
    document: Mapping[str, Any],
    child: Mapping[str, Any],
    extraction_row: Mapping[str, Any],
    nlp: Any,
    spacy_library_version: str,
) -> CompiledChildCandidateExportV1:
    """Revalidate one durable local-extraction claim body for materialization.

    The extraction worker already compiled the typed claim body. Recompiling
    from raw text alone would silently discard its zero-shot entity lane. This
    boundary rebuilds only the deterministic spaCy observation/evidence layer,
    recompiles the persisted provider-neutral ``LocalExtractionV1``, and
    requires exact equality with the persisted ``ClaimCompilationV1`` before
    producing the same candidate envelope used by the Mark materializer.
    """

    if spacy_library_version != SPACY_LIBRARY_VERSION:
        raise ClaimInputError("spaCy library version is not pinned")
    if str(nlp.meta.get("version") or "") != SPACY_MODEL_VERSION:
        raise ClaimInputError("spaCy model version is not pinned")
    document_id = str(document.get("doc_id") or "").strip()
    child_id = str(child.get("chunk_id") or "").strip()
    text = child.get("text")
    if (
        not document_id
        or str(child.get("doc_id") or "").strip() != document_id
        or not child_id
        or not isinstance(text, str)
        or not text.strip()
    ):
        raise ClaimInputError("existing-claim source ownership does not close")
    if (
        str(extraction_row.get("corpus_id") or "").strip() != corpus_id
        or str(extraction_row.get("doc_id") or "").strip() != document_id
        or str(extraction_row.get("chunk_id") or "").strip() != child_id
        or extraction_row.get("status") != "ok"
        or extraction_row.get("schema_version")
        != "polymath.extract.local_extraction.v1"
    ):
        raise ClaimInputError("existing extraction identity or status drifted")

    source_version = document_source_version_id(document)
    if str(extraction_row.get("source_version_id") or "") != source_version:
        raise ClaimInputError("existing extraction source version drifted")
    bundle = build_spacy_observation_bundle(
        text=text,
        nlp=nlp,
        source_version_id=source_version,
        hierarchy_node_id=child_id,
        parser_id=SPACY_MODEL,
        parser_version=PARSER_VERSION,
    )
    if validate_evidence_round_trip(bundle, text):
        raise ClaimInputError("existing-claim evidence failed exact source round trip")

    try:
        local = LocalExtractionV1.model_validate(extraction_row.get("local_extraction"))
        persisted = ClaimCompilationV1.model_validate(
            extraction_row.get("claim_compilation")
        )
    except Exception as exc:
        raise ClaimInputError("existing claim contracts are invalid") from exc
    if local.document_id != document_id or local.child_id != child_id:
        raise ClaimInputError("existing local extraction ownership drifted")
    evidence_ids = [item.evidence_ref_id for item in bundle.evidence_refs]
    if local.sentence_ids != evidence_ids:
        raise ClaimInputError("existing sentence evidence identity drifted")
    for entity in local.entities:
        if (
            entity.end_char > len(text)
            or text[entity.start_char : entity.end_char] != entity.text
        ):
            raise ClaimInputError("existing entity span failed source round trip")
    for predicate in local.predicates:
        if (
            predicate.end_char > len(text)
            or text[predicate.start_char : predicate.end_char] != predicate.surface_text
        ):
            raise ClaimInputError("existing predicate span failed source round trip")

    recompiled = compile_claim_records_v1(bundle=bundle, extraction=local)
    if recompiled != persisted:
        raise ClaimInputError("existing claim compilation is not byte-deterministic")
    normalization = load_normalization_identity()
    raw_artifact_id = str(extraction_row.get("raw_output_artifact_id") or "").strip()
    if not raw_artifact_id:
        raise ClaimInputError("existing extraction raw artifact lineage is missing")
    return CompiledChildCandidateExportV1(
        schema_version="semantic_digest_claim_compilation_export.v1",
        corpus_id=corpus_id,
        document_id=document_id,
        source_version_id=source_version,
        child_id=child_id,
        source_text_hash=bundle.text_hash,
        observation_bundle_id=bundle.bundle_id,
        observation_recipe_hash=bundle.recipe_hash,
        local_extraction_recipe_hash=local_extraction_recipe_hash(),
        normalization_registry_hash=normalization["hash"],
        compiler_version=COMPILER_VERSION,
        compiler_recipe_hash=persisted.compiler_recipe_hash,
        spacy_library_version=SPACY_LIBRARY_VERSION,
        spacy_model=SPACY_MODEL,
        spacy_model_version=SPACY_MODEL_VERSION,
        parser_version=PARSER_VERSION,
        evidence_refs=bundle.evidence_refs,
        compilation=persisted,
    )


def validate_candidate_against_source(
    candidate: CompiledChildCandidateExportV1,
    *,
    corpus_id: str,
    document: Mapping[str, Any],
    child: Mapping[str, Any],
) -> None:
    """Revalidate a host-compiled candidate in the canonical image."""

    text = child.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ClaimInputError("current child text is missing")
    if candidate.corpus_id != corpus_id:
        raise ClaimInputError("candidate corpus does not match import corpus")
    if str(child.get("chunk_id") or "") != candidate.child_id:
        raise ClaimInputError("candidate child ID does not match source")
    if str(child.get("doc_id") or "") != candidate.document_id:
        raise ClaimInputError("candidate document ID does not match source")
    if str(document.get("doc_id") or "") != candidate.document_id:
        raise ClaimInputError("candidate document row does not match source")
    expected_source_version = document_source_version_id(document)
    if candidate.source_version_id != expected_source_version:
        raise ClaimInputError("candidate source version drifted")
    expected_text_hash = domain_hash("normalized-text", text)
    if candidate.source_text_hash != expected_text_hash:
        raise ClaimInputError("candidate source text hash drifted")
    expected_observation_recipe = semantic_observation_recipe_hash(
        parser_id=SPACY_MODEL,
        parser_version=PARSER_VERSION,
    )
    if candidate.observation_recipe_hash != expected_observation_recipe:
        raise ClaimInputError("candidate observation recipe drifted")
    if candidate.observation_bundle_id != _expected_observation_bundle_id(
        source_version_id_=expected_source_version,
        child_id=candidate.child_id,
        source_text_hash=expected_text_hash,
        observation_recipe_hash=expected_observation_recipe,
    ):
        raise ClaimInputError("candidate observation bundle identity drifted")
    if candidate.local_extraction_recipe_hash != local_extraction_recipe_hash():
        raise ClaimInputError("candidate local-extraction recipe drifted")
    normalization = load_normalization_identity()
    if candidate.normalization_registry_hash != normalization["hash"]:
        raise ClaimInputError("candidate normalization registry drifted")
    expected_compiler_recipe = claim_compiler_recipe_hash(expected_observation_recipe)
    if candidate.compiler_recipe_hash != expected_compiler_recipe:
        raise ClaimInputError("candidate compiler recipe drifted")
    for evidence in candidate.evidence_refs:
        if evidence.end > len(text):
            raise ClaimInputError("candidate evidence ends outside current child")
        expected = make_evidence_ref(
            text=text,
            start=evidence.start,
            end=evidence.end,
            source_version_id=expected_source_version,
            hierarchy_node_id=candidate.child_id,
        )
        if expected != evidence:
            raise ClaimInputError("candidate evidence does not round-trip exactly")


def materialize_candidate_row(
    candidate: CompiledChildCandidateExportV1,
    *,
    corpus_id: str,
    document: Mapping[str, Any],
    child: Mapping[str, Any],
    run_id: str,
    now: datetime | None = None,
    raw_artifact_ids: Sequence[str] = (),
    provenance_producer_kind: ProducerKind = "spacy",
    provenance_engine: str = "spacy",
    provenance_model_id: str | None = SPACY_MODEL,
    provenance_model_revision: str | None = SPACY_MODEL_VERSION,
) -> ClaimCompilationMaterializationRowV1:
    """Build one canonical-image-validated, noncanonical durable row."""

    validate_candidate_against_source(
        candidate,
        corpus_id=corpus_id,
        document=document,
        child=child,
    )
    if not run_id.strip():
        raise ClaimInputError("materialization run ID must be nonempty")
    if not provenance_engine.strip():
        raise ClaimInputError("materialization provenance engine must be nonempty")
    normalized_raw_artifact_ids = tuple(
        str(value).strip() for value in raw_artifact_ids
    )
    if any(not value for value in normalized_raw_artifact_ids) or len(
        normalized_raw_artifact_ids
    ) != len(set(normalized_raw_artifact_ids)):
        raise ClaimInputError("raw artifact lineage must be nonempty and unique")
    created_at = now or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        raise ClaimInputError("materialization timestamp must be timezone-aware")
    input_set_hash = namespace_hash(
        "input-set",
        {
            "corpus_id": corpus_id,
            "document_id": candidate.document_id,
            "source_version_id": candidate.source_version_id,
            "child_id": candidate.child_id,
            "source_text_hash": candidate.source_text_hash,
            "observation_bundle_id": candidate.observation_bundle_id,
        },
    )
    artifact_id = (
        "claim-input:"
        + namespace_hash(
            "logical-artifact",
            {
                "artifact_kind": COMPILATION_ARTIFACT_TYPE,
                "natural_keys": {
                    "corpus_id": corpus_id,
                    "source_version_id": candidate.source_version_id,
                    "child_id": candidate.child_id,
                    "compiler_version": candidate.compiler_version,
                },
            },
        ).split(":", 1)[1]
    )
    envelope = make_artifact_envelope(
        artifact_type=COMPILATION_ARTIFACT_TYPE,
        schema_id="polymath.claim_compilation.v1",
        schema_version="claim_compilation.v1",
        artifact_id=artifact_id,
        artifact_state="candidate",
        knowledge_status=None,
        ownership=ArtifactOwnership(
            corpus_id=corpus_id,
            doc_id=candidate.document_id,
            source_version_id=candidate.source_version_id,
            hierarchy_node_id=candidate.child_id,
        ),
        input_set_hash=input_set_hash,
        recipe_hash=candidate.compiler_recipe_hash,
        evidence_set_hash=evidence_set_hash(candidate.evidence_refs),
        registry_set_hash=candidate.normalization_registry_hash,
        provenance=ArtifactProvenance(
            work_id=work_id(
                COMPILATION_ARTIFACT_TYPE,
                input_set_hash,
                candidate.compiler_recipe_hash,
            ),
            attempt_id=None,
            raw_artifact_ids=normalized_raw_artifact_ids,
            producer_kind=provenance_producer_kind,
            engine=provenance_engine,
            model_id=provenance_model_id,
            model_revision=provenance_model_revision,
            prompt_id=None,
            prompt_hash=None,
            compiler_version=candidate.compiler_version,
            parser_version=candidate.parser_version,
            rule_pack_version=candidate.local_extraction_recipe_hash,
            run_id=run_id,
        ),
        validation=ArtifactValidation(
            contract_valid=True,
            evidence_valid=True,
            registry_valid=True,
            policy_valid=True,
            validator_version=MATERIALIZATION_VALIDATOR_VERSION,
            errors=(),
            warnings=(),
        ),
        lifecycle=ArtifactLifecycle(
            created_at=created_at,
            validated_at=created_at,
            activated_at=None,
            supersedes_revision_id=None,
            superseded_at=None,
        ),
        body=candidate.compilation,
    )
    return ClaimCompilationMaterializationRowV1(
        _id=envelope.artifact_revision_id,
        schema_version="semantic_digest_claim_compilation_row.v1",
        corpus_id=corpus_id,
        document_id=candidate.document_id,
        source_version_id=candidate.source_version_id,
        child_id=candidate.child_id,
        source_text_hash=candidate.source_text_hash,
        observation_bundle_id=candidate.observation_bundle_id,
        observation_recipe_hash=candidate.observation_recipe_hash,
        local_extraction_recipe_hash=candidate.local_extraction_recipe_hash,
        normalization_registry_hash=candidate.normalization_registry_hash,
        compiler_version=candidate.compiler_version,
        compiler_recipe_hash=candidate.compiler_recipe_hash,
        spacy_library_version=candidate.spacy_library_version,
        spacy_model=candidate.spacy_model,
        spacy_model_version=candidate.spacy_model_version,
        parser_version=candidate.parser_version,
        canonical_write=False,
        status="candidate",
        evidence_refs=candidate.evidence_refs,
        envelope=envelope,
    )


def validate_materialized_row_against_source(
    row: ClaimCompilationMaterializationRowV1,
    *,
    corpus_id: str,
    document: Mapping[str, Any],
    child: Mapping[str, Any],
) -> None:
    candidate = CompiledChildCandidateExportV1(
        schema_version="semantic_digest_claim_compilation_export.v1",
        corpus_id=row.corpus_id,
        document_id=row.document_id,
        source_version_id=row.source_version_id,
        child_id=row.child_id,
        source_text_hash=row.source_text_hash,
        observation_bundle_id=row.observation_bundle_id,
        observation_recipe_hash=row.observation_recipe_hash,
        local_extraction_recipe_hash=row.local_extraction_recipe_hash,
        normalization_registry_hash=row.normalization_registry_hash,
        compiler_version=row.compiler_version,
        compiler_recipe_hash=row.compiler_recipe_hash,
        spacy_library_version=row.spacy_library_version,
        spacy_model=row.spacy_model,
        spacy_model_version=row.spacy_model_version,
        parser_version=row.parser_version,
        evidence_refs=row.evidence_refs,
        compilation=row.envelope.body,
    )
    validate_candidate_against_source(
        candidate,
        corpus_id=corpus_id,
        document=document,
        child=child,
    )


def _safe_entity(entity: Any) -> PacketExtractionEntityV1 | None:
    if not isinstance(entity, Mapping):
        return None
    canonical_name = str(entity.get("canonical_name") or "").strip()
    entity_type = str(entity.get("entity_type") or "").strip()
    if not canonical_name or not entity_type:
        return None
    values: dict[str, Any] = {
        "canonical_name": canonical_name,
        "entity_type": entity_type,
        "query_aliases": sorted(
            {
                str(item).strip()
                for item in entity.get("query_aliases") or []
                if isinstance(item, str) and item.strip()
            }
        )[:8],
    }
    for field in ("surface_form", "object_kind", "definitional_phrase"):
        value = str(entity.get(field) or "").strip()
        if value:
            values[field] = value
    confidence = entity.get("confidence")
    if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
        values["confidence"] = float(confidence)
    return PacketExtractionEntityV1.model_validate(values)


def build_atomic_parent_packet(
    *,
    corpus_id: str,
    corpus_name: str,
    parent: Mapping[str, Any],
    compilation_rows: Mapping[str, ClaimCompilationMaterializationRowV1],
    extraction_rows: Sequence[Mapping[str, Any]],
    max_entities: int,
) -> AtomicParentPacketBuild:
    """Build one strict parent packet with no interim or whole-parent claim."""

    parent_id = str(parent.get("parent_id") or "").strip()
    document_id = str(parent.get("doc_id") or "").strip()
    parent_text = str(parent.get("text") or "").strip()
    if not parent_id or not document_id or not parent_text:
        raise ClaimInputError("parent identity or text is missing")
    if parent.get("validation_status") != "valid":
        raise ClaimInputError("parent validation status is not valid")
    eligibility = classify_parent_text_v2(parent_text)
    if not eligibility.eligible:
        raise ClaimInputError(f"parent is not B1 eligible: {eligibility.reason}")
    source_child_ids = sorted(
        {str(value).strip() for value in parent.get("child_ids") or [] if value}
    )
    if not source_child_ids:
        raise ClaimInputError("parent has no source child IDs")
    if set(compilation_rows) != set(source_child_ids):
        raise ClaimInputError("parent compilation rows do not close over child IDs")

    claims: list[ClaimRecordV1] = []
    links = []
    evidence: dict[str, PacketEvidenceSentenceV1] = {}
    compiler_recipe_hashes: set[str] = set()
    revision_ids: list[str] = []
    for child_id in source_child_ids:
        row = compilation_rows[child_id]
        if (
            row.corpus_id != corpus_id
            or row.document_id != document_id
            or row.child_id != child_id
        ):
            raise ClaimInputError("materialized compilation ownership drifted")
        compiler_recipe_hashes.add(row.compiler_recipe_hash)
        revision_ids.append(row.row_id)
        refs = {item.evidence_ref_id: item for item in row.evidence_refs}
        for claim in row.envelope.body.claims:
            claims.append(claim)
            for evidence_id in claim.evidence_sentence_ids:
                ref = refs[evidence_id]
                candidate = PacketEvidenceSentenceV1(
                    evidence_sentence_id=evidence_id,
                    child_id=child_id,
                    text=ref.quote,
                )
                existing = evidence.get(evidence_id)
                if existing is not None and existing != candidate:
                    raise ClaimInputError(
                        "evidence ID maps to conflicting source quotes"
                    )
                evidence[evidence_id] = candidate
        for link in row.envelope.body.links:
            links.append(link)
            for evidence_id in link.evidence_sentence_ids:
                ref = refs[evidence_id]
                candidate = PacketEvidenceSentenceV1(
                    evidence_sentence_id=evidence_id,
                    child_id=child_id,
                    text=ref.quote,
                )
                existing = evidence.get(evidence_id)
                if existing is not None and existing != candidate:
                    raise ClaimInputError(
                        "link evidence maps to conflicting source quotes"
                    )
                evidence[evidence_id] = candidate
    if len(compiler_recipe_hashes) != 1:
        raise ClaimInputError("parent child compiler recipes are inconsistent")
    if not claims:
        raise PacketNotReadyError("zero_atomic_claims")

    entities_by_key: dict[tuple[str, str, str], PacketExtractionEntityV1] = {}
    accepted_extraction_children: set[str] = set()
    for extraction in extraction_rows:
        if (
            extraction.get("status") != "ok"
            or extraction.get("schema_version") != "polymath.extract.v1"
        ):
            continue
        child_id = str(extraction.get("chunk_id") or "").strip()
        if child_id:
            accepted_extraction_children.add(child_id)
        for raw in extraction.get("entities") or []:
            entity = _safe_entity(raw)
            if entity is None:
                continue
            key = (
                entity.canonical_name.casefold(),
                entity.entity_type.casefold(),
                str(entity.surface_form or "").casefold(),
            )
            entities_by_key.setdefault(key, entity)
    if not accepted_extraction_children:
        raise PacketNotReadyError("no_accepted_extraction_child")
    entities = [entities_by_key[key] for key in sorted(entities_by_key)][:max_entities]
    if not entities:
        raise PacketNotReadyError("no_accepted_extraction_entity")

    claim_models = sorted(
        (PacketAtomicClaimV1.from_claim_record(item) for item in claims),
        key=lambda item: item.claim_id,
    )
    link_models = sorted(links, key=lambda item: item.link_id)
    evidence_models = [evidence[key] for key in sorted(evidence)]
    compiler_recipe_hash = next(iter(compiler_recipe_hashes))
    packet = SemanticParentPacketAtomicClaimsV1(
        packet_schema_version=ATOMIC_PACKET_SCHEMA_VERSION,
        corpus_id=corpus_id,
        corpus_name=corpus_name,
        doc_id=document_id,
        parent_id=parent_id,
        parent_text=parent_text,
        claims=claim_models,
        evidence_sentences=evidence_models,
        claim_links=link_models,
        extraction_entities=entities,
        evidence_contract=PacketEvidenceContractV1(
            eligibility_schema_version="semantic_parent_eligibility.v2",
            eligibility_recipe_hash=parent_eligibility_recipe_hash(),
            packet_schema_version=ATOMIC_PACKET_SCHEMA_VERSION,
            claim_record_schema_hash=namespace_hash(
                "schema", ClaimRecordV1.model_json_schema()
            ),
            claim_compilation_schema_hash=namespace_hash(
                "schema", ClaimCompilationV1.model_json_schema()
            ),
            compiler_version=COMPILER_VERSION,
            compiler_recipe_hash=compiler_recipe_hash,
            parser_id=SPACY_MODEL,
            parser_version=PARSER_VERSION,
            source_child_ids=source_child_ids,
            source_compilation_revision_ids=sorted(revision_ids),
            claims_interim=False,
            relation_disposition="relations_remain_observation_only",
        ),
    )
    context = SemanticValidationContext.from_owner_registries(
        parent_id=parent_id,
        claims=tuple(ClaimScope(item.claim_id, parent_id) for item in claim_models),
        claim_grounded_mode=True,
    )
    return AtomicParentPacketBuild(
        packet=packet,
        context=context,
        parent_id=parent_id,
        doc_id=document_id,
        entity_count=len(entities),
        source_child_count=len(source_child_ids),
        claim_count=len(claim_models),
        link_count=len(link_models),
        evidence_count=len(evidence_models),
    )


def _ordered_parent_child_ids(parent: Mapping[str, Any]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for raw in parent.get("child_ids") or []:
        child_id = str(raw).strip()
        if child_id and child_id not in seen:
            seen.add(child_id)
            ordered.append(child_id)
    return ordered


def build_sentence_hybrid_parent_packet(
    *,
    corpus_id: str,
    corpus_name: str,
    parent: Mapping[str, Any],
    compilation_rows: Mapping[str, ClaimCompilationMaterializationRowV1],
    extraction_rows: Sequence[Mapping[str, Any]],
    max_entities: int,
) -> SentenceHybridParentPacketBuild:
    """Build the approved ordered-unit v3 packet without provider side effects."""

    source_build = build_atomic_parent_packet(
        corpus_id=corpus_id,
        corpus_name=corpus_name,
        parent=parent,
        compilation_rows=compilation_rows,
        extraction_rows=extraction_rows,
        max_entities=max_entities,
    )
    source_child_ids = _ordered_parent_child_ids(parent)
    if set(source_child_ids) != set(
        source_build.packet.evidence_contract.source_child_ids
    ):
        raise ClaimInputError("sentence-hybrid source child order does not close")
    if any(
        not compilation_rows[child_id].envelope.body.claims
        for child_id in source_child_ids
    ):
        raise PacketNotReadyError("source_child_without_atomic_claim")

    sentence_units: list[PacketCitableSentenceUnitV3 | PacketSentenceUnitV3] = []
    ordered_evidence_ids: list[str] = []
    expansions: list[SentenceAtomicExpansionV3] = []
    seen_evidence_ids: set[str] = set()
    revision_ids: list[str] = []
    for child_id in source_child_ids:
        row = compilation_rows[child_id]
        revision_ids.append(row.row_id)
        atomic_by_sentence: dict[str, set[str]] = {}
        for claim in row.envelope.body.claims:
            for evidence_id in claim.evidence_sentence_ids:
                atomic_by_sentence.setdefault(evidence_id, set()).add(claim.claim_id)

        child_evidence_ids: set[str] = set()
        for ref in sorted(
            row.evidence_refs,
            key=lambda item: (item.start, item.end, item.evidence_ref_id),
        ):
            if ref.evidence_ref_id in seen_evidence_ids:
                raise ClaimInputError(
                    "sentence-hybrid evidence ID is duplicated across source order"
                )
            seen_evidence_ids.add(ref.evidence_ref_id)
            child_evidence_ids.add(ref.evidence_ref_id)
            ordered_evidence_ids.append(ref.evidence_ref_id)
            atomic_claim_ids = tuple(
                sorted(atomic_by_sentence.get(ref.evidence_ref_id, set()))
            )
            if atomic_claim_ids:
                sentence_units.append(
                    PacketCitableSentenceUnitV3(
                        claim_id=ref.evidence_ref_id,
                        text=ref.quote,
                    )
                )
                expansions.append(
                    SentenceAtomicExpansionV3(
                        sentence_claim_id=ref.evidence_ref_id,
                        parent_id=source_build.parent_id,
                        child_id=child_id,
                        source_version_id=row.source_version_id,
                        source_compilation_revision_id=row.row_id,
                        atomic_claim_ids=atomic_claim_ids,
                    )
                )
            else:
                sentence_units.append(PacketSentenceUnitV3(text=ref.quote))
        if set(atomic_by_sentence) - child_evidence_ids:
            raise ClaimInputError(
                "sentence-hybrid atomic mapping references absent child evidence"
            )

    if not sentence_units:
        raise PacketNotReadyError("zero_source_sentences")
    mapped_count = len(expansions)
    sentence_counts = PacketSentenceCountsV3(
        mapped=mapped_count,
        unmapped=len(sentence_units) - mapped_count,
    )
    evidence_contract = PacketEvidenceContractV3(
        claims_interim=True,
        sentence_order="parent_child_order_then_source_offset",
        context_only_units_uncitable=True,
        post_validation_mapping="sentence_claim_id_to_local_atomic_claim_ids",
    )
    packet_values = {
        "packet_schema_version": SENTENCE_HYBRID_PACKET_SCHEMA_VERSION,
        "corpus_id": source_build.packet.corpus_id,
        "corpus_name": source_build.packet.corpus_name,
        "doc_id": source_build.packet.doc_id,
        "parent_id": source_build.packet.parent_id,
        "sentence_units": [item.model_dump(mode="python") for item in sentence_units],
        "extraction_entities": [
            item.model_dump(
                mode="python",
                exclude_none=True,
                exclude_defaults=True,
            )
            for item in source_build.packet.extraction_entities
        ],
        "sentence_counts": sentence_counts.model_dump(mode="python"),
        "evidence_contract": evidence_contract.model_dump(mode="python"),
    }
    packet_utf8_bytes = len(canonical_json_v1(packet_values).encode("utf-8"))
    if packet_utf8_bytes > SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES:
        raise PacketNotReadyError(
            "sentence_hybrid_packet_exceeds_byte_bound",
            packet_utf8_bytes=packet_utf8_bytes,
            max_packet_utf8_bytes=SENTENCE_HYBRID_PACKET_MAX_UTF8_BYTES,
        )
    packet = SemanticParentPacketSentenceHybridV3.model_validate(packet_values)
    context = SemanticValidationContext.from_owner_registries(
        parent_id=source_build.parent_id,
        claims=tuple(
            ClaimScope(item.sentence_claim_id, source_build.parent_id)
            for item in expansions
        ),
        claim_grounded_mode=True,
    )
    source_compilation_set_hash = namespace_hash("input-set", frozenset(revision_ids))
    return SentenceHybridParentPacketBuild(
        packet=packet,
        context=context,
        parent_id=source_build.parent_id,
        doc_id=source_build.doc_id,
        entity_count=source_build.entity_count,
        source_child_ids=tuple(source_child_ids),
        source_compilation_revision_ids=tuple(revision_ids),
        ordered_evidence_sentence_ids=tuple(ordered_evidence_ids),
        sentence_atomic_expansions=tuple(expansions),
        source_child_order_hash=namespace_hash(
            "work",
            {
                "work_kind": "sentence_hybrid_source_child_order.v3",
                "parent_id": source_build.parent_id,
                "source_child_ids": source_child_ids,
            },
        ),
        source_compilation_set_hash=source_compilation_set_hash,
        sentence_order_hash=namespace_hash(
            "work",
            {
                "work_kind": "sentence_hybrid_sentence_order.v3",
                "parent_id": source_build.parent_id,
                "ordered_evidence_sentence_ids": ordered_evidence_ids,
            },
        ),
        packet_utf8_bytes=packet_utf8_bytes,
    )


def expand_sentence_claim_ids(
    build: SentenceHybridParentPacketBuild,
    sentence_claim_ids: Sequence[str],
    *,
    expected_parent_id: str,
    expected_source_compilation_revision_ids: Sequence[str],
) -> ExpandedSentenceClaimsV3:
    """Expand direct sentence citations without asserting unique model intent."""

    if expected_parent_id != build.parent_id:
        raise ClaimInputError("sentence expansion parent closure drifted")
    expected_revisions = tuple(expected_source_compilation_revision_ids)
    if len(expected_revisions) != len(set(expected_revisions)):
        raise ClaimInputError("sentence expansion revisions are duplicated")
    if namespace_hash("input-set", frozenset(expected_revisions)) != (
        build.source_compilation_set_hash
    ):
        raise ClaimInputError("sentence expansion compilation revisions are stale")

    revision_by_child = dict(
        zip(
            build.source_child_ids,
            build.source_compilation_revision_ids,
            strict=True,
        )
    )
    expansion_by_sentence: dict[str, SentenceAtomicExpansionV3] = {}
    for row in build.sentence_atomic_expansions:
        if row.parent_id != build.parent_id:
            raise ClaimInputError("sentence expansion mapping crosses parent closure")
        if revision_by_child.get(row.child_id) != row.source_compilation_revision_id:
            raise ClaimInputError("sentence expansion mapping crosses child closure")
        if not row.source_version_id:
            raise ClaimInputError("sentence expansion mapping lacks source version")
        if not row.atomic_claim_ids or row.atomic_claim_ids != tuple(
            sorted(set(row.atomic_claim_ids))
        ):
            raise ClaimInputError(
                "sentence expansion atomic mapping is empty or unstable"
            )
        if row.sentence_claim_id in expansion_by_sentence:
            raise ClaimInputError("sentence expansion mapping is duplicated")
        expansion_by_sentence[row.sentence_claim_id] = row

    direct_ids = tuple(sentence_claim_ids)
    if len(direct_ids) != len(set(direct_ids)):
        raise ClaimInputError("sentence citations are duplicated")
    selected: list[SentenceAtomicExpansionV3] = []
    for sentence_id in direct_ids:
        row = expansion_by_sentence.get(sentence_id)
        if row is None:
            raise ClaimInputError(
                "sentence citation is context-only or outside closure"
            )
        selected.append(row)
    atomic_ids = tuple(
        sorted(
            {
                atomic_claim_id
                for row in selected
                for atomic_claim_id in row.atomic_claim_ids
            }
        )
    )
    return ExpandedSentenceClaimsV3(
        sentence_claim_ids=direct_ids,
        supporting_atomic_claim_ids=atomic_ids,
        mapping_cardinalities=tuple(len(row.atomic_claim_ids) for row in selected),
        source_compilation_set_hash=build.source_compilation_set_hash,
    )


def _claim_is_nuanced(claim: ClaimRecordV1) -> bool:
    return bool(
        claim.conditions
        or claim.exceptions
        or claim.temporal_cues
        or claim.modality != "asserted"
    )


def _selection_priority(claim: ClaimRecordV1) -> tuple[int, int, int, str, str]:
    return (
        0 if claim.typing_status == "typed" else 1,
        0 if claim.polarity == "negative" else 1,
        0 if _claim_is_nuanced(claim) else 1,
        claim.claim_type,
        claim.claim_id,
    )


def _round_robin_claims(claims: Sequence[ClaimRecordV1]) -> list[ClaimRecordV1]:
    by_child: dict[str, list[ClaimRecordV1]] = {}
    for claim in claims:
        by_child.setdefault(claim.child_id, []).append(claim)
    for values in by_child.values():
        values.sort(key=lambda item: (item.claim_type, item.claim_id))
    ordered: list[ClaimRecordV1] = []
    index = 0
    while True:
        emitted = False
        for child_id in sorted(by_child):
            values = by_child[child_id]
            if index < len(values):
                emitted = True
                ordered.append(values[index])
        if not emitted:
            return ordered
        index += 1


def _selection_lane_accounting(
    source: Sequence[ClaimRecordV1],
    emitted_ids: set[str],
    predicate: Any,
) -> SelectionLaneAccountingV2:
    source_ids = {item.claim_id for item in source if predicate(item)}
    emitted_count = len(source_ids & emitted_ids)
    return SelectionLaneAccountingV2(
        source_count=len(source_ids),
        emitted_count=emitted_count,
        excluded_count=len(source_ids) - emitted_count,
    )


def _bounded_packet_values(
    *,
    source_build: AtomicParentPacketBuild,
    source_claims: Sequence[ClaimRecordV1],
    selected_claims: Sequence[ClaimRecordV1],
    recipe_hash: str,
) -> dict[str, Any]:
    source_claim_ids = {item.claim_id for item in source_claims}
    emitted_claim_ids = {item.claim_id for item in selected_claims}
    excluded_claim_ids = source_claim_ids - emitted_claim_ids
    source_child_ids = set(source_build.packet.evidence_contract.source_child_ids)
    covered_child_ids = {item.child_id for item in selected_claims}
    source_links = source_build.packet.claim_links
    emitted_links = [
        PacketClaimLinkV2.from_claim_link(link)
        for link in source_links
        if {link.source_claim_id, link.target_claim_id} <= emitted_claim_ids
    ]
    source_link_ids = {item.link_id for item in source_links}
    emitted_link_ids = {item.link_id for item in emitted_links}
    excluded_link_ids = source_link_ids - emitted_link_ids
    manifest = PacketSelectionManifestV2(
        recipe_version=BOUNDED_SELECTION_RECIPE_VERSION,
        recipe_hash=recipe_hash,
        max_packet_utf8_bytes=BOUNDED_PACKET_MAX_UTF8_BYTES,
        source_claim_count=len(source_claim_ids),
        emitted_claim_count=len(emitted_claim_ids),
        excluded_claim_count=len(excluded_claim_ids),
        source_claim_set_hash=namespace_hash("input-set", frozenset(source_claim_ids)),
        emitted_claim_set_hash=namespace_hash(
            "input-set", frozenset(emitted_claim_ids)
        ),
        excluded_claim_set_hash=namespace_hash(
            "input-set", frozenset(excluded_claim_ids)
        ),
        source_child_count=len(source_child_ids),
        covered_source_child_count=len(covered_child_ids),
        source_child_set_hash=namespace_hash("input-set", frozenset(source_child_ids)),
        covered_source_child_set_hash=namespace_hash(
            "input-set", frozenset(covered_child_ids)
        ),
        source_claim_link_count=len(source_link_ids),
        emitted_claim_link_count=len(emitted_link_ids),
        excluded_claim_link_count=len(excluded_link_ids),
        source_claim_link_set_hash=namespace_hash(
            "input-set", frozenset(source_link_ids)
        ),
        emitted_claim_link_set_hash=namespace_hash(
            "input-set", frozenset(emitted_link_ids)
        ),
        excluded_claim_link_set_hash=namespace_hash(
            "input-set", frozenset(excluded_link_ids)
        ),
        typed=_selection_lane_accounting(
            source_claims,
            emitted_claim_ids,
            lambda item: item.typing_status == "typed",
        ),
        negative=_selection_lane_accounting(
            source_claims,
            emitted_claim_ids,
            lambda item: item.polarity == "negative",
        ),
        nuanced=_selection_lane_accounting(
            source_claims,
            emitted_claim_ids,
            _claim_is_nuanced,
        ),
        ordinary=_selection_lane_accounting(
            source_claims,
            emitted_claim_ids,
            lambda item: (
                item.typing_status != "typed"
                and item.polarity != "negative"
                and not _claim_is_nuanced(item)
            ),
        ),
        cap_applied=bool(excluded_claim_ids),
        proposal_space_disposition=(
            "bounded_to_emitted_claims_excluded_claims_remain_local"
        ),
    )
    source_contract = source_build.packet.evidence_contract
    evidence_contract = PacketEvidenceContractV2(
        eligibility_schema_version=source_contract.eligibility_schema_version,
        eligibility_recipe_hash=source_contract.eligibility_recipe_hash,
        packet_schema_version=BOUNDED_ATOMIC_PACKET_SCHEMA_VERSION,
        claim_record_schema_hash=source_contract.claim_record_schema_hash,
        claim_compilation_schema_hash=(source_contract.claim_compilation_schema_hash),
        compiler_version=source_contract.compiler_version,
        compiler_recipe_hash=source_contract.compiler_recipe_hash,
        parser_id=source_contract.parser_id,
        parser_version=source_contract.parser_version,
        source_child_set_hash=manifest.source_child_set_hash,
        source_compilation_set_hash=namespace_hash(
            "input-set",
            frozenset(source_contract.source_compilation_revision_ids),
        ),
        claims_interim=False,
        relation_disposition=source_contract.relation_disposition,
    )
    return {
        "packet_schema_version": BOUNDED_ATOMIC_PACKET_SCHEMA_VERSION,
        "corpus_id": source_build.packet.corpus_id,
        "corpus_name": source_build.packet.corpus_name,
        "doc_id": source_build.packet.doc_id,
        "parent_id": source_build.packet.parent_id,
        "claims": [
            PacketAtomicClaimV2.from_claim_record(item).model_dump(mode="python")
            for item in sorted(selected_claims, key=lambda item: item.claim_id)
        ],
        "claim_links": [
            item.model_dump(mode="python")
            for item in sorted(emitted_links, key=lambda item: item.link_id)
        ],
        "extraction_entities": [
            item.model_dump(mode="python")
            for item in source_build.packet.extraction_entities
        ],
        "evidence_contract": evidence_contract.model_dump(mode="python"),
        "selection_manifest": manifest.model_dump(mode="python"),
    }


def build_bounded_atomic_parent_packet(
    *,
    corpus_id: str,
    corpus_name: str,
    parent: Mapping[str, Any],
    compilation_rows: Mapping[str, ClaimCompilationMaterializationRowV1],
    extraction_rows: Sequence[Mapping[str, Any]],
    max_entities: int,
) -> BoundedAtomicParentPacketBuild:
    """Project fully validated atomic claims into a bounded provider packet."""

    source_build = build_atomic_parent_packet(
        corpus_id=corpus_id,
        corpus_name=corpus_name,
        parent=parent,
        compilation_rows=compilation_rows,
        extraction_rows=extraction_rows,
        max_entities=max_entities,
    )
    recipe, recipe_hash = load_bounded_selection_recipe()
    if recipe.max_packet_utf8_bytes != BOUNDED_PACKET_MAX_UTF8_BYTES:
        raise ClaimInputError("bounded packet recipe maximum drifted")

    source_claims = sorted(
        (
            claim
            for child_id in sorted(compilation_rows)
            for claim in compilation_rows[child_id].envelope.body.claims
        ),
        key=lambda item: item.claim_id,
    )
    source_claim_ids = [item.claim_id for item in source_claims]
    if len(source_claim_ids) != len(set(source_claim_ids)):
        raise ClaimInputError("source claim IDs are duplicated across children")
    if set(source_claim_ids) != {item.claim_id for item in source_build.packet.claims}:
        raise ClaimInputError("source records do not equal validated v1 claims")
    source_child_ids = set(source_build.packet.evidence_contract.source_child_ids)
    claims_by_child: dict[str, list[ClaimRecordV1]] = {
        child_id: [] for child_id in source_child_ids
    }
    for claim in source_claims:
        claims_by_child[claim.child_id].append(claim)
    if any(not values for values in claims_by_child.values()):
        raise PacketNotReadyError("source_child_without_atomic_claim")

    selected = [
        min(claims_by_child[child_id], key=_selection_priority)
        for child_id in sorted(claims_by_child)
    ]
    selected_ids = {item.claim_id for item in selected}
    byte_rejections: dict[str, list[int]] = {}
    seed_values = _bounded_packet_values(
        source_build=source_build,
        source_claims=source_claims,
        selected_claims=selected,
        recipe_hash=recipe_hash,
    )
    if (
        len(canonical_json_v1(seed_values).encode("utf-8"))
        > recipe.max_packet_utf8_bytes
    ):
        raise PacketNotReadyError("source_child_coverage_seed_exceeds_byte_bound")

    priority_lanes = (
        [item for item in source_claims if item.typing_status == "typed"],
        [item for item in source_claims if item.polarity == "negative"],
        [item for item in source_claims if _claim_is_nuanced(item)],
        source_claims,
    )
    for lane in priority_lanes:
        for claim in _round_robin_claims(lane):
            if claim.claim_id in selected_ids:
                continue
            candidate = [*selected, claim]
            candidate_values = _bounded_packet_values(
                source_build=source_build,
                source_claims=source_claims,
                selected_claims=candidate,
                recipe_hash=recipe_hash,
            )
            candidate_size = len(canonical_json_v1(candidate_values).encode("utf-8"))
            if candidate_size <= recipe.max_packet_utf8_bytes:
                selected = candidate
                selected_ids.add(claim.claim_id)
            else:
                byte_rejections.setdefault(claim.claim_id, []).append(candidate_size)

    values = _bounded_packet_values(
        source_build=source_build,
        source_claims=source_claims,
        selected_claims=selected,
        recipe_hash=recipe_hash,
    )
    packet = SemanticParentPacketAtomicClaimsV2.model_validate(values)
    emitted_records = tuple(
        item for item in source_claims if item.claim_id in selected_ids
    )
    excluded_records = tuple(
        item for item in source_claims if item.claim_id not in selected_ids
    )
    excluded_claim_ids = {item.claim_id for item in excluded_records}
    if set(byte_rejections) != excluded_claim_ids:
        raise ClaimInputError("bounded packet byte-exclusion accounting drifted")
    exclusion_decisions = tuple(
        BoundedClaimByteExclusion(
            claim_id=claim_id,
            first_attempted_packet_utf8_bytes=sizes[0],
            last_attempted_packet_utf8_bytes=sizes[-1],
            rejection_attempt_count=len(sizes),
            max_packet_utf8_bytes=recipe.max_packet_utf8_bytes,
        )
        for claim_id, sizes in sorted(byte_rejections.items())
    )
    if any(
        decision.first_attempted_packet_utf8_bytes <= recipe.max_packet_utf8_bytes
        or decision.last_attempted_packet_utf8_bytes <= recipe.max_packet_utf8_bytes
        for decision in exclusion_decisions
    ):
        raise ClaimInputError("bounded packet exclusion did not exceed byte bound")
    if {item.child_id for item in emitted_records} != source_child_ids:
        raise ClaimInputError("bounded packet lost source-child coverage")
    context = SemanticValidationContext.from_owner_registries(
        parent_id=source_build.parent_id,
        claims=tuple(
            ClaimScope(item.claim_id, source_build.parent_id) for item in packet.claims
        ),
        claim_grounded_mode=True,
    )
    manifest = packet.selection_manifest
    return BoundedAtomicParentPacketBuild(
        packet=packet,
        context=context,
        parent_id=source_build.parent_id,
        doc_id=source_build.doc_id,
        entity_count=source_build.entity_count,
        source_child_count=manifest.source_child_count,
        source_claim_count=manifest.source_claim_count,
        emitted_claim_count=manifest.emitted_claim_count,
        excluded_claim_count=manifest.excluded_claim_count,
        source_link_count=manifest.source_claim_link_count,
        emitted_link_count=manifest.emitted_claim_link_count,
        excluded_link_count=manifest.excluded_claim_link_count,
        emitted_claim_records=emitted_records,
        excluded_claim_records=excluded_records,
        excluded_claim_byte_decisions=exclusion_decisions,
    )
