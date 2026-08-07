"""EvidenceItem — normalized evidence unit for verification stages (Slice 1).

A typed VIEW over the hydrated ``SourceChunk`` (models/_schemas_legacy.py):
no field duplication of storage shapes, references + normalized accessors
only. Downstream consumers (contradiction detection, post-synthesis claim
verification — steps 9/10 of the owner-mandated order) operate on
EvidenceItems instead of untyped dicts.

The shadow graph-read rule is encoded here: graph-sourced evidence carries
release metadata so canonical synthesis can refuse mismatched/unpromoted
records while experimental inspection may still surface them labeled.

Dependency-light: ``from_source_chunk`` duck-types the chunk shape so this
module never imports services.* and contract tests run standalone.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

EVIDENCE_ITEM_SCHEMA_VERSION = "polymath.evidence_item.v1"

AssertionLane = Literal[
    "lexical",
    "vector",
    "summary",
    "graph",
    "document_anchor",
    "vocabulary_translation",
    "web",
    "direct_original_vector",
    "trusted_canonical_expansion",
    "linked_child_anchor",
    "mongo_lexical",
    "summary_guided_child",
    "qualified_graph_child",
    "unknown",
]


class EvidenceReleaseMetadata(BaseModel):
    """Release stamps every graph-sourced result must carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    extractor_release: str = ""
    ontology_release: str = ""
    acceptance_policy_release: str = ""
    promotion_version: str = ""
    certificate_id: str | None = None
    canonical_or_shadow: Literal["canonical", "shadow", "unknown"] = "unknown"

    def matches(self, pins: dict[str, str]) -> bool:
        """True when every stamped field equals the requested release pin.

        Unstamped fields (empty) fail closed: an unpinned record can never
        masquerade as canonical evidence.
        """

        checks = (
            (self.extractor_release, pins.get("extractor_release", "")),
            (self.ontology_release, pins.get("ontology_release", "")),
            (self.acceptance_policy_release, pins.get("acceptance_policy_release", "")),
        )
        for stamped, requested in checks:
            if not stamped or not requested or stamped != requested:
                return False
        return self.canonical_or_shadow == "canonical"


class EvidenceItem(BaseModel):
    """One normalized piece of evidence shown to (or usable by) synthesis."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = Field(default=EVIDENCE_ITEM_SCHEMA_VERSION)

    # Identity spine (POLYMATH_ARCHITECTURE §1 — one set of keys, all stores).
    corpus_id: str = Field(min_length=1)
    doc_id: str = Field(min_length=1)
    parent_id: str = ""
    chunk_id: str = Field(min_length=1)

    # Evidence payload reference (text retained for span-exact verification).
    text: str = ""
    score: float = 0.0
    source_tier: str = ""
    assertion_lane: AssertionLane = "unknown"

    # Provenance copied by reference from the hydrated chunk.
    provenance: tuple[dict[str, Any], ...] = ()
    metadata_keys: tuple[str, ...] = ()

    release: EvidenceReleaseMetadata = Field(default_factory=EvidenceReleaseMetadata)

    # Cross-domain curation spine (additive; defaults keep Slice-1 callers valid).
    domain: str = ""
    query_obligations: tuple[str, ...] = ()
    retrieval_lanes: tuple[str, ...] = ()
    lane_ranks: dict[str, int] = Field(default_factory=dict)
    direct_query_support: bool = False
    vocabulary_support: bool = False
    lexical_support: bool = False
    summary_support: bool = False
    graph_support: bool = False
    temporal_validity: str = ""
    text_hash: str = ""
    token_count: int = 0
    selection_reasons: tuple[str, ...] = ()

    @classmethod
    def from_source_chunk(cls, chunk: Any, *, assertion_lane: str = "unknown") -> "EvidenceItem":
        """Build the view from a duck-typed hydrated SourceChunk."""

        lane = assertion_lane if assertion_lane in AssertionLane.__args__ else "unknown"
        provenance = tuple(chunk.provenance or ()) if getattr(chunk, "provenance", None) else ()
        metadata = getattr(chunk, "metadata", None) or {}
        planned_lanes = metadata.get("planned_lanes") or ()
        if isinstance(planned_lanes, str):
            planned_lanes = (planned_lanes,)
        lane_ranks_raw = metadata.get("lane_ranks") or {}
        lane_ranks = {
            str(k): int(v)
            for k, v in dict(lane_ranks_raw).items()
            if str(k) and v is not None
        }
        text = str(getattr(chunk, "text", "") or "")
        text_hash = (
            str(metadata.get("text_hash") or "")
            or (hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else "")
        )
        return cls(
            corpus_id=str(getattr(chunk, "corpus_id", "") or ""),
            doc_id=str(getattr(chunk, "doc_id", "") or ""),
            parent_id=str(getattr(chunk, "parent_id", "") or ""),
            chunk_id=str(getattr(chunk, "chunk_id", "") or ""),
            text=text,
            score=float(getattr(chunk, "score", 0.0) or 0.0),
            source_tier=str(getattr(chunk, "source_tier", "") or ""),
            assertion_lane=lane,
            provenance=provenance,
            metadata_keys=tuple(sorted(metadata.keys())),
            domain=str(
                getattr(chunk, "domain", None)
                or metadata.get("domain")
                or ""
            ),
            query_obligations=tuple(
                str(x) for x in (metadata.get("query_obligations") or ()) if str(x)
            ),
            retrieval_lanes=tuple(str(x) for x in planned_lanes if str(x)),
            lane_ranks=lane_ranks,
            direct_query_support=bool(metadata.get("direct_query_support")),
            vocabulary_support=bool(metadata.get("vocabulary_support")),
            lexical_support=bool(metadata.get("lexical_support")),
            summary_support=bool(metadata.get("summary_support")),
            graph_support=bool(metadata.get("graph_support")),
            temporal_validity=str(metadata.get("temporal_validity") or ""),
            text_hash=text_hash,
            token_count=int(metadata.get("token_count") or 0),
            selection_reasons=tuple(
                str(x) for x in (metadata.get("selection_reasons") or ()) if str(x)
            ),
        )

    def evidence_key(self) -> str:
        """Stable identity for dedupe and claim-span matching."""

        return f"{self.corpus_id}:{self.doc_id}:{self.chunk_id}"
