"""Document-scoped entity clustering from gated + parent-aggregated alias evidence.

Phase 5 only. No corpus-wide clustering. No production writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from models.alias_identity import (
    ALIAS_CLUSTER_RELEASE,
    AliasCandidateV1,
    AliasDecisionV1,
    DocumentEntityV1,
    ParentAliasBundleV1,
)
from services.ingestion.alias_parent_aggregation import (
    ChildAliasEvidence,
    ParentAggregationBatch,
    ParentSourceRegion,
    aggregate_parent_alias_evidence,
)

CLUSTER_RELEASE = ALIAS_CLUSTER_RELEASE
_IDENTITY = frozenset({"ACCEPT_IDENTITY", "ACCEPT_TEMPORAL_IDENTITY"})


@dataclass(frozen=True)
class DocumentClusteringBatch:
    entities: list[DocumentEntityV1] = field(default_factory=list)
    parent_bundles: list[ParentAliasBundleV1] = field(default_factory=list)
    cluster_release: str = CLUSTER_RELEASE
    # Acceptance-gate counters (MEASURED diagnostics).
    ambiguous_acronym_merges: int = 0
    description_or_role_merges: int = 0
    retrieval_only_identity_merges: int = 0
    cooccurrence_only_merges: int = 0
    summaries_used_as_identity_evidence: int = 0


def _norm(value: str) -> str:
    return " ".join((value or "").lower().split())


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        self.add(item)
        while self._parent[item] != item:
            self._parent[item] = self._parent[self._parent[item]]
            item = self._parent[item]
        return item

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Deterministic link: lexicographically smaller root wins.
        if ra < rb:
            self._parent[rb] = ra
        else:
            self._parent[ra] = rb

    def clusters(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for item in sorted(self._parent):
            out.setdefault(self.find(item), []).append(item)
        return out


def _canonical_name_for_surfaces(surfaces: Sequence[str]) -> str:
    """Deterministic canonical: longest surface, then lexicographic."""

    cleaned = [s for s in surfaces if s and s.strip()]
    if not cleaned:
        return ""
    return sorted(cleaned, key=lambda s: (-len(s.strip()), s.lower(), s))[0]


def cluster_document_entities(
    evidence: Iterable[ChildAliasEvidence] | None,
    *,
    parent_regions: Iterable[ParentSourceRegion] | None = None,
    parent_batch: ParentAggregationBatch | None = None,
) -> DocumentClusteringBatch:
    """Build DocumentEntityV1 clusters for one or more documents.

    Identity merges only from ACCEPT_IDENTITY / ACCEPT_TEMPORAL_IDENTITY
    (child decisions and/or parent bundles with identity_merge_allowed).
    """

    rows = list(evidence or [])
    if parent_batch is None:
        parent_batch = aggregate_parent_alias_evidence(
            rows, parent_regions=parent_regions
        )
    bundles = list(parent_batch.bundles)

    # Group by document.
    by_doc: dict[str, list[ChildAliasEvidence]] = {}
    for row in rows:
        by_doc.setdefault(row.candidate.document_id, []).append(row)

    doc_bundles: dict[str, list[ParentAliasBundleV1]] = {}
    for bundle in bundles:
        doc_bundles.setdefault(bundle.document_id, []).append(bundle)

    entities: list[DocumentEntityV1] = []
    ambiguous_merges = 0
    desc_role_merges = 0
    retrieval_identity_merges = 0

    all_doc_ids = sorted(set(by_doc) | set(doc_bundles))
    for document_id in all_doc_ids:
        doc_entities, stats = _cluster_one_document(
            document_id=document_id,
            rows=by_doc.get(document_id, []),
            bundles=doc_bundles.get(document_id, []),
        )
        entities.extend(doc_entities)
        ambiguous_merges += stats["ambiguous_acronym_merges"]
        desc_role_merges += stats["description_or_role_merges"]
        retrieval_identity_merges += stats["retrieval_only_identity_merges"]

    entities.sort(
        key=lambda e: (e.document_id, e.canonical_name.lower(), e.document_entity_id)
    )
    return DocumentClusteringBatch(
        entities=entities,
        parent_bundles=bundles,
        cluster_release=CLUSTER_RELEASE,
        ambiguous_acronym_merges=ambiguous_merges,
        description_or_role_merges=desc_role_merges,
        retrieval_only_identity_merges=retrieval_identity_merges,
        cooccurrence_only_merges=0,
        summaries_used_as_identity_evidence=parent_batch.summaries_used_as_identity_evidence,
    )


def _cluster_one_document(
    *,
    document_id: str,
    rows: Sequence[ChildAliasEvidence],
    bundles: Sequence[ParentAliasBundleV1],
) -> tuple[list[DocumentEntityV1], dict[str, int]]:
    stats = {
        "ambiguous_acronym_merges": 0,
        "description_or_role_merges": 0,
        "retrieval_only_identity_merges": 0,
    }

    uf = _UnionFind()
    surface_meta: dict[str, dict] = {}

    def _touch(surface: str, **meta: object) -> None:
        key = _norm(surface)
        if not key:
            return
        uf.add(key)
        bucket = surface_meta.setdefault(
            key,
            {
                "display": surface,
                "mention_ids": set(),
                "accepted_alias_ids": set(),
                "retrieval_variant_ids": set(),
                "description_records": set(),
                "defining_child_ids": set(),
                "supporting_child_ids": set(),
                "entity_types": set(),
                "temporal_formers": set(),
            },
        )
        # Prefer longer display form.
        if len(surface) > len(str(bucket["display"])):
            bucket["display"] = surface
        for field_name, value in meta.items():
            if value is None:
                continue
            if field_name.endswith("_ids") or field_name.endswith("_records") or field_name.endswith("_formers"):
                cast_set = bucket[field_name]
                if isinstance(value, (set, list, tuple)):
                    cast_set.update(value)
                else:
                    cast_set.add(value)
            elif field_name == "entity_type" and value:
                bucket["entity_types"].add(str(value))

    # 1) Identity edges from parent bundles (preferred — includes support links).
    for bundle in bundles:
        if bundle.decision == "REVIEW" and bundle.ambiguity_status == "ambiguous_acronym":
            # Separate clusters: touch surfaces but do not union.
            _touch(
                bundle.canonical_surface,
                mention_ids={f"mention:{bundle.evidence_candidate_ids[0]}"}
                if bundle.evidence_candidate_ids
                else {f"mention:bundle:{bundle.parent_alias_bundle_id}:canon"},
                defining_child_ids=bundle.defining_child_ids,
                supporting_child_ids=bundle.supporting_child_ids,
                entity_type=bundle.entity_type,
            )
            _touch(
                bundle.candidate_surface,
                mention_ids={f"mention:bundle:{bundle.parent_alias_bundle_id}:cand"},
                supporting_child_ids=bundle.supporting_child_ids,
                entity_type=bundle.entity_type,
            )
            continue

        if bundle.identity_merge_allowed and bundle.decision in _IDENTITY:
            _touch(
                bundle.canonical_surface,
                accepted_alias_ids=bundle.evidence_candidate_ids,
                defining_child_ids=bundle.defining_child_ids,
                supporting_child_ids=bundle.supporting_child_ids,
                entity_type=bundle.entity_type,
                mention_ids=[
                    f"mention:{cid}" for cid in bundle.evidence_candidate_ids
                ]
                or [f"mention:bundle:{bundle.parent_alias_bundle_id}"],
            )
            _touch(
                bundle.candidate_surface,
                accepted_alias_ids=bundle.evidence_candidate_ids,
                defining_child_ids=bundle.defining_child_ids,
                supporting_child_ids=bundle.supporting_child_ids,
                entity_type=bundle.entity_type,
                mention_ids=[
                    f"mention:{cid}" for cid in bundle.evidence_candidate_ids
                ],
            )
            uf.union(_norm(bundle.canonical_surface), _norm(bundle.candidate_surface))
            if bundle.decision == "ACCEPT_TEMPORAL_IDENTITY":
                _touch(
                    bundle.canonical_surface,
                    temporal_formers={bundle.candidate_surface},
                    description_records={
                        f"temporal_former_name:{bundle.candidate_surface}"
                    },
                )
            continue

        if bundle.decision == "ACCEPT_RETRIEVAL_ONLY":
            # Attach as retrieval-only when a side already exists; never union.
            canon_key = _norm(bundle.canonical_surface)
            if canon_key in surface_meta:
                _touch(
                    bundle.canonical_surface,
                    retrieval_variant_ids=bundle.evidence_candidate_ids,
                    supporting_child_ids=bundle.supporting_child_ids,
                )
            else:
                _touch(
                    bundle.canonical_surface,
                    retrieval_variant_ids=bundle.evidence_candidate_ids,
                    supporting_child_ids=bundle.supporting_child_ids,
                    mention_ids=[
                        f"mention:{cid}" for cid in bundle.evidence_candidate_ids
                    ],
                )
                _touch(
                    bundle.candidate_surface,
                    retrieval_variant_ids=bundle.evidence_candidate_ids,
                )
                # Do NOT union — separate surfaces stay unmerged.
            continue

        if bundle.decision == "REVIEW":
            _touch(
                bundle.canonical_surface,
                description_records={
                    f"review:{bundle.candidate_type}:{bundle.candidate_surface}"
                },
                supporting_child_ids=bundle.supporting_child_ids,
            )

    # 2) Child-level identity decisions not already covered (safety net).
    for row in rows:
        cand = row.candidate
        dec = row.decision
        if dec.decision in _IDENTITY and dec.identity_merge_allowed:
            if dec.ambiguity_status == "ambiguous_acronym":
                stats["ambiguous_acronym_merges"] += 1
                continue
            _touch(
                cand.canonical_surface,
                accepted_alias_ids={cand.alias_candidate_id},
                defining_child_ids={row.child_id},
                supporting_child_ids={row.child_id},
                entity_type=cand.entity_type,
                mention_ids={f"mention:{cand.alias_candidate_id}"},
            )
            _touch(
                cand.candidate_surface,
                accepted_alias_ids={cand.alias_candidate_id},
                defining_child_ids={row.child_id},
                supporting_child_ids={row.child_id},
                mention_ids={f"mention:{cand.alias_candidate_id}"},
            )
            uf.union(_norm(cand.canonical_surface), _norm(cand.candidate_surface))
            if dec.decision == "ACCEPT_TEMPORAL_IDENTITY":
                _touch(
                    cand.canonical_surface,
                    temporal_formers={cand.candidate_surface},
                    description_records={f"temporal_former_name:{cand.candidate_surface}"},
                )
            continue

        if cand.candidate_type in {
            "descriptive_apposition",
            "role_apposition",
            "location_apposition",
        }:
            if dec.decision in _IDENTITY:
                stats["description_or_role_merges"] += 1
            _touch(
                cand.canonical_surface,
                description_records={
                    f"{cand.candidate_type}:{cand.candidate_surface}"
                },
                supporting_child_ids={row.child_id},
            )
            continue

        if dec.decision == "ACCEPT_RETRIEVAL_ONLY":
            if dec.identity_merge_allowed:
                stats["retrieval_only_identity_merges"] += 1
            _touch(
                cand.canonical_surface,
                retrieval_variant_ids={cand.alias_candidate_id},
                supporting_child_ids={row.child_id},
            )

    # Build entities from union-find components that have identity evidence OR
    # stand-alone retrieval/description anchors.
    clusters = uf.clusters()
    entities: list[DocumentEntityV1] = []
    emitted_roots: set[str] = set()

    for root, members in sorted(clusters.items()):
        metas = [surface_meta[m] for m in members if m in surface_meta]
        if not metas:
            continue
        accepted = set()
        retrieval = set()
        descriptions = set()
        defining = set()
        supporting = set()
        mentions = set()
        types: set[str] = set()
        displays: list[str] = []
        has_identity = False
        for meta in metas:
            accepted |= meta["accepted_alias_ids"]
            retrieval |= meta["retrieval_variant_ids"]
            descriptions |= meta["description_records"]
            defining |= meta["defining_child_ids"]
            supporting |= meta["supporting_child_ids"]
            mentions |= meta["mention_ids"]
            types |= meta["entity_types"]
            displays.append(str(meta["display"]))
            if meta["accepted_alias_ids"]:
                has_identity = True
        # Skip pure co-occurrence shells with no accepted / retrieval / description.
        if not accepted and not retrieval and not descriptions:
            continue
        # Components that only exist because two retrieval surfaces were touched
        # separately should remain separate — they were never unioned.
        canonical = _canonical_name_for_surfaces(displays)
        entity_type = sorted(types)[0] if len(types) == 1 else (
            sorted(types)[0] if types else None
        )
        # If multiple types conflict, keep None (do not invent).
        if len(types) > 1:
            entity_type = None

        # Identity clusters require defining children when they have accepted ids
        # from normal evidence; boundary reconstruction may only have defining.
        if has_identity and not defining and not supporting:
            continue

        entities.append(
            DocumentEntityV1.create(
                document_id=document_id,
                canonical_name=canonical,
                mention_ids=sorted(mentions),
                accepted_alias_ids=sorted(accepted),
                retrieval_variant_ids=sorted(retrieval),
                description_records=sorted(descriptions),
                defining_child_ids=sorted(defining),
                supporting_child_ids=sorted(supporting),
                entity_type=entity_type,
                cluster_release=CLUSTER_RELEASE,
            )
        )
        emitted_roots.add(root)

    return entities, stats


def cluster_from_candidates_and_decisions(
    candidates: Sequence[AliasCandidateV1],
    decisions: Sequence[AliasDecisionV1],
    *,
    parent_id_by_chunk: dict[str, str],
    child_text_by_chunk: dict[str, str] | None = None,
    child_spans_by_chunk: dict[str, tuple[int, int]] | None = None,
    parent_regions: Iterable[ParentSourceRegion] | None = None,
) -> DocumentClusteringBatch:
    """Convenience: zip candidates/decisions into ChildAliasEvidence then cluster."""

    decision_map = {d.alias_candidate_id: d for d in decisions}
    text_map = child_text_by_chunk or {}
    span_map = child_spans_by_chunk or {}
    evidence: list[ChildAliasEvidence] = []
    for cand in candidates:
        dec = decision_map.get(cand.alias_candidate_id)
        if dec is None:
            continue
        parent_id = parent_id_by_chunk.get(cand.chunk_id)
        if not parent_id:
            continue
        start_end = span_map.get(cand.chunk_id)
        evidence.append(
            ChildAliasEvidence(
                candidate=cand,
                decision=dec,
                parent_id=parent_id,
                child_id=cand.chunk_id,
                child_text=text_map.get(cand.chunk_id, ""),
                child_start_in_parent=None if start_end is None else start_end[0],
                child_end_in_parent=None if start_end is None else start_end[1],
            )
        )
    return cluster_document_entities(evidence, parent_regions=parent_regions)
