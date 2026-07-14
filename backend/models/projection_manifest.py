"""P2.5b ProjectionManifest — frozen identity for every projection family.

Contract (FINAL_SCHEMA §12 + checklist P2.5b + owner store ruling 2026-07-14):
Mongo plus ONE manifest must be able to reproduce the exact identity set of a
Qdrant/Neo4j projection. A manifest freezes: what was projected (source schema
hashes), how it was represented (representation role + embedding profile
INCLUDING instruction_version), how it is stored/searched (payload schema,
quantization/search compatibility), and where to roll back (predecessor).

Manifests are immutable: any change of any field = a NEW manifest whose
`rollback_predecessor` points at the old one. manifest_id derives from the
"projection-profile" hash namespace, so identity is content-addressed.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from models.hash_taxonomy import namespace_hash

MANIFEST_VERSION = "projection_manifest.v1"

# Owner-ruled projection families (RETRIEVAL_OPTIMIZATION_PLAN, 2026-07-14).
QDRANT_FAMILIES = (
    "source_child",
    "context_enriched_child",
    "atomic_claim",            # owner-marked illustrative; promotion-gated
    "parent_summary",
    "document_summary",
    "latent_concept",
    "motif_description",
    "cross_domain_analogy",
)
NEO4J_PARTITIONS = (
    "asserted_claim_graph",
    "validated_semantic_graph",
    "provisional_expansion_graph",
    "analogy_graph",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class EmbeddingProfile(StrictModel):
    """Only meaningful for vector stores; None for graph partitions."""

    model_id: str
    dims: int
    quantization: Literal["float32", "float16", "mxfp8", "binary"]
    # Queries embed WITH a versioned instruction; documents embed raw.
    instruction_version: str
    document_side_instruction: Literal["raw"] = "raw"


class SearchCompat(StrictModel):
    """Quantization/search compatibility knobs frozen with the projection."""

    oversampling: float = Field(ge=1.0)
    rescore_with_full_vectors: bool
    distance: Literal["cosine", "dot", "euclid"] = "cosine"


class ProjectionManifest(StrictModel):
    schema_version: Literal["projection_manifest.v1"]
    store: Literal["qdrant", "neo4j"]
    family: str
    representation_role: str
    source_schema_hashes: dict[str, str]
    payload_schema_hash: str
    embedding_profile: Optional[EmbeddingProfile] = None
    search_compat: Optional[SearchCompat] = None
    recipe_version: str
    rollback_predecessor: Optional[str] = None

    def validate_family(self) -> "ProjectionManifest":
        valid = QDRANT_FAMILIES if self.store == "qdrant" else NEO4J_PARTITIONS
        if self.family not in valid:
            raise ValueError(
                f"unknown {self.store} projection family {self.family!r}; "
                f"valid: {valid}"
            )
        if self.store == "qdrant" and self.embedding_profile is None:
            raise ValueError("qdrant manifests require an embedding_profile")
        if self.store == "neo4j" and self.embedding_profile is not None:
            raise ValueError("neo4j manifests must not carry an embedding_profile")
        return self

    @property
    def manifest_id(self) -> str:
        body = self.model_dump(exclude={"rollback_predecessor"})
        digest = namespace_hash("projection-profile", body).split(":", 1)[1]
        return f"projm:{digest}"


def make_manifest(**kwargs) -> ProjectionManifest:
    """Construct + family-validate in one step (the only supported path)."""
    return ProjectionManifest(schema_version="projection_manifest.v1", **kwargs).validate_family()
