"""Deterministic T9.1 document-profile compiler.

This module is side-effect free.  It consumes only already-durable source
artifacts and produces a candidate serving profile with exact source/hash
closure.  Provider calls and accepted-state writes are structurally absent.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

from models.claim_record import ClaimCompilationV1
from models.document_semantic_profile import (
    PROFILE_SCHEMA_VERSION,
    ProfileConceptEvidenceV1,
    ProfileDomainEvidenceV1,
    ProfileFrameEvidenceV1,
    ProfileMotifEvidenceV1,
    ProfileRegistryClosureV1,
    ProfileSourceSliceV1,
    T91DocumentProfileV1,
    profile_logical_hash,
)
from models.frame_motif import FrameSequenceItemV1
from models.hash_taxonomy import namespace_hash
from models.local_extraction import LocalExtractionV1
from models.registry_loader import load_all
from models.semantic_resolution import DomainSignalV1
from services.ingestion.corpus_lexicon import normalize_identity
from services.ingestion.frame_motif import compile_frame_instance, match_motifs
from services.ingestion.semantic_digest_claim_inputs import (
    document_source_version_id,
)
from services.ingestion.semantic_resolution import (
    resolve_domains,
    resolve_superframe_rule,
)

PROFILE_COMPILER_VERSION = "deterministic_t91_document_profile_compiler.v1"
MAX_CONCEPT_TERMS = 512
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class DocumentProfileCompilationError(ValueError):
    """A source identity or typed extraction invariant failed closed."""


def current_registry_closure() -> ProfileRegistryClosureV1:
    registries = load_all()
    return ProfileRegistryClosureV1(
        domain_registry_hash=namespace_hash("registry", registries["domain"]),
        domain_resolution_policy_hash=namespace_hash(
            "registry", registries["domain_resolution"]
        ),
        superframe_registry_hash=namespace_hash("registry", registries["superframe"]),
        superframe_rule_registry_hash=namespace_hash(
            "registry", registries["superframe_rule"]
        ),
        frame_role_binding_policy_hash=namespace_hash(
            "registry", registries["frame_role_binding"]
        ),
        motif_registry_hash=namespace_hash("registry", registries["motif"]),
        motif_stage_binding_hash=namespace_hash("registry", registries["binding"]),
        motif_matching_policy_hash=namespace_hash(
            "registry", registries["motif_matching"]
        ),
    )


def current_profile_recipe_hash(
    closure: ProfileRegistryClosureV1 | None = None,
) -> str:
    closure = closure or current_registry_closure()
    return namespace_hash(
        "recipe",
        {
            "compiler": PROFILE_COMPILER_VERSION,
            "schema_version": PROFILE_SCHEMA_VERSION,
            "registry_closure": closure.model_dump(mode="python"),
            "domain_resolution": "T9.1 exact registry resolver",
            "superframe_resolution": "T9.1 predicate rule resolver",
            "frame_binding": "T9.2 explicit role binding",
            "motif_matching": "T9.2 strict sequence and exact role threading",
            "concept_normalization": (
                "corpus_lexicon.normalize_identity.v1 + "
                "query_semantics.CONCEPT_STOP_WORDS"
            ),
            "concept_term_cap": MAX_CONCEPT_TERMS,
            "provider_calls": 0,
        },
    )


def _canonical_source_hash(document: Mapping[str, Any]) -> str:
    identity = document.get("source_identity") or {}
    value = str(identity.get("content_sha256") or "").strip().lower()
    if value.startswith("sha256:"):
        value = value[7:]
    if not _HEX64.fullmatch(value):
        raise DocumentProfileCompilationError(
            "document source content hash is not canonical SHA-256"
        )
    return f"sha256:{value}"


def _source_key(document: Mapping[str, Any], expected_hash: str) -> str:
    identity = document.get("source_identity") or {}
    value = str(
        document.get("source_key") or identity.get("source_key") or expected_hash
    ).strip()
    if not value.startswith("sha256:") and _HEX64.fullmatch(value.lower()):
        value = "sha256:" + value.lower()
    if value != expected_hash:
        raise DocumentProfileCompilationError(
            "document source key/content hash closure drifted"
        )
    return value


def _artifact_id(kind: str, natural_keys: dict[str, Any]) -> str:
    digest = namespace_hash(
        "logical-artifact",
        {"artifact_kind": kind, "natural_keys": natural_keys},
    )
    return f"{kind}:{digest.split(':', 1)[1]}"


def _selected_document_payload(document: Mapping[str, Any]) -> dict[str, Any]:
    profile = document.get("doc_profile") or {}
    identity = document.get("source_identity") or {}
    return {
        "corpus_id": str(document.get("corpus_id") or ""),
        "doc_id": str(document.get("doc_id") or ""),
        "title": str(
            document.get("original_filename")
            or document.get("filename")
            or document.get("title")
            or profile.get("title")
            or ""
        ),
        "source_identity": {
            "content_sha256": str(identity.get("content_sha256") or ""),
            "source_key": str(
                document.get("source_key") or identity.get("source_key") or ""
            ),
        },
        "doc_profile": {
            "summary": str(profile.get("summary") or ""),
            "concepts": sorted(
                {str(value) for value in profile.get("concepts") or [] if value}
            ),
            "section_ids": sorted(
                {str(value) for value in profile.get("section_ids") or [] if value}
            ),
        },
    }


def _selected_parent_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "parent_id": str(row.get("parent_id") or ""),
        "heading_path": [
            str(value) for value in row.get("heading_path") or [] if value
        ],
        "summary": str(row.get("summary") or ""),
    }


def _selected_extraction_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    stage = row.get("stage_identity") or {}
    return {
        "chunk_id": str(row.get("chunk_id") or ""),
        "source_version_id": str(row.get("source_version_id") or ""),
        "stage_identity": {
            "identity_version": str(stage.get("identity_version") or ""),
            "source_file_hash": str(stage.get("source_file_hash") or ""),
            "source_key": str(stage.get("source_key") or ""),
            "chunk_hash": str(stage.get("chunk_hash") or ""),
            "extraction_contract_hash": str(
                stage.get("extraction_contract_hash") or ""
            ),
        },
        "document_ordinal": int(row.get("_document_ordinal") or 0),
        "local_extraction": row.get("local_extraction"),
        "claim_compilation": row.get("claim_compilation"),
    }


def _source_slice(
    source_kind: str,
    rows: list[tuple[str, dict[str, Any]]],
) -> ProfileSourceSliceV1:
    artifact_ids = sorted(item[0] for item in rows)
    return ProfileSourceSliceV1(
        source_kind=source_kind,
        row_count=len(rows),
        artifact_ids=artifact_ids,
        slice_hash=namespace_hash(
            "input-set",
            sorted(
                (
                    {"artifact_id": artifact_id, "payload": payload}
                    for artifact_id, payload in rows
                ),
                key=lambda item: item["artifact_id"],
            ),
        ),
    )


def _concept_surfaces(value: str) -> list[str]:
    # Lazy import avoids making the side-effect-free ingestion compiler depend
    # on retriever package initialization at module import time.
    from services.retriever.query_semantics import (
        CONCEPT_STOP_WORDS,
        GENERIC_CONCEPT_TOKENS,
        query_tokens,
    )

    normalized = normalize_identity(value)
    if not normalized:
        return []
    tokens = query_tokens(normalized, stop_words=CONCEPT_STOP_WORDS)
    tokens = [token for token in tokens if token not in GENERIC_CONCEPT_TOKENS]
    values = set(tokens)
    if len(tokens) > 1:
        values.add(" ".join(tokens))
    return sorted(values)


def _thread_key(
    *,
    corpus_id: str,
    doc_id: str,
    argument: Any,
    entity_labels: Mapping[str, str],
) -> str:
    if argument.filler_kind == "entity_mention":
        label = normalize_identity(entity_labels[argument.filler_ref])
        return _artifact_id(
            "profile-entity-thread",
            {"corpus_id": corpus_id, "doc_id": doc_id, "label": label},
        )
    return _artifact_id(
        "profile-span-thread",
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "span_observation_id": argument.span_observation_id,
        },
    )


def _validate_row_ownership(
    row: Mapping[str, Any],
    *,
    corpus_id: str,
    doc_id: str,
    source_hash: str,
    source_version_id: str,
) -> None:
    if str(row.get("corpus_id") or "") != corpus_id:
        raise DocumentProfileCompilationError("extraction corpus ownership drifted")
    if str(row.get("doc_id") or row.get("document_id") or "") != doc_id:
        raise DocumentProfileCompilationError("extraction document ownership drifted")
    stage = row.get("stage_identity") or {}
    if str(stage.get("identity_version") or "") != "stage_identity.v1":
        raise DocumentProfileCompilationError(
            "extraction stage identity is missing or noncanonical"
        )
    stage_hash = str(stage.get("source_file_hash") or "").lower()
    if stage_hash.startswith("sha256:"):
        stage_hash = stage_hash[7:]
    if not stage_hash or f"sha256:{stage_hash}" != source_hash:
        raise DocumentProfileCompilationError(
            "extraction source identity is stale for current document"
        )
    stage_key = str(stage.get("source_key") or "")
    if stage_key != source_hash:
        raise DocumentProfileCompilationError(
            "extraction source key is stale for current document"
        )
    row_source_version = str(row.get("source_version_id") or "")
    if row_source_version and row_source_version != source_version_id:
        raise DocumentProfileCompilationError(
            "extraction source version is stale for current document"
        )


def _finalize_profile(payload: dict[str, Any]) -> T91DocumentProfileV1:
    body_hash = namespace_hash("body", payload)
    logical_hash = profile_logical_hash(
        corpus_id=payload["corpus_id"],
        doc_id=payload["doc_id"],
        source_version_id=payload["source_version_id"],
    )
    profile_hash = namespace_hash(
        "revision",
        {
            "logical_artifact_hash": logical_hash,
            "body_hash": body_hash,
            "supersedes": None,
        },
    )
    return T91DocumentProfileV1.model_validate(
        {**payload, "body_hash": body_hash, "profile_hash": profile_hash}
    )


def compile_document_profile(
    *,
    document: Mapping[str, Any],
    parent_rows: Iterable[Mapping[str, Any]],
    extraction_rows: Iterable[Mapping[str, Any]],
) -> T91DocumentProfileV1:
    """Compile one provenance-closed candidate profile without side effects."""

    corpus_id = str(document.get("corpus_id") or "")
    doc_id = str(document.get("doc_id") or "")
    if not corpus_id or not doc_id:
        raise DocumentProfileCompilationError(
            "document profile requires corpus and document IDs"
        )
    source_hash = _canonical_source_hash(document)
    source_key = _source_key(document, source_hash)
    source_version = document_source_version_id(document)
    parents = sorted(
        (dict(row) for row in parent_rows),
        key=lambda row: str(row.get("parent_id") or ""),
    )
    extractions = sorted(
        (dict(row) for row in extraction_rows),
        key=lambda row: (
            int(row.get("_document_ordinal") or 0),
            str(row.get("chunk_id") or ""),
        ),
    )
    if len({str(row.get("parent_id") or "") for row in parents}) != len(parents):
        raise DocumentProfileCompilationError("parent IDs must be unique")
    if len({str(row.get("chunk_id") or "") for row in extractions}) != len(extractions):
        raise DocumentProfileCompilationError("extraction chunk IDs must be unique")

    document_artifact_id = _artifact_id(
        "profile-document-source",
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "source_version_id": source_version,
        },
    )
    document_slice = _source_slice(
        "document",
        [(document_artifact_id, _selected_document_payload(document))],
    )
    parent_slice = _source_slice(
        "parent_chunks",
        [
            (
                _artifact_id(
                    "profile-parent-source",
                    {
                        "corpus_id": corpus_id,
                        "doc_id": doc_id,
                        "parent_id": str(row.get("parent_id") or ""),
                        "source_version_id": source_version,
                    },
                ),
                _selected_parent_payload(row),
            )
            for row in parents
        ],
    )

    concept_refs: dict[str, set[str]] = defaultdict(set)
    concept_claims: dict[str, set[str]] = defaultdict(set)
    domain_signals: dict[str, DomainSignalV1] = {}
    frame_instances_by_parent: dict[str, list[Any]] = defaultdict(list)
    frame_evidence: list[ProfileFrameEvidenceV1] = []
    extraction_payload_rows: list[tuple[str, dict[str, Any]]] = []
    for row in extractions:
        _validate_row_ownership(
            row,
            corpus_id=corpus_id,
            doc_id=doc_id,
            source_hash=source_hash,
            source_version_id=source_version,
        )
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id:
            raise DocumentProfileCompilationError("extraction chunk ID is empty")
        local = LocalExtractionV1.model_validate(row.get("local_extraction"))
        compilation = ClaimCompilationV1.model_validate(row.get("claim_compilation"))
        if (
            local.document_id != doc_id
            or compilation.document_id != doc_id
            or local.child_id != chunk_id
            or compilation.child_id != chunk_id
        ):
            raise DocumentProfileCompilationError(
                "local extraction/claim ownership drifted"
            )
        row_artifact_id = _artifact_id(
            "profile-extraction-source",
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "source_version_id": source_version,
            },
        )
        extraction_payload_rows.append(
            (row_artifact_id, _selected_extraction_payload(row))
        )
        entities = {item.mention_id: item for item in local.entities}
        for claim in compilation.claims:
            entity_types = {}
            entity_labels = {}
            for argument in claim.arguments:
                if argument.filler_kind == "entity_mention":
                    entity = entities.get(argument.filler_ref)
                    if entity is None:
                        raise DocumentProfileCompilationError(
                            f"claim {claim.claim_id} references a missing entity"
                        )
                    entity_types[argument.filler_ref] = entity.entity_type
                    entity_labels[argument.filler_ref] = (
                        entity.canonical_label or entity.text
                    )
                    concept_values = {
                        argument.surface,
                        entity.text,
                        entity.canonical_label,
                    }
                else:
                    concept_values = {argument.surface}
                for value in concept_values:
                    for term in _concept_surfaces(value):
                        concept_refs[term].update(claim.evidence_sentence_ids)
                        concept_claims[term].add(claim.claim_id)
                for value in concept_values:
                    normalized = normalize_identity(value)
                    if not normalized:
                        continue
                    signal_id = _artifact_id(
                        "profile-domain-signal",
                        {
                            "doc_id": doc_id,
                            "claim_id": claim.claim_id,
                            "label": normalized,
                        },
                    )
                    domain_signals.setdefault(
                        signal_id,
                        DomainSignalV1(
                            schema_version="domain_signal.v1",
                            signal_id=signal_id,
                            label=value,
                            signal_kind="claim_concept",
                            evidence_ref_ids=sorted(claim.evidence_sentence_ids),
                            supporting_claim_ids=[claim.claim_id],
                        ),
                    )
            resolution = resolve_superframe_rule(
                claim,
                entity_types_by_mention_id=entity_types,
            )
            if not resolution.matches:
                continue
            match = resolution.matches[0]
            thread_keys = {
                argument.filler_ref: _thread_key(
                    corpus_id=corpus_id,
                    doc_id=doc_id,
                    argument=argument,
                    entity_labels=entity_labels,
                )
                for argument in claim.arguments
            }
            frame = compile_frame_instance(
                claim,
                match,
                thread_keys_by_filler_ref=thread_keys,
            )
            parent_scope = str(row.get("_parent_id") or "") or f"child:{chunk_id}"
            frame_instances_by_parent[parent_scope].append(frame)
            frame_evidence.append(
                ProfileFrameEvidenceV1(
                    frame_id=frame.frame_id,
                    source_claim_id=frame.source_claim_id,
                    source_rule_id=frame.source_rule_id,
                    evidence_sentence_ids=sorted(frame.evidence_sentence_ids),
                )
            )

    profile = document.get("doc_profile") or {}
    for concept in profile.get("concepts") or []:
        for term in _concept_surfaces(str(concept)):
            concept_refs[term].add(document_artifact_id)

    title = str(
        document.get("original_filename")
        or document.get("filename")
        or document.get("title")
        or profile.get("title")
        or ""
    )
    heading_rows = [(title, document_artifact_id)] if title else []
    heading_rows.extend(
        (
            str(heading),
            str(row.get("parent_id") or ""),
        )
        for row in parents
        for heading in row.get("heading_path") or []
        if heading
    )
    for label, evidence_ref in heading_rows:
        normalized = normalize_identity(label)
        if not normalized:
            continue
        signal_id = _artifact_id(
            "profile-heading-domain-signal",
            {
                "doc_id": doc_id,
                "label": normalized,
                "evidence_ref": evidence_ref,
            },
        )
        domain_signals[signal_id] = DomainSignalV1(
            schema_version="domain_signal.v1",
            signal_id=signal_id,
            label=label,
            signal_kind="section_heading",
            evidence_ref_ids=[evidence_ref],
            supporting_claim_ids=[],
        )

    target_id = _artifact_id(
        "profile-domain-target",
        {
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "source_version_id": source_version,
        },
    )
    domain_resolution = resolve_domains(
        target_artifact_id=target_id,
        signals=sorted(domain_signals.values(), key=lambda item: item.signal_id),
        context_profile_ids=[document_artifact_id],
    )
    domain_evidence = [
        ProfileDomainEvidenceV1(
            domain_id=item.domain_id,
            assignment_role=item.assignment_role,
            derivation_method=item.derivation_method,
            matched_normalized_terms=sorted(item.matched_normalized_terms),
            evidence_ref_ids=sorted(item.evidence_ref_ids),
            supporting_claim_ids=sorted(item.supporting_claim_ids),
        )
        for item in domain_resolution.assignments
    ]

    motif_evidence: list[ProfileMotifEvidenceV1] = []
    for parent_scope, frame_instances in sorted(frame_instances_by_parent.items()):
        sequence = [
            FrameSequenceItemV1(
                schema_version="frame_sequence_item.v1",
                sequence_index=index,
                frame_instance_id=frame.frame_instance_id,
            )
            for index, frame in enumerate(frame_instances)
        ]
        motif_result = match_motifs(
            target_artifact_id=f"{target_id}:{parent_scope}",
            frame_instances=frame_instances,
            sequence_items=sequence,
        )
        motif_evidence.extend(
            ProfileMotifEvidenceV1(
                motif_id=item.motif_id,
                motif_candidate_id=item.motif_candidate_id,
                frame_instance_ids=sorted(item.frame_instance_ids),
                frame_ids=sorted({stage.frame_id for stage in item.stage_matches}),
                source_claim_ids=sorted(item.source_claim_ids),
                evidence_sentence_ids=sorted(item.evidence_sentence_ids),
                matcher_disposition=item.matcher_disposition,
            )
            for item in motif_result.candidates
            if item.assignment_state == "candidate"
        )

    ranked_terms = sorted(
        concept_refs,
        key=lambda term: (
            -len(concept_claims.get(term, set())),
            -len(concept_refs[term]),
            term,
        ),
    )[:MAX_CONCEPT_TERMS]
    concept_evidence = [
        ProfileConceptEvidenceV1(
            concept_term=term,
            evidence_ref_ids=sorted(concept_refs[term]),
            supporting_claim_ids=sorted(concept_claims.get(term, set())),
        )
        for term in sorted(ranked_terms)
    ]
    extraction_slice = _source_slice(
        "extraction_rows",
        extraction_payload_rows,
    )
    source_slices = sorted(
        [document_slice, parent_slice, extraction_slice],
        key=lambda item: item.source_kind,
    )
    closure = current_registry_closure()
    recipe_hash = current_profile_recipe_hash(closure)
    logical_hash = profile_logical_hash(
        corpus_id=corpus_id,
        doc_id=doc_id,
        source_version_id=source_version,
    )
    payload = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": f"t91-doc-profile:{logical_hash.split(':', 1)[1]}",
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "source_version_id": source_version,
        "source_key": source_key,
        "source_content_sha256": source_hash,
        "domain_ids": sorted(item.domain_id for item in domain_evidence),
        "superframe_ids": sorted({item.frame_id for item in frame_evidence}),
        "motif_ids": sorted({item.motif_id for item in motif_evidence}),
        "concept_terms": sorted(item.concept_term for item in concept_evidence),
        "domain_evidence": [
            item.model_dump(mode="python")
            for item in sorted(domain_evidence, key=lambda item: item.domain_id)
        ],
        "frame_evidence": [
            item.model_dump(mode="python")
            for item in sorted(
                frame_evidence,
                key=lambda item: (item.frame_id, item.source_claim_id),
            )
        ],
        "motif_evidence": [
            item.model_dump(mode="python")
            for item in sorted(
                motif_evidence,
                key=lambda item: (item.motif_id, item.motif_candidate_id),
            )
        ],
        "concept_evidence": [
            item.model_dump(mode="python") for item in concept_evidence
        ],
        "source_slices": [item.model_dump(mode="python") for item in source_slices],
        "registry_closure": closure.model_dump(mode="python"),
        "input_set_hash": namespace_hash(
            "input-set",
            sorted(item.slice_hash for item in source_slices),
        ),
        "profile_recipe_hash": recipe_hash,
        "assignment_state": "candidate",
        "canonical_write": False,
        "llm_call_count": 0,
        "provider_spend_usd": 0.0,
    }
    return _finalize_profile(payload)
