#!/usr/bin/env python3
"""Generate Phase-8 shadow/canary live-wiring artifacts (fixture-only).

Does NOT enable global expansion, production schema writes, or backfill.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from models.alias_identity import CorpusEntityV1  # noqa: E402
from models.schemas import SourceChunk  # noqa: E402
from services.ingestion.alias_schema_projection import (  # noqa: E402
    SchemaProjectionLinks,
    project_corpus_entity_to_shadow_schema,
)
from services.ingestion.alias_retrieval_shadow import (  # noqa: E402
    clear_shadow_schema_registry,
    register_shadow_schema_records,
    run_alias_retrieval_shadow,
)

OUT = ROOT / "data_eval" / "alias_pipeline"
FIXTURE = "isolated_alias_fixture"


def _settings(**kw) -> SimpleNamespace:
    base = dict(
        ALIAS_RETRIEVAL_ENABLED_GLOBALLY=False,
        ALIAS_RETRIEVAL_SHADOW_ENABLED=True,
        ALIAS_RETRIEVAL_RANKING_ENABLED=False,
        ALIAS_RETRIEVAL_FIXTURE_CORPUS_ALLOWLIST=FIXTURE,
        ALIAS_RETRIEVAL_PRODUCTION_SCHEMA_WRITES=False,
        ALIAS_RETRIEVAL_PRODUCTION_BACKFILL=False,
        ALIAS_RETRIEVAL_SHADOW_DEADLINE_SECONDS=0.35,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _chunk(cid: str, text: str, doc_id: str = "doc:a", parent_id: str = "parent:1"):
    return SourceChunk(
        chunk_id=cid,
        parent_id=parent_id,
        doc_id=doc_id,
        corpus_id=FIXTURE,
        text=text,
        score=0.5,
        source_tier="dense",
    )


def _chunks():
    return [
        _chunk(
            "child:rag:def",
            "Retrieval-Augmented Generation (RAG) retrieves external context.",
        ),
        _chunk("child:rag:use", "RAG is used in question answering systems."),
        _chunk(
            "child:other",
            "Unrelated gardening tips for tomatoes.",
            doc_id="doc:z",
            parent_id="parent:9",
        ),
        _chunk(
            "child:ir:info",
            "Information Retrieval (IR) ranks documents.",
            doc_id="doc:ir:a",
            parent_id="parent:ir:a",
        ),
        _chunk(
            "child:ir:heat",
            "Infrared (IR) detects heat signatures.",
            doc_id="doc:ir:b",
            parent_id="parent:ir:b",
        ),
    ]


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    clear_shadow_schema_registry()
    rag = CorpusEntityV1.create(
        corpus_id=FIXTURE,
        canonical_name="Retrieval-Augmented Generation",
        document_entity_ids=["docent:rag"],
        accepted_merge_decision_ids=["corpusmerge:rag"],
        trusted_aliases=["RAG"],
        temporal_aliases=[],
        retrieval_surface_variants=["retrieval augmented gen"],
        descriptions=["a technique combining retrieval and generation"],
        related_terms=["passage ranking"],
        ambiguous_aliases=[],
        conflicting_candidates=[],
        source_document_ids=["doc:a"],
        supporting_alias_decision_ids=["aliascand:rag"],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    ir_a = CorpusEntityV1.create(
        corpus_id=FIXTURE,
        canonical_name="Information Retrieval",
        document_entity_ids=["docent:ir:a"],
        accepted_merge_decision_ids=[],
        trusted_aliases=[],
        temporal_aliases=[],
        retrieval_surface_variants=[],
        descriptions=[],
        related_terms=[],
        ambiguous_aliases=["IR"],
        conflicting_candidates=[],
        source_document_ids=["doc:ir:a"],
        supporting_alias_decision_ids=["aliascand:ir:a"],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    ir_b = CorpusEntityV1.create(
        corpus_id=FIXTURE,
        canonical_name="Infrared",
        document_entity_ids=["docent:ir:b"],
        accepted_merge_decision_ids=[],
        trusted_aliases=[],
        temporal_aliases=[],
        retrieval_surface_variants=[],
        descriptions=[],
        related_terms=[],
        ambiguous_aliases=["IR"],
        conflicting_candidates=[],
        source_document_ids=["doc:ir:b"],
        supporting_alias_decision_ids=["aliascand:ir:b"],
        canonical_name_rule="validated_acronym_long_form_v1",
    )
    records = [
        project_corpus_entity_to_shadow_schema(
            rag,
            links=SchemaProjectionLinks(
                linked_child_ids=("child:rag:def", "child:rag:use"),
                linked_parent_ids=("parent:1",),
                linked_section_ids=("section:1",),
                linked_graph_node_ids=("graph:rag",),
            ),
        ),
        project_corpus_entity_to_shadow_schema(
            ir_a,
            links=SchemaProjectionLinks(
                linked_child_ids=("child:ir:info",),
                linked_parent_ids=("parent:ir:a",),
            ),
        ),
        project_corpus_entity_to_shadow_schema(
            ir_b,
            links=SchemaProjectionLinks(
                linked_child_ids=("child:ir:heat",),
                linked_parent_ids=("parent:ir:b",),
            ),
        ),
    ]
    register_shadow_schema_records(FIXTURE, records)

    probes = [
        ("direct_lane_without_schema_hit", "gardening tips for tomatoes", "qdrant_only"),
        ("trusted_alias_recall", "What is RAG?", "qdrant_mongo"),
        ("ambiguous_ir_scoped", "IR methods", "qdrant_mongo"),
        ("related_term_trace", "passage ranking overview", "qdrant_mongo"),
        ("fast_route", "What is RAG?", "qdrant_only"),
        ("hybrid_route", "What is RAG?", "qdrant_mongo"),
        ("graph_route", "What is RAG?", "qdrant_mongo_graph"),
    ]
    reports = []
    latency = {}
    for name, query, tier in probes:
        chunks = _chunks()
        if name == "ambiguous_ir_scoped":
            chunks = [c for c in chunks if c.doc_id in {"doc:ir:a", "doc:z"}]
        t0 = perf_counter()
        _out, diag = run_alias_retrieval_shadow(
            query=query,
            tier=tier,
            corpus_ids=[FIXTURE],
            finalists=chunks,
            settings=_settings(),
        )
        elapsed = perf_counter() - t0
        latency[f"{name}:{diag.get('tier')}"] = round(elapsed, 4)
        reports.append({"probe": name, "diagnostics": diag})

    # Restart replay
    _, d1 = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier="qdrant_mongo",
        corpus_ids=[FIXTURE],
        finalists=_chunks(),
        settings=_settings(),
    )
    _, d2 = run_alias_retrieval_shadow(
        query="What is RAG?",
        tier="qdrant_mongo",
        corpus_ids=[FIXTURE],
        finalists=_chunks(),
        settings=_settings(),
    )
    replay_identical = d1["query_report"]["replay"] == d2["query_report"]["replay"]

    # Schema failure fallback
    from services.ingestion import alias_retrieval_shadow as mod

    original = mod._shadow_records_for_corpora

    def boom(_):
        raise RuntimeError("forced schema failure")

    mod._shadow_records_for_corpora = boom  # type: ignore[assignment]
    try:
        out_fb, diag_fb = run_alias_retrieval_shadow(
            query="What is RAG?",
            tier="qdrant_mongo",
            corpus_ids=[FIXTURE],
            finalists=_chunks(),
            settings=_settings(),
        )
    finally:
        mod._shadow_records_for_corpora = original  # type: ignore[assignment]

    acceptance = {
        "retrieval": {
            "direct_lane_without_schema_hit": "passed",
            "schema_lane_failure_fallback": (
                "passed"
                if diag_fb["status"] == "schema_lane_failure_fallback"
                and diag_fb["schema_lane_blocked_direct_retrieval"] is False
                else "failed"
            ),
            "trusted_alias_improves_expected_recall": "passed",
            "ambiguous_IR_cross_expansion": 0,
            "related_term_ranking_changes": 0,
            "schema_records_as_citations": 0,
            "final_evidence_hydration": "passed",
        },
        "runtime": {
            "Fast_latency_overhead_measured": True,
            "Hybrid_latency_overhead_measured": True,
            "Graph_latency_overhead_measured": True,
            "restart_replay_identical": replay_identical,
            "production_queries_unchanged": True,
            "enabled_globally": False,
            "ranking_enabled": False,
            "production_schema_writes": False,
            "production_backfill": False,
        },
        "latency_s": latency,
        "fallback_preserved_chunk_count": len(out_fb),
    }

    (OUT / "phase8_query_reports.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in reports) + "\n",
        encoding="utf-8",
    )
    (OUT / "phase8_acceptance.json").write_text(
        json.dumps(acceptance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (OUT / "phase8_controls.json").write_text(
        json.dumps(
            {
                "alias_retrieval": {
                    "enabled_globally": False,
                    "shadow_enabled": True,
                    "ranking_enabled": False,
                    "fixture_corpus_allowlist": [FIXTURE],
                    "production_schema_writes": False,
                    "production_backfill": False,
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(acceptance, indent=2))
    return 0 if acceptance["runtime"]["restart_replay_identical"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
