"""Shared validated-wire conversion for deterministic extraction lanes.

The ``polymath.extract.v1`` wire shape (validated entity/relation/fact dicts)
is produced by more than one lane — the RunPod Flash remote function returns
it, and backend-side code converts it into ``services.ghost_b`` dataclasses.
This module owns that conversion so no lane has to import another lane.
"""

from __future__ import annotations

SCHEMA_VERSION = "polymath.extract.v1"


def to_results(raw: list[dict]) -> list:
    """Build ExtractionResult dataclasses from validated wire dicts."""
    from services.ghost_b import EntityItem, ExtractionResult, FactItem, RelationItem

    out = []
    for r in raw:
        entities = [
            EntityItem(
                canonical_name=e["canonical_name"],
                surface_form=e.get("surface_form", ""),
                entity_type=e["entity_type"],
                confidence=float(e.get("confidence") or 0.0),
                query_aliases=list(e.get("query_aliases") or []),
                definitional_phrase=e.get("definitional_phrase", ""),
                object_kind=e.get("object_kind", ""),
            )
            for e in (r.get("entities") or [])
        ]
        relations = [
            RelationItem(
                subject=x["subject"], predicate=x["predicate"], object=x["object"],
                object_kind=x.get("object_kind", "entity"),
                confidence=float(x.get("confidence") or 0.0),
                evidence_phrase=x.get("evidence_phrase", ""),
                relation_cue=x.get("relation_cue", ""),
                source_predicate=None, validation_status=None,
            )
            for x in (r.get("relations") or [])
        ]
        facts = [
            FactItem(
                subject=f["subject"], fact_type=f["fact_type"],
                property_name=f.get("property_name", ""), value=f.get("value", ""),
                unit=(f.get("unit") or None), condition=(f.get("condition") or None),
                confidence=float(f.get("confidence") or 0.0),
                evidence_phrase=f.get("evidence_phrase", ""),
            )
            for f in (r.get("facts") or [])
        ]
        out.append(ExtractionResult(
            schema_version=r.get("schema_version") or SCHEMA_VERSION,
            chunk_id=r.get("chunk_id", ""),
            doc_id=r.get("doc_id", ""),
            corpus_id=r.get("corpus_id", ""),
            entities=entities,
            relations=relations,
            facts=facts,
            text=r.get("text", ""),
            entity_drop_count=int(r.get("entity_drop_count") or 0),
            relation_drop_count=int(r.get("relation_drop_count") or 0),
            evidence_drop_count=int(r.get("evidence_drop_count") or 0),
            fact_drop_count=int(r.get("fact_drop_count") or 0),
            # R-pre: full counter map survives the in-process path too.
            extraction_counters={
                str(k): int(v)
                for k, v in (r.get("extraction_counters") or {}).items()
            },
            schema_lens_id=r.get("schema_lens_id"),
            # T-HOOK-1 additive capture fields (wire contract v3); rows that
            # pre-date the field fall back to the dataclass defaults.
            temporal_captures=list(r.get("temporal_captures") or []),
            temporal_capture_version=r.get("temporal_capture_version"),
        ))
    return out
