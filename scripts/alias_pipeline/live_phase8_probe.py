#!/usr/bin/env python3
"""Live Phase-8 shadow probe (fixture registry + production path unchanged)."""

from __future__ import annotations

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
from config import get_settings

FIXTURE = "isolated_alias_fixture"


def main() -> int:
    s = get_settings()
    controls = alias_retrieval_controls(s)
    print("CONTROLS", controls)
    assert controls["enabled_globally"] is False
    assert controls["shadow_enabled"] is True
    assert controls["ranking_enabled"] is False
    assert controls["production_schema_writes"] is False

    clear_shadow_schema_registry()
    ent = CorpusEntityV1.create(
        corpus_id=FIXTURE,
        canonical_name="Retrieval-Augmented Generation",
        document_entity_ids=["docent:rag"],
        accepted_merge_decision_ids=["corpusmerge:rag"],
        trusted_aliases=["RAG"],
        temporal_aliases=[],
        retrieval_surface_variants=[],
        descriptions=[],
        related_terms=[],
        ambiguous_aliases=[],
        conflicting_candidates=[],
        source_document_ids=["doc:a"],
        supporting_alias_decision_ids=["aliascand:rag"],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    rec = project_corpus_entity_to_shadow_schema(
        ent,
        links=SchemaProjectionLinks(
            linked_child_ids=("child:rag:def",),
            linked_parent_ids=("parent:1",),
        ),
    )
    register_shadow_schema_records(FIXTURE, [rec])
    chunks = [
        SourceChunk(
            chunk_id="child:other",
            parent_id="p9",
            doc_id="doc:z",
            corpus_id=FIXTURE,
            text="gardening tips for tomatoes",
            score=0.4,
            source_tier="dense",
        ),
        SourceChunk(
            chunk_id="child:rag:def",
            parent_id="parent:1",
            doc_id="doc:a",
            corpus_id=FIXTURE,
            text="Retrieval-Augmented Generation (RAG) retrieves external context.",
            score=0.9,
            source_tier="dense",
        ),
    ]
    for tier in (
        RetrievalTier.qdrant_only,
        RetrievalTier.qdrant_mongo,
        RetrievalTier.qdrant_mongo_graph,
    ):
        out, diag = run_alias_retrieval_shadow(
            query="What is RAG?",
            tier=tier,
            corpus_ids=[FIXTURE],
            finalists=chunks,
            settings=s,
        )
        print(
            "TIER",
            tier.value,
            "status",
            diag["status"],
            "latency",
            diag.get("latency_s"),
            "traces",
            len(diag.get("query_report", {}).get("schema_traces", [])),
            "unchanged",
            diag.get("ranking_effect", {}).get("production_queries_unchanged"),
        )
        assert diag["status"] == "ok"
        assert diag["schema_records_as_citations"] == 0
        assert [c.chunk_id for c in out] == [c.chunk_id for c in chunks]

    clear_shadow_schema_registry()
    prod_chunks = list(chunks)
    out2, diag2 = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier=RetrievalTier.qdrant_mongo,
        corpus_ids=["999b5934-272e-4f20-a538-b5d422249a05"],
        finalists=prod_chunks,
        settings=s,
    )
    print(
        "PROD_PATH",
        diag2["status"],
        "records",
        diag2.get("shadow_records_loaded"),
        "unchanged",
        [c.chunk_id for c in out2] == [c.chunk_id for c in prod_chunks],
    )
    assert [c.chunk_id for c in out2] == [c.chunk_id for c in prod_chunks]
    print("LIVE_PROBE_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
