"""Project CorpusEntityV1 into separated shadow schema trust classes (Phase 7).

Never writes production schema collections. Never collapses trust classes into
a single query_aliases array.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from models.alias_identity import CorpusEntityV1
from models.alias_schema_projection import (
    SHADOW_SCHEMA_PROJECTION_RELEASE,
    ShadowSchemaRecordV1,
    ShadowSchemaSurfaceV1,
)

PROJECTION_RELEASE = SHADOW_SCHEMA_PROJECTION_RELEASE


@dataclass(frozen=True)
class SchemaProjectionLinks:
    """Optional hierarchy/graph link inventory for a corpus entity."""

    linked_child_ids: tuple[str, ...] = ()
    linked_parent_ids: tuple[str, ...] = ()
    linked_section_ids: tuple[str, ...] = ()
    linked_graph_node_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SchemaProjectionBatch:
    records: list[ShadowSchemaRecordV1] = field(default_factory=list)
    projection_release: str = PROJECTION_RELEASE
    production_schema_mutations: int = 0
    collapsed_query_aliases_emitted: int = 0


def _surf(
    surface: str,
    *,
    trust_class: str,
    identity_authority: bool,
    expansion_mode: str,
    qualification: str = "alias_pipeline_v1",
    source_alias_candidate_ids: Sequence[str] = (),
    source_alias_decision_ids: Sequence[str] = (),
    expansion_scope: str | None = None,
) -> ShadowSchemaSurfaceV1:
    return ShadowSchemaSurfaceV1(
        surface=surface,
        trust_class=trust_class,  # type: ignore[arg-type]
        identity_authority=identity_authority,
        expansion_mode=expansion_mode,  # type: ignore[arg-type]
        qualification=qualification,
        source_alias_candidate_ids=sorted(set(source_alias_candidate_ids)),
        source_alias_decision_ids=sorted(set(source_alias_decision_ids)),
        expansion_scope=expansion_scope,
    )


def project_corpus_entity_to_shadow_schema(
    entity: CorpusEntityV1,
    *,
    links: SchemaProjectionLinks | None = None,
    legacy_query_aliases: Sequence[str] = (),
    alias_candidate_ids_by_surface: dict[str, list[str]] | None = None,
    alias_decision_ids_by_surface: dict[str, list[str]] | None = None,
) -> ShadowSchemaRecordV1:
    """Map one corpus entity into separated trust-class shadow fields."""

    links = links or SchemaProjectionLinks()
    cand_map = alias_candidate_ids_by_surface or {}
    dec_map = alias_decision_ids_by_surface or {}
    support = list(entity.supporting_alias_decision_ids)

    def ids_for(surface: str) -> tuple[list[str], list[str]]:
        key = surface.lower()
        cands = cand_map.get(key) or cand_map.get(surface) or []
        decs = dec_map.get(key) or dec_map.get(surface) or support
        return list(cands), list(decs)

    trusted: list[ShadowSchemaSurfaceV1] = []
    for surface in entity.trusted_aliases:
        cands, decs = ids_for(surface)
        trusted.append(
            _surf(
                surface,
                trust_class="trusted_aliases",
                identity_authority=True,
                expansion_mode="canonical_query_expansion",
                source_alias_candidate_ids=cands,
                source_alias_decision_ids=decs,
            )
        )

    temporal: list[ShadowSchemaSurfaceV1] = []
    for surface in entity.temporal_aliases:
        cands, decs = ids_for(surface)
        temporal.append(
            _surf(
                surface,
                trust_class="temporal_aliases",
                identity_authority=True,
                expansion_mode="canonical_query_expansion",
                qualification="temporal_former_or_historical",
                source_alias_candidate_ids=cands,
                source_alias_decision_ids=decs,
            )
        )

    retrieval: list[ShadowSchemaSurfaceV1] = []
    for surface in entity.retrieval_surface_variants:
        cands, decs = ids_for(surface)
        retrieval.append(
            _surf(
                surface,
                trust_class="retrieval_surface_variants",
                identity_authority=False,
                expansion_mode="bounded_assistance",
                source_alias_candidate_ids=cands,
                source_alias_decision_ids=decs,
            )
        )

    ambiguous: list[ShadowSchemaSurfaceV1] = []
    for surface in entity.ambiguous_aliases:
        cands, decs = ids_for(surface)
        ambiguous.append(
            _surf(
                surface,
                trust_class="ambiguous_aliases",
                identity_authority=False,
                expansion_mode="scoped_only",
                expansion_scope="document_or_parent_only",
                source_alias_candidate_ids=cands,
                source_alias_decision_ids=decs,
            )
        )

    related: list[ShadowSchemaSurfaceV1] = []
    for surface in entity.related_terms:
        cands, decs = ids_for(surface)
        related.append(
            _surf(
                surface,
                trust_class="related_terms",
                identity_authority=False,
                expansion_mode="shadow_trace_only",
                source_alias_candidate_ids=cands,
                source_alias_decision_ids=decs,
            )
        )

    descriptions: list[ShadowSchemaSurfaceV1] = []
    for surface in entity.descriptions:
        # Strip temporal markers from alias-like description spill if present.
        if str(surface).startswith("temporal_former_name:"):
            continue
        if str(surface).startswith("descriptive_apposition:"):
            text = surface.split(":", 1)[-1]
        else:
            text = surface
        if not text.strip():
            continue
        descriptions.append(
            _surf(
                text,
                trust_class="descriptions",
                identity_authority=False,
                expansion_mode="metadata_only",
                qualification="retrieval_metadata_only",
            )
        )

    legacy: list[ShadowSchemaSurfaceV1] = []
    seen = {
        s.surface.lower()
        for s in trusted + temporal + retrieval + ambiguous + related
    }
    for surface in legacy_query_aliases:
        if not surface or surface.lower() in seen:
            continue
        legacy.append(
            _surf(
                surface,
                trust_class="legacy_unqualified",
                identity_authority=False,
                expansion_mode="none",
                qualification="legacy_unqualified",
            )
        )

    return ShadowSchemaRecordV1.create(
        corpus_id=entity.corpus_id,
        corpus_entity_id=entity.corpus_entity_id,
        canonical_term=entity.canonical_name,
        entity_type=entity.entity_type,
        trusted_aliases=trusted,
        temporal_aliases=temporal,
        retrieval_surface_variants=retrieval,
        ambiguous_aliases=ambiguous,
        related_terms=related,
        descriptions=descriptions,
        legacy_unqualified=legacy,
        linked_child_ids=list(links.linked_child_ids),
        linked_parent_ids=list(links.linked_parent_ids),
        linked_section_ids=list(links.linked_section_ids),
        linked_graph_node_ids=list(links.linked_graph_node_ids),
        source_document_ids=list(entity.source_document_ids),
        supporting_alias_decision_ids=list(entity.supporting_alias_decision_ids),
        projection_release=PROJECTION_RELEASE,
    )


def project_corpus_entities_to_shadow_schemas(
    entities: Iterable[CorpusEntityV1] | None,
    *,
    links_by_entity_id: dict[str, SchemaProjectionLinks] | None = None,
    legacy_query_aliases_by_entity_id: dict[str, list[str]] | None = None,
) -> SchemaProjectionBatch:
    """Batch projection. Deterministic order. Zero production mutations."""

    link_map = links_by_entity_id or {}
    legacy_map = legacy_query_aliases_by_entity_id or {}
    rows = sorted(
        list(entities or []),
        key=lambda e: (e.corpus_id, e.canonical_name.lower(), e.corpus_entity_id),
    )
    records = [
        project_corpus_entity_to_shadow_schema(
            entity,
            links=link_map.get(entity.corpus_entity_id),
            legacy_query_aliases=legacy_map.get(entity.corpus_entity_id, ()),
        )
        for entity in rows
    ]
    return SchemaProjectionBatch(
        records=records,
        projection_release=PROJECTION_RELEASE,
        production_schema_mutations=0,
        collapsed_query_aliases_emitted=0,
    )


def shadow_records_as_dicts(batch: SchemaProjectionBatch) -> list[dict]:
    return [r.model_dump() for r in batch.records]
