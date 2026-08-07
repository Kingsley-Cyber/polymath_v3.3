#!/usr/bin/env python3
"""Post-bake durability checks for Phase-8 shadow wiring (runs inside container)."""

from __future__ import annotations

import json
import sys

from config import get_settings
from models.alias_identity import CorpusEntityV1
from models.schemas import RetrievalTier, SourceChunk
from services.ingestion.alias_retrieval_shadow import (
    alias_retrieval_controls,
    clear_shadow_schema_registry,
    register_shadow_schema_records,
    run_alias_retrieval_shadow,
)
from services.ingestion.alias_schema_projection import (
    SchemaProjectionLinks,
    project_corpus_entity_to_shadow_schema,
)


def main() -> int:
    s = get_settings()
    controls = alias_retrieval_controls(s)
    assert controls["enabled_globally"] is False
    assert controls["shadow_enabled"] is True
    assert controls["ranking_enabled"] is False
    assert controls["production_schema_writes"] is False
    assert controls["production_backfill"] is False

    fixture = "isolated_alias_fixture"
    clear_shadow_schema_registry()
    ent = CorpusEntityV1.create(
        corpus_id=fixture,
        canonical_name="Retrieval-Augmented Generation",
        document_entity_ids=["docent:rag"],
        accepted_merge_decision_ids=["m"],
        trusted_aliases=["RAG"],
        temporal_aliases=[],
        retrieval_surface_variants=[],
        descriptions=[],
        related_terms=[],
        ambiguous_aliases=[],
        conflicting_candidates=[],
        source_document_ids=["doc:a"],
        supporting_alias_decision_ids=["a"],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    rec = project_corpus_entity_to_shadow_schema(
        ent,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:rag:def",),
            linked_parent_ids=("parent:1",),
        ),
    )
    register_shadow_schema_records(fixture, [rec])
    chunks = [
        SourceChunk(
            chunk_id="child:rag:def",
            parent_id="parent:1",
            doc_id="doc:a",
            corpus_id=fixture,
            text="Retrieval-Augmented Generation (RAG) retrieves external context.",
            score=0.9,
            source_tier="dense",
        )
    ]
    reports = []
    for tier in (
        RetrievalTier.qdrant_only,
        RetrievalTier.qdrant_mongo,
        RetrievalTier.qdrant_mongo_graph,
    ):
        out, diag = run_alias_retrieval_shadow(
            query="What is RAG?",
            tier=tier,
            corpus_ids=[fixture],
            finalists=chunks,
            settings=s,
        )
        assert diag["status"] == "ok"
        assert [c.chunk_id for c in out] == [c.chunk_id for c in chunks]
        assert diag["ranking_effect"]["production_queries_unchanged"] is True
        reports.append(diag["query_report"]["replay"])

    _, d1 = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier=RetrievalTier.qdrant_mongo,
        corpus_ids=[fixture],
        finalists=chunks,
        settings=s,
    )
    _, d2 = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier=RetrievalTier.qdrant_mongo,
        corpus_ids=[fixture],
        finalists=chunks,
        settings=s,
    )
    replay_identical = d1["query_report"]["replay"] == d2["query_report"]["replay"]
    out = {
        "feature_flags_preserved": True,
        "controls": controls,
        "direct_ranking_unchanged": True,
        "replay_identical": replay_identical,
        "tier_replays": reports,
        "status": "DURABILITY_PROBE_OK",
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if replay_identical else 1


if __name__ == "__main__":
    sys.exit(main())
