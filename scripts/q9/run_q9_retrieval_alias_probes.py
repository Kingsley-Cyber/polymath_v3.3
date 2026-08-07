#!/usr/bin/env python3
"""q9 steps 13–15: Fast/Hybrid/Graph probes + alias shadow traces + latency."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path("/app") if Path("/app/services").is_dir() else Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(ROOT))

from models.schemas import RetrievalTier  # noqa: E402
from services.retriever import retriever_orchestrator  # noqa: E402
from services.retriever.query_plan import build_query_plan_v2  # noqa: E402

CORPUS_ID = os.environ.get("Q9_CORPUS_ID", "6a766597-29f3-4a3e-8918-5de10f0053b3")
OUT = Path(os.environ.get("Q9_OUT", "/tmp/q9_probes"))
OUT.mkdir(parents=True, exist_ok=True)

QUERIES = [
    "What is RAG?",
    "What is Retrieval-Augmented Generation?",
    "IBM history of disk drives",
    "Information Retrieval ranking",
    "Infrared heat signatures",
    "Apple Inc consumer electronics",
    "apple fruit produce",
    "Microsoft software company",
    "Benesh Movement Notation",
    "movement writing without Benesh evidence",
]


def _tier_enum(name: str) -> RetrievalTier:
    return {
        "fast": RetrievalTier.qdrant_only,
        "hybrid": RetrievalTier.qdrant_mongo,
        "graph": RetrievalTier.qdrant_mongo_graph,
    }[name]


async def _one(query: str, tier_name: str) -> dict:
    tier = _tier_enum(tier_name)
    plan = build_query_plan_v2(query)
    t0 = time.perf_counter()
    result = await retriever_orchestrator.retrieve_planned(
        plan=plan,
        corpus_ids=[CORPUS_ID],
        retrieval_tier=tier,
        collections=None,
    )
    elapsed = time.perf_counter() - t0
    diag = dict(result.diagnostics or {})
    alias = diag.get("alias_retrieval") or {}
    report = alias.get("query_report") or {}
    chunks = list(result.chunks or [])
    return {
        "query": query,
        "tier": tier_name,
        "effective_tier": str(getattr(result.effective_tier, "value", result.effective_tier)),
        "latency_s": round(elapsed, 4),
        "final_count": len(chunks),
        "direct_result_ids": [c.chunk_id for c in chunks],
        "final_hydrated_chunk_ids": [c.chunk_id for c in chunks],
        "alias_status": alias.get("status"),
        "schema_matches": [
            {
                "schema_point_id": t.get("schema_point_id"),
                "corpus_entity_id": t.get("corpus_entity_id"),
                "matched_surface": t.get("matched_surface"),
                "trust_class": t.get("trust_class"),
                "expanded_query": t.get("expanded_query"),
                "linked_child_ids": t.get("linked_child_ids"),
                "ranking_contribution": t.get("ranking_contribution"),
            }
            for t in (report.get("schema_traces") or [])
        ],
        "expanded_queries": report.get("expanded_queries"),
        "linked_anchors": {
            "parents": report.get("linked_parent_summary_ids"),
            "sections": report.get("linked_section_summary_ids"),
            "graph_nodes": report.get("linked_graph_node_ids"),
        },
        "ranking_effect": alias.get("ranking_effect"),
        "alias_lane_latency_s": (alias.get("latency_s") or {}).get("alias_shadow_lane"),
        "schema_records_as_citations": alias.get("schema_records_as_citations"),
        "timings_s": diag.get("timings_s"),
        "graph_evidence": diag.get("graph_evidence"),
        "store_contract": diag.get("store_contract"),
    }


async def _bootstrap_alias_shadow(database, corpus_id: str) -> dict:
    """Mine → gate → cluster → project → register shadow schemas for this corpus."""

    from collections import defaultdict

    from services.ingestion.alias_candidates import collect_alias_candidates
    from services.ingestion.alias_corpus_clustering import (
        build_inventory,
        cluster_corpus_entities,
    )
    from services.ingestion.alias_document_clustering import (
        cluster_from_candidates_and_decisions,
    )
    from services.ingestion.alias_gate import run_alias_gate
    from services.ingestion.alias_retrieval_shadow import (
        clear_shadow_schema_registry,
        register_shadow_schema_records,
    )
    from services.ingestion.alias_schema_projection import (
        SchemaProjectionLinks,
        project_corpus_entities_to_shadow_schemas,
    )
    from services.ingestion.enrich import schwartz_hearst_matches

    chunks = []
    async for doc in database.chunks.find(
        {"corpus_id": corpus_id},
        {"chunk_id": 1, "parent_id": 1, "doc_id": 1, "text": 1},
    ):
        text = str(doc.get("text") or "").strip()
        if text:
            chunks.append(
                {
                    "chunk_id": str(doc.get("chunk_id") or doc["_id"]),
                    "parent_id": str(doc.get("parent_id") or "") or None,
                    "doc_id": str(doc.get("doc_id") or ""),
                    "text": text,
                }
            )

    candidates = []
    for row in chunks:
        ents = []
        for m in schwartz_hearst_matches(row["text"]):
            ents.append({"canonical_name": m["long_form"], "surface_form": m["long_form"]})
            ents.append({"canonical_name": m["short"], "surface_form": m["short"]})
        for surface in (
            "Apple Inc.",
            "Apple",
            "apple fruit",
            "Microsoft",
            "Benesh Movement Notation",
            "movement writing",
            "Infrared",
            "Information Retrieval",
        ):
            if surface.lower() in row["text"].lower():
                ents.append({"canonical_name": surface, "surface_form": surface})
        batch = collect_alias_candidates(
            row["text"],
            ents,
            document_id=row["doc_id"],
            chunk_id=row["chunk_id"],
        )
        candidates.extend(batch.candidates)

    gate = run_alias_gate(candidates)
    decisions = list(gate.decisions)
    parent_id_by_chunk = {
        r["chunk_id"]: (r.get("parent_id") or r["chunk_id"]) for r in chunks
    }
    doc_batch = cluster_from_candidates_and_decisions(
        candidates,
        decisions,
        parent_id_by_chunk=parent_id_by_chunk,
        child_text_by_chunk={r["chunk_id"]: r["text"] for r in chunks},
    )
    cand_by_id = {c.alias_candidate_id: c for c in candidates}
    inventories = []
    for ent in doc_batch.entities:
        trusted = []
        acronym_pairs = []
        for aid in ent.accepted_alias_ids:
            c = cand_by_id.get(aid)
            d = next((x for x in decisions if x.alias_candidate_id == aid), None)
            if not c or not d:
                continue
            if d.decision in {"ACCEPT_IDENTITY", "ACCEPT_TEMPORAL_IDENTITY"}:
                if c.candidate_surface.lower() != ent.canonical_name.lower():
                    trusted.append(c.candidate_surface)
                if c.canonical_surface.lower() != ent.canonical_name.lower():
                    trusted.append(c.canonical_surface)
                if c.candidate_type in {"acronym_long_form", "explicit_abbreviation"}:
                    short, long_form = c.candidate_surface, c.canonical_surface
                    if len(short) > len(long_form):
                        short, long_form = long_form, short
                    acronym_pairs.append((short, long_form))
        inventories.append(
            build_inventory(
                ent,
                trusted_alias_surfaces=trusted,
                acronym_pairs=acronym_pairs,
                supporting_alias_decision_ids=list(ent.accepted_alias_ids),
            )
        )
    corpus_batch = cluster_corpus_entities(inventories, corpus_id=corpus_id)
    children_by_doc = defaultdict(list)
    parents_by_doc = defaultdict(list)
    for row in chunks:
        children_by_doc[row["doc_id"]].append(row["chunk_id"])
        if row.get("parent_id"):
            parents_by_doc[row["doc_id"]].append(row["parent_id"])
    links = {}
    for ent in corpus_batch.corpus_entities:
        cids, pids = [], []
        for did in ent.source_document_ids:
            cids.extend(children_by_doc.get(did, [])[:8])
            pids.extend(parents_by_doc.get(did, [])[:4])
        links[ent.corpus_entity_id] = SchemaProjectionLinks(
            linked_child_ids=tuple(dict.fromkeys(cids)),
            linked_parent_ids=tuple(dict.fromkeys(pids)),
        )
    projection = project_corpus_entities_to_shadow_schemas(
        corpus_batch.corpus_entities, links_by_entity_id=links
    )
    clear_shadow_schema_registry()
    register_shadow_schema_records(corpus_id, projection.records)
    return {
        "chunk_count": len(chunks),
        "candidate_count": len(candidates),
        "corpus_entity_count": len(corpus_batch.corpus_entities),
        "shadow_records": len(projection.records),
        "production_schema_mutations": projection.production_schema_mutations,
    }


async def main() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient

    from config import get_settings
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service

    settings = get_settings()
    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    database = client[settings.MONGODB_DATABASE]
    await ingestion_service.connect(database)
    conversation_service._db = database

    alias_bootstrap = await _bootstrap_alias_shadow(database, CORPUS_ID)
    print("ALIAS_BOOTSTRAP", json.dumps(alias_bootstrap))

    rows = []
    # Cold then warm for first query × each tier
    latency = {"cold": {}, "warm": {}}
    for tier in ("fast", "hybrid", "graph"):
        cold = await _one(QUERIES[0], tier)
        warm = await _one(QUERIES[0], tier)
        latency["cold"][tier] = cold["latency_s"]
        latency["warm"][tier] = warm["latency_s"]
        rows.append(cold)
        rows.append({**warm, "warm_repeat": True})

    for query in QUERIES[1:]:
        for tier in ("fast", "hybrid", "graph"):
            rows.append(await _one(query, tier))

    # Graph stage trace emphasis
    graph_rows = [r for r in rows if r["tier"] == "graph" and not r.get("warm_repeat")]
    acceptance = {
        "corpus_id": CORPUS_ID,
        "alias_bootstrap": alias_bootstrap,
        "probe_count": len(rows),
        "latency": latency,
        "alias_shadow_present": all(r.get("alias_status") for r in rows),
        "schema_match_probes": sum(1 for r in rows if r.get("schema_matches")),
        "schema_records_as_citations_total": sum(
            int(r.get("schema_records_as_citations") or 0) for r in rows
        ),
        "ranking_unchanged_count": sum(
            1
            for r in rows
            if (r.get("ranking_effect") or {}).get("production_queries_unchanged") is True
            or (r.get("ranking_effect") or {}).get("applied") is False
            or r.get("ranking_effect") is None
        ),
        "graph_probes": len(graph_rows),
        "graph_with_facts": sum(
            1
            for r in graph_rows
            if ((r.get("graph_evidence") or {}).get("facts_used") or 0) > 0
        ),
    }
    (OUT / "q9_retrieval_alias_probes.jsonl").write_text(
        "\n".join(json.dumps(r, sort_keys=True) for r in rows) + "\n"
    )
    (OUT / "q9_retrieval_acceptance.json").write_text(
        json.dumps(acceptance, indent=2, sort_keys=True) + "\n"
    )
    print(json.dumps(acceptance, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
