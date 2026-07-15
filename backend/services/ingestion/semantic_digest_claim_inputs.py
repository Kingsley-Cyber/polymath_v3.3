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
    make_artifact_envelope,
)
from models.claim_record import ClaimCompilationV1, ClaimRecordV1
from models.hash_taxonomy import canonical_json_v1, namespace_hash
from models.identifier_recipes import source_version_id, work_id
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
    PacketSelectionManifestV2,
    SelectionLaneAccountingV2,
    SemanticParentPacketAtomicClaimsV1,
    SemanticParentPacketAtomicClaimsV2,
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

    def __init__(self, reason: str) -> None:
        self.reason = reason
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
            raw_artifact_ids=(),
            producer_kind="spacy",
            engine="spacy",
            model_id=SPACY_MODEL,
            model_revision=SPACY_MODEL_VERSION,
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
