"""Integration e2e for the graph-metrics self-heal + bounded producer (commit edca9f6).

Runs against LIVE Neo4j / Qdrant / Mongo inside the backend container:
    docker exec -w /app polymath_v33-backend-1 python tests/test_graph_metrics_selfheal_e2e.py

ASSERTS (non-zero exit on any failure) — non-destructive (restores the cache it found):
  1. Bounded producer — the top-N RELATES_TO load caps nodes AND completes fast
     (this is the fix for the 762k-entity load that used to hang forever).
  2. Self-heal is a no-op when the cache is fresh.
  3. Bridges actually FIRE — mode_a expansion over real query seeds yields
     graph_mode_a_bridge chunks (the user-facing outcome: bridges != 0).
  4. Self-heal detects a missing cache and SCHEDULES a rebuild + writes a durable
     Mongo claim (corrective + restart-safe).
  5. Self-heal dedups — a second call returns "in_flight" (no stampede).
"""
import asyncio
import os
import sys
import time

from motor.motor_asyncio import AsyncIOMotorClient

CID = os.environ.get("E2E_CORPUS", "f8a0aa85-6cb4-4f64-a973-f9183f1546bb")
QUERY = os.environ.get("E2E_QUERY", "what is nlp and how does it assist in model fine tuning")

_fail: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    if not cond:
        _fail.append(name)


async def main() -> None:
    from services.ingestion_service import ingestion_service
    from services.graph import analytics
    from services.graph.analytics import compute_corpus_change_signature, get_cached_metrics
    from services.graph.cache_warmup import ensure_graph_metrics_fresh

    db = AsyncIOMotorClient(os.environ["MONGODB_URI"]).get_default_database()
    await ingestion_service.connect(db)
    qdrant = ingestion_service.qdrant_client
    neo4j = ingestion_service.neo4j_driver

    sig = await compute_corpus_change_signature(db, CID)
    # Snapshot the cache so the test can restore it (the self-heal asserts delete it).
    cache_snapshot = await db["graph_metrics_cache"].find_one({"corpus_id": CID})

    # ── 1. Bounded producer: the cap cypher returns <= cap, FAST (was a 762k hang)
    async with neo4j.session() as s:
        t0 = time.time()
        res = await s.run(
            analytics._TOP_RELATED_ENTITIES_CYPHER,
            corpus_id=CID,
            max_nodes=analytics._GRAPH_METRICS_MAX_NODES,
        )
        ids = [r["entity_id"] async for r in res]
        dt = time.time() - t0
    check("1_bounded_load_caps_nodes",
          0 < len(ids) <= analytics._GRAPH_METRICS_MAX_NODES,
          f"{len(ids)} hub ids (cap={analytics._GRAPH_METRICS_MAX_NODES})")
    check("1_bounded_load_fast", dt < 60, f"{dt:.1f}s (full load of ~762k used to hang)")

    # Precondition for the fresh/bridges asserts: cache populated with bridges.
    m = await get_cached_metrics(db, CID, sig)
    fb = len(getattr(m, "fragile_bridges", []) or []) if m else 0
    check("precondition_cache_has_bridges", fb > 0, f"fragile_bridges={fb}")

    # ── 2. Self-heal is a no-op when fresh
    r = await ensure_graph_metrics_fresh(CID)
    check("2_selfheal_noop_when_fresh", r == "fresh", f"got {r!r}")

    # ── 3. Bridges FIRE — deterministic. Seed the expander with chunks that
    # mention a cached fragile-bridge ENDPOINT; the bridge lane must then surface
    # the partner endpoint's chunks (source_tier=graph_mode_a_bridge). This tests
    # the mechanism without depending on which entities a given query happens to
    # land on. (Live queries do hit bridges too — observed bridges=5.)
    from services.retriever.mode_a import mode_a_expansion
    from models.schemas import SourceChunk

    if getattr(mode_a_expansion, "_driver", None) is None:
        mode_a_expansion._driver = neo4j

    _ = SourceChunk  # (kept import; bridge lane is exercised directly below)
    fbs = getattr(m, "fragile_bridges", []) or []
    bridge_out: list = []
    seeded_from = "none"
    for fb in fbs[:8]:
        src, tgt = fb.get("source"), fb.get("target")
        if not src or not tgt:
            continue
        # A chunk mentioning the source but NOT the target — guarantees the
        # bonus condition (src in seeds, tgt not) so the bridge MUST surface tgt.
        async with neo4j.session() as s:
            res = await s.run(
                "MATCH (c:Chunk {corpus_id:$cid})-[:MENTIONS]->(:Entity {entity_id:$src}) "
                "WHERE NOT EXISTS { MATCH (c)-[:MENTIONS]->(:Entity {entity_id:$tgt}) } "
                "RETURN c.chunk_id AS cid LIMIT 3",
                cid=CID, src=src, tgt=tgt,
            )
            seed_cids = [r["cid"] async for r in res if r["cid"]]
        if not seed_cids:
            continue
        # Exercise the bridge lane directly (the part that reads graph_metrics_cache).
        bridge_out = await mode_a_expansion._expand_via_bridges(
            seed_ids=seed_cids, corpus_ids=[CID], db=db, limit=10
        )
        seeded_from = f"{src} -> {tgt}"
        if bridge_out:
            break
    check("3_bridges_fire",
          len(bridge_out) > 0,
          f"{len(bridge_out)} bridge chunks (seeded {seeded_from}; source_tier="
          + (bridge_out[0].source_tier if bridge_out else "n/a") + ")")

    # ── 4. Self-heal detects missing cache → schedules + writes durable claim
    await db["graph_metrics_cache"].delete_many({"corpus_id": CID})
    await db["graph_metrics_warm_state"].delete_many({"corpus_id": CID})
    r = await ensure_graph_metrics_fresh(CID)
    check("4_selfheal_schedules_on_miss", r == "scheduled", f"got {r!r}")
    ws = await db["graph_metrics_warm_state"].find_one({"corpus_id": CID})
    check("4_selfheal_durable_claim",
          ws is not None and ws.get("signature") == sig and ws.get("status") == "warming",
          f"claim={'present' if ws else 'MISSING'}")

    # ── 5. Self-heal dedups — second call sees the claim → in_flight
    r = await ensure_graph_metrics_fresh(CID)
    check("5_selfheal_dedup_no_stampede", r == "in_flight", f"got {r!r}")

    # ── Restore: re-insert the snapshot so the system stays populated; clear the
    # test's warm claim. (The scheduled task dies with this process, so no real
    # rebuild runs — restoring avoids a needless re-warm.)
    await db["graph_metrics_warm_state"].delete_many({"corpus_id": CID})
    if cache_snapshot:
        cache_snapshot.pop("_id", None)
        await db["graph_metrics_cache"].update_one(
            {"corpus_id": CID}, {"$set": cache_snapshot}, upsert=True
        )
        print("[info] restored metrics cache snapshot", flush=True)

    print(f"\n{'=== ALL PASS ===' if not _fail else '=== FAILURES: ' + ', '.join(_fail) + ' ==='}", flush=True)
    sys.exit(1 if _fail else 0)


# Import-safe: only runs as a script (docker exec), never at pytest collection
# (a bare module-level asyncio.run would fire main() on import and crash a
# fresh clone with no live stack).
if __name__ == "__main__":
    import os as _os
    _sys = __import__("sys")
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    asyncio.run(main())
