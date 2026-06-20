"""Phase 3 (GERG) — query-ranked typed-edge decoration. Asserting e2e.

The facts>=3 gate skipped typed-edge decoration in the prod regime (facts=12),
and when it ran, edges were ranked by raw confidence DESC — surfacing catalog
noise (OpenAI--works_for-->Sam Altman, machine learning--uses-->JavaScript)
instead of query-relevant edges. GERG un-gates decoration and QUERY-RANKS the
edges: a SUBJECT MATCH to a non-generic query concept is REQUIRED (a
definitional predicate is a bonus on top, never a standalone pass), so only
query-relevant typed relations survive — and when the graph has none, the
decoration is correctly EMPTY rather than padded with tangential noise.

IMPORTANT HONEST FINDING (this test encodes it): for "what is nlp ...", the
typed graph holds NO nlp-subject RELATES_TO edges among the winners — the
earlier "graph holds 8/8 on-topic edges" claim did NOT reproduce. So GERG's
value on THIS query is removing the tangential ML/OpenAI noise the un-gated
confidence ranking would inject (correctly absent), NOT beating hybrid. GERG
surfaces real edges only when the subject entity actually has typed structure
(proved live on a 'machine learning' query).

Proofs:
  S — DETERMINISTIC mechanism: query-ranking drops high-confidence off-topic
      edges and keeps subject-matching ones; confidence-DESC surfaces the noise.
  L — LIVE no-noise: every GERG edge on the real nlp query is subject-relevant
      (or empty); the OLD confidence path injected off-topic edges GERG removes.
  V — LIVE value: on a query whose subject HAS typed structure, GERG returns
      non-empty, all-subject-relevant edges.

Run:
  docker cp services/retriever/graph_decoration.py polymath_v33-backend-1:/app/services/retriever/graph_decoration.py
  docker cp /tmp/assert_phase3.py polymath_v33-backend-1:/app/_assert_phase3.py
  docker exec -w /app polymath_v33-backend-1 python _assert_phase3.py
"""
import asyncio
import os
import sys

from motor.motor_asyncio import AsyncIOMotorClient

CID = os.environ.get("E2E_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
QUERY = os.environ.get("E2E_QUERY", "what is nlp and how does it assist in model fine tuning")
VALUE_QUERY = os.environ.get("E2E_VALUE_QUERY", "what is machine learning and supervised learning")

_fail: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        _fail.append(name)


async def main() -> None:
    from services.ingestion_service import ingestion_service
    from services.conversation import conversation_service
    from services.retriever import retriever_orchestrator
    from services.retriever.graph_decoration import (
        graph_decorator, _query_rank_rows, _edge_query_relevance,
    )
    from services.retriever.query_grounding import concept_groups
    from models.schemas import RetrievalTier

    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    await ingestion_service.connect(db)
    try:
        await conversation_service.connect()
    except Exception as exc:
        print(f"[info] conversation_service.connect: {exc}", flush=True)
    if getattr(graph_decorator, "_driver", None) is None:
        graph_decorator._driver = ingestion_service.neo4j_driver

    # ── S. DETERMINISTIC mechanism proof ──────────────────────────────────
    rows = [
        {"seed_entity": "NLP", "neighbor_entity": "tokenization", "predicate": "uses", "edge_weight": 0.50},
        {"seed_entity": "machine learning", "neighbor_entity": "JavaScript", "predicate": "uses", "edge_weight": 0.99},
        {"seed_entity": "transformer architecture", "neighbor_entity": "natural language processing", "predicate": "part_of", "edge_weight": 0.40},
        {"seed_entity": "OpenAI", "neighbor_entity": "Sam Altman", "predicate": "works_for", "edge_weight": 0.95},
    ]
    ranked = _query_rank_rows(rows, QUERY, 8)
    kept = {(r["seed_entity"], r["neighbor_entity"]) for r in ranked}
    check("S1_query_rank_keeps_only_subject_edges",
          kept == {("NLP", "tokenization"), ("transformer architecture", "natural language processing")},
          f"kept={kept}")
    conf_top2 = {r["neighbor_entity"] for r in sorted(rows, key=lambda r: r["edge_weight"], reverse=True)[:2]}
    check("S2_confidence_would_surface_noise",
          {"Sam Altman", "JavaScript"} & conf_top2 == {"Sam Altman", "JavaScript"},
          f"confidence top-2 neighbors={conf_top2}")

    # ── L. LIVE no-noise / correctly-absent on the real nlp query ─────────
    g = await retriever_orchestrator.retrieve(
        QUERY, [CID], RetrievalTier.qdrant_mongo_graph,
        collections=None, final_top_k=8, rerank_enabled=True,
    )
    winners = g.chunks or []
    groups = concept_groups(QUERY)
    decs_A = await graph_decorator.decorate_winners(winning_chunks=winners, corpus_ids=[CID], neighbor_limit=8, query=None)
    decs_B = await graph_decorator.decorate_winners(winning_chunks=winners, corpus_ids=[CID], neighbor_limit=8, query=QUERY)

    b_bad = [(d.seed_entity, d.predicate, d.neighbor_entity) for d in decs_B
             if _edge_query_relevance(d.seed_entity, d.neighbor_entity, d.predicate, groups) < 1]
    a_offtopic = [(d.seed_entity, d.predicate, d.neighbor_entity) for d in decs_A
                  if _edge_query_relevance(d.seed_entity, d.neighbor_entity, d.predicate, groups) < 1]
    check("L1_gerg_has_no_offtopic_noise", not b_bad,
          f"GERG off-topic survivors: {b_bad[:4]} (decs_B size={len(decs_B)})")
    check("L2_gerg_removes_confidence_noise", len(a_offtopic) > 0 and len(decs_B) < len(decs_A),
          f"confidence path off-topic={len(a_offtopic)}/{len(decs_A)}; GERG kept={len(decs_B)} (graph lacks nlp structure -> correctly absent)")

    # ── V. LIVE value on a subject that HAS typed structure ───────────────
    gv = await retriever_orchestrator.retrieve(
        VALUE_QUERY, [CID], RetrievalTier.qdrant_mongo_graph,
        collections=None, final_top_k=8, rerank_enabled=True,
    )
    vgroups = concept_groups(VALUE_QUERY)
    decs_V = await graph_decorator.decorate_winners(
        winning_chunks=gv.chunks or [], corpus_ids=[CID], neighbor_limit=8, query=VALUE_QUERY)
    v_bad = [(d.seed_entity, d.predicate, d.neighbor_entity) for d in decs_V
             if _edge_query_relevance(d.seed_entity, d.neighbor_entity, d.predicate, vgroups) < 1]
    check("V1_value_query_nonempty", len(decs_V) > 0,
          f"{len(decs_V)} query-ranked edges for {VALUE_QUERY!r}")
    check("V2_value_query_all_relevant", not v_bad, f"off-topic: {v_bad[:4]}")

    print(f"\n  nlp query: confidence kept {len(decs_A)} (off-topic {len(a_offtopic)}) -> GERG kept {len(decs_B)} (correctly absent)", flush=True)
    print(f"  value query {VALUE_QUERY!r}: GERG kept {len(decs_V)} subject-relevant edges:", flush=True)
    for d in decs_V[:6]:
        ev = (d.edge_evidence or "")[:44]
        print(f"    {d.seed_entity[:26]} --{d.predicate}--> {d.neighbor_entity[:26]}" + (f'  "{ev}"' if ev else ""), flush=True)

    print(f"\n{'=== ALL PASS ===' if not _fail else '=== FAILURES: ' + ', '.join(_fail) + ' ==='}", flush=True)
    sys.exit(1 if _fail else 0)


asyncio.run(main())
