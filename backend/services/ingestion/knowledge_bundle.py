"""Adapt live ExtractionResult / ghost_b rows → KnowledgeArtifactBundleV1.

Recovery adapter only. Does not re-extract. Does not invent Facts.
Graph/summary/schemas should consume these bundles instead of raw facts=[].
"""

from __future__ import annotations

from typing import Any

from models.knowledge_artifact_bundle import (
    BundleAudit,
    BundleEntityMention,
    BundleIdentity,
    BundleRelation,
    BundleReleases,
    BundleSourceStructure,
    KnowledgeArtifactBundleV1,
)

_ACCEPTED_PREFIXES = ("accept_",)
_REVIEW_PREFIXES = ("review_", "store_", "shadow_")
_REJECT_PREFIXES = ("reject_",)


def _as_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    if hasattr(row, "model_dump"):
        return row.model_dump()
    if hasattr(row, "__dict__"):
        return dict(row.__dict__)
    return {}


def _status_bucket(status: str) -> str:
    s = (status or "").lower()
    if any(s.startswith(p) for p in _ACCEPTED_PREFIXES):
        return "accepted"
    if any(s.startswith(p) for p in _REJECT_PREFIXES):
        return "rejected"
    if any(s.startswith(p) for p in _REVIEW_PREFIXES):
        return "review"
    if "open" in s or s in {"", "unknown"}:
        return "open"
    # Default: treat unknown non-reject as candidate/open
    return "open"


def _entity_mention(ent: dict[str, Any]) -> BundleEntityMention:
    canonical = str(ent.get("canonical_name") or ent.get("surface_form") or "").strip()
    surface = str(ent.get("surface_form") or canonical).strip()
    aliases = [str(a) for a in (ent.get("query_aliases") or []) if a]
    return BundleEntityMention(
        surface=surface,
        canonical_name=canonical,
        entity_type=str(ent.get("entity_type") or ent.get("type") or ""),
        confidence=float(ent.get("confidence") or 0.0),
        query_aliases=aliases,
    )


def _relation(rel: dict[str, Any]) -> BundleRelation:
    status = str(rel.get("validation_status") or "")
    pred = str(rel.get("predicate") or rel.get("source_predicate") or "")
    evidence = str(rel.get("evidence_phrase") or rel.get("evidence_text") or "")
    return BundleRelation(
        subject=str(rel.get("subject") or "").strip(),
        predicate=pred.strip(),
        object=str(rel.get("object") or "").strip(),
        confidence=float(rel.get("confidence") or 0.0),
        evidence_text=evidence,
        validation_status=status,
        acceptance_lane=status or "unknown",
        source_predicate=str(rel.get("source_predicate") or pred),
        object_kind=str(rel.get("object_kind") or "entity"),
        negated=bool(rel.get("negated") or "negat" in status.lower()),
        modal=bool(rel.get("modal") or "modal" in status.lower()),
        attribution=str(rel.get("attribution") or ""),
    )


def bundle_from_extraction_row(
    row: Any,
    *,
    corpus_generation: str = "",
    parent_id: str = "",
    section_id: str = "",
    run_id: str = "",
    heading_path: list[str] | None = None,
    trusted_aliases: list[str] | None = None,
    corpus_entity_refs: list[str] | None = None,
    ontology_release: str = "",
    projection_release: str = "polymath.graph_projection.v1",
    alias_pipeline_release: str = "",
) -> KnowledgeArtifactBundleV1:
    """Build one chunk-level KnowledgeArtifactBundle from a ghost_b / ExtractionResult row."""

    data = _as_dict(row)
    local = _as_dict(data.get("local_extraction"))
    chunk_id = str(data.get("chunk_id") or "")
    doc_id = str(data.get("doc_id") or data.get("document_id") or "")
    corpus_id = str(data.get("corpus_id") or "")
    text = str(data.get("text") or "")

    mentions = [_entity_mention(_as_dict(e)) for e in (data.get("entities") or [])]
    accepted: list[BundleRelation] = []
    review: list[BundleRelation] = []
    rejected: list[BundleRelation] = []
    open_rels: list[BundleRelation] = []
    candidates: list[BundleRelation] = []
    negation: list[dict[str, Any]] = []
    modality: list[dict[str, Any]] = []

    for raw in data.get("relations") or []:
        rel = _relation(_as_dict(raw))
        if not rel.subject or not rel.object or not rel.predicate:
            continue
        candidates.append(rel)
        bucket = _status_bucket(rel.validation_status)
        if bucket == "accepted":
            accepted.append(rel)
        elif bucket == "rejected":
            rejected.append(rel)
        elif bucket == "review":
            review.append(rel)
        else:
            open_rels.append(rel)
        if rel.negated:
            negation.append(
                {
                    "subject": rel.subject,
                    "predicate": rel.predicate,
                    "object": rel.object,
                    "status": rel.validation_status,
                }
            )
        if rel.modal:
            modality.append(
                {
                    "subject": rel.subject,
                    "predicate": rel.predicate,
                    "object": rel.object,
                    "status": rel.validation_status,
                }
            )

    # Qualified facts remain release-gated; Relex facts=[] is honest empty.
    facts = [_as_dict(f) for f in (data.get("facts") or [])]

    surfaces = sorted(
        {
            *(m.surface for m in mentions if m.surface),
            *(m.canonical_name for m in mentions if m.canonical_name),
            *(a for m in mentions for a in m.query_aliases),
            *(trusted_aliases or []),
        }
    )

    gate = dict(local.get("gate_decision_counts") or {})
    unresolved_predicates = sorted(
        {r.predicate for r in open_rels if r.predicate}
        | {r.source_predicate for r in open_rels if r.source_predicate}
    )

    bundle = KnowledgeArtifactBundleV1(
        identity=BundleIdentity(
            run_id=run_id or str(local.get("run_id") or ""),
            corpus_id=corpus_id,
            corpus_generation=corpus_generation,
            document_id=doc_id,
            chunk_id=chunk_id,
            parent_id=parent_id,
            section_id=section_id,
        ),
        releases=BundleReleases(
            extraction_contract_hash=str(
                data.get("extraction_contract_hash")
                or local.get("contract")
                or ""
            ),
            extractor_release=str(
                local.get("extractor_release") or data.get("extractor_release") or ""
            ),
            model_hash=str(local.get("model_hash") or ""),
            alias_pipeline_release=alias_pipeline_release,
            entity_cluster_release="",
            ontology_release=ontology_release
            or str(local.get("ontology_hash") or ""),
            acceptance_policy_release=str(
                local.get("acceptance_policy_hash") or ""
            ),
            summary_release="",
            projection_release=projection_release,
        ),
        source_structure=BundleSourceStructure(
            heading_path=list(heading_path or []),
            source_offsets={},
            sentence_boundaries=[],
            page_or_position_metadata={},
        ),
        entity_mentions=mentions,
        document_entities=mentions,
        corpus_entity_refs=list(corpus_entity_refs or []),
        trusted_aliases=list(trusted_aliases or []),
        retrieval_surface_variants=surfaces,
        descriptions=[],
        relation_candidates=candidates,
        accepted_relation_assertions=accepted,
        open_relations=open_rels,
        review_relations=review,
        rejected_relations=rejected,
        qualified_claims=[],
        qualified_facts=facts,
        negation=negation,
        modality=modality,
        attribution=[],
        temporal_expressions=[],
        quantities=[],
        evidence_text=text,
        evidence_offsets={},
        supporting_sentence_ids=[],
        supporting_child_ids=[chunk_id] if chunk_id else [],
        audit=BundleAudit(
            gate_decisions={str(k): int(v) for k, v in gate.items()},
            rejection_reasons=[],
            unresolved_entities=[],
            unresolved_predicates=unresolved_predicates,
            source_adapter="ghost_b_extraction_result",
        ),
    )
    return bundle.with_hash()


async def bundles_for_document(
    db: Any,
    *,
    corpus_id: str,
    document_id: str,
    corpus_generation: str = "",
) -> list[KnowledgeArtifactBundleV1]:
    """Load all post-gate ghost rows for a document as bundles."""

    parent_map: dict[str, str] = {}
    async for chunk in db["chunks"].find(
        {"corpus_id": corpus_id, "doc_id": document_id},
        {"chunk_id": 1, "parent_id": 1},
    ):
        cid = str(chunk.get("chunk_id") or "")
        if cid:
            parent_map[cid] = str(chunk.get("parent_id") or "")

    out: list[KnowledgeArtifactBundleV1] = []
    async for row in db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id, "doc_id": document_id}
    ):
        cid = str(row.get("chunk_id") or "")
        out.append(
            bundle_from_extraction_row(
                row,
                corpus_generation=corpus_generation,
                parent_id=parent_map.get(cid, ""),
            )
        )
    out.sort(key=lambda b: b.identity.chunk_id)
    return out


def assertion_rows_from_bundle(
    bundle: KnowledgeArtifactBundleV1,
) -> list[dict[str, Any]]:
    """Shape accepted assertions for graph projectors (never uses facts=[] alone)."""

    rows: list[dict[str, Any]] = []
    for rel in bundle.accepted_relation_assertions:
        rows.append(
            {
                "subject": rel.subject,
                "predicate": rel.predicate,
                "object": rel.object,
                "object_kind": rel.object_kind,
                "confidence": rel.confidence,
                "evidence_phrase": rel.evidence_text,
                "source_predicate": rel.source_predicate,
                "validation_status": rel.validation_status,
                "negated": rel.negated,
                "modal": rel.modal,
            }
        )
    return rows
