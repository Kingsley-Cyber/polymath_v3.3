"""P2.5b identifier recipes — stable IDs per FINAL_SCHEMA §Identifier recipes.

Each identifier is a namespaced hash of EXACTLY the fields the spec names,
serialized through the canonical serializer. Prefixes and field serializers
are frozen by golden tests (tests/test_identifier_recipes_golden.py); any
change is a new recipe version, never an edit.

Spec rules enforced here:
- No lineage inference: a logical doc_id REQUIRES a strong external source
  key. Absent one, Polymath must NOT infer that two uploads are versions of
  the same document — callers keep the legacy content-derived doc_id and bind
  versions only via explicit owner/source lineage (we raise instead of guess).
- Retries of the same deterministic work REUSE work_id; execution attempts
  and stochastic outputs get their own identifiers (attempt_id is a plain
  uuid4 at the call site — deliberately NOT defined here; raw outputs hash
  their exact bytes).
- evidence_ref_id already exists in models.semantic_artifacts
  (make_evidence_ref) and is NOT duplicated here.
"""

from __future__ import annotations

import hashlib
import uuid

from models.hash_taxonomy import canonicalize
from models.semantic_artifacts import UNIT_SEPARATOR, canonical_json, domain_hash

RECIPE_VERSION = "identifier_recipes.v1"


def _id(tag: str, prefix: str, value: object) -> str:
    digest = domain_hash(tag, canonicalize(value)).split(":", 1)[1]
    return f"{prefix}:{digest}"


def logical_doc_id(corpus_id: str, strong_source_key: str) -> str:
    """doc_id = hash("logical-document", corpus_id + strong_source_key)."""
    if not (corpus_id or "").strip():
        raise ValueError("corpus_id required")
    if not (strong_source_key or "").strip():
        raise ValueError(
            "strong_source_key required: without one, version lineage must not "
            "be inferred — keep the legacy content-derived doc_id instead"
        )
    return _id("logical-document", "doc", {
        "corpus_id": corpus_id, "strong_source_key": strong_source_key,
    })


def source_version_id(doc_id: str, source_content_hash: str) -> str:
    return _id("source-version", "srcv", {
        "doc_id": doc_id, "source_content_hash": source_content_hash,
    })


def hierarchy_node_id(
    source_version_id_: str, hierarchy_recipe_id: str,
    node_type: str, coordinate_or_ordinal: str,
) -> str:
    return _id("hierarchy-node", "hnode", {
        "source_version_id": source_version_id_,
        "hierarchy_recipe_id": hierarchy_recipe_id,
        "node_type": node_type,
        "coordinate_or_ordinal": coordinate_or_ordinal,
    })


def claim_id(
    ownership_namespace: str, knowledge_status: str,
    evidence_ref_ids: set[str] | frozenset[str] | list[str],
    derivation_parent_ids: set[str] | frozenset[str] | list[str],
    canonical_proposition_signature: str, scope_hash: str,
) -> str:
    """Sorted-set semantics for evidence/derivation ids (order-free)."""
    return _id("claim", "claim", {
        "ownership_namespace": ownership_namespace,
        "knowledge_status": knowledge_status,
        "evidence_ref_ids": frozenset(evidence_ref_ids),
        "derivation_parent_ids": frozenset(derivation_parent_ids),
        "canonical_proposition_signature": canonical_proposition_signature,
        "scope_hash": scope_hash,
    })


def artifact_revision_id(artifact_id: str, schema_hash: str, body_hash: str) -> str:
    return _id("artifact-revision", "rev", {
        "artifact_id": artifact_id, "schema_hash": schema_hash,
        "body_hash": body_hash,
    })


def work_id(artifact_type: str, input_set_hash: str, recipe_hash: str) -> str:
    return _id("semantic-work", "work", {
        "artifact_type": artifact_type, "input_set_hash": input_set_hash,
        "recipe_hash": recipe_hash,
    })


def raw_artifact_id(exact_raw_output_bytes: bytes) -> str:
    """Hashes exact BYTES (not JSON) — stochastic outputs stay distinct."""
    if not isinstance(exact_raw_output_bytes, (bytes, bytearray)):
        raise TypeError("raw_artifact_id takes exact output bytes")
    digest = hashlib.sha256(
        b"raw-output" + UNIT_SEPARATOR + bytes(exact_raw_output_bytes)
    ).hexdigest()
    return f"raw:{digest}"


def projection_point_id(
    artifact_id: str, representation_role: str, projection_profile_hash: str,
) -> str:
    """Deterministic UUID for Qdrant point identity (uuid_from_sha256)."""
    digest = hashlib.sha256(
        canonical_json(canonicalize({
            "artifact_id": artifact_id,
            "representation_role": representation_role,
            "projection_profile_hash": projection_profile_hash,
        })).encode("utf-8")
    ).digest()
    return str(uuid.UUID(bytes=digest[:16]))
