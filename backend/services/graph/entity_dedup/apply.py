"""Entity dedup APPLY + UNDO — mutates Neo4j, exactly reversible.

Edge topology (verified on the live graph):
  (Chunk)-[:MENTIONS]->(Entity)        incoming to entity
  (Entity)-[:RELATES_TO]->(Entity)     both directions exist
  (Entity)-[:HAS_FACT]->(Fact)         outgoing
  SUPPORTS_FACT is Fact<->Chunk and does NOT touch entities — untouched here.

A merge (dup D -> survivor S):
  1. SNAPSHOT D: all its properties + every MENTIONS/RELATES_TO(in,out,self)/
     HAS_FACT edge with the other endpoint's stable id and the edge properties.
     Stored in Mongo graph_entity_dedup_merge_log keyed by (merge_run, dup_id).
  2. RE-POINT each D edge onto S. Parallel RELATES_TO collapse by predicate
     (survivor's edge kept; dup's dropped). EVERY edge we create carries
     {merge_run, merged_from} markers so undo can find exactly what we made.
  3. TOMBSTONE: create (:Entity {entity_id:'tombstone:'+D, original_entity_id:D,
     merged_into:S, tombstone:true}) and DETACH DELETE D. (entity_id is UNIQUE;
     the tombstone uses a distinct id so it never collides, and read-path
     resolution follows original_entity_id -> merged_into.)

UNDO (per merged dup, from the Mongo audit):
  delete S's edges tagged with this (merge_run, merged_from); delete the
  tombstone; recreate D from the snapshot props; recreate all of D's original
  edges. S's pre-existing edges are never touched, so the round-trip is exact.

--selftest applies then immediately undoes N proposals and asserts the global
node/edge counts return to baseline. This is the reversibility GATE: it must be
green before any real apply. The selftest uses the safest same-type proposals.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from config import get_settings

MERGE_LOG = "graph_entity_dedup_merge_log"
PREVIEW = "graph_entity_dedup_preview"
ENTITY_DEDUP_WRITE_BATCH_SIZE = 100


def _A(s: Any, *names: str, default: Any = None) -> Any:
    for n in names:
        v = getattr(s, n, None)
        if v:
            return v
    return default


def _driver():
    s = get_settings()
    from neo4j import AsyncGraphDatabase

    return AsyncGraphDatabase.driver(
        _A(s, "NEO4J_URI", "NEO4J_URL"),
        auth=(
            _A(s, "NEO4J_USER", "NEO4J_USERNAME", default="neo4j"),
            _A(s, "NEO4J_PASSWORD", "NEO4J_PASS"),
        ),
    )


def _mongo():
    s = get_settings()
    from motor.motor_asyncio import AsyncIOMotorClient

    mc = AsyncIOMotorClient(_A(s, "MONGODB_URI", "MONGODB_URL"))
    return mc, mc[_A(s, "MONGODB_DB", default="polymath")]


# ── baseline counts (reversibility invariant) ───────────────────────────────
async def baseline_counts(sess) -> dict:
    q = """
    CALL { MATCH (e:Entity) WHERE coalesce(e.tombstone,false)=false RETURN count(e) AS live_entities }
    CALL { MATCH (t:Entity) WHERE coalesce(t.tombstone,false)=true RETURN count(t) AS tombstones }
    CALL { MATCH ()-[r:RELATES_TO]->() RETURN count(r) AS relates_to }
    CALL { MATCH ()-[r:MENTIONS]->() RETURN count(r) AS mentions }
    CALL { MATCH ()-[r:HAS_FACT]->() RETURN count(r) AS has_fact }
    RETURN live_entities, tombstones, relates_to, mentions, has_fact
    """
    r = await (await sess.run(q)).single()
    return dict(r)


# ── snapshot (read-only) ─────────────────────────────────────────────────────
_SNAPSHOT = """
MATCH (d:Entity {entity_id:$did})
OPTIONAL MATCH (c:Chunk)-[m:MENTIONS]->(d)
WITH d, collect({chunk:c.chunk_id, corpus_id:c.corpus_id, props:properties(m)}) AS mentions
OPTIONAL MATCH (d)-[ro:RELATES_TO]->(x:Entity) WHERE x.entity_id <> $did
WITH d, mentions, collect({other:x.entity_id, props:properties(ro)}) AS rel_out
OPTIONAL MATCH (y:Entity)-[ri:RELATES_TO]->(d) WHERE y.entity_id <> $did
WITH d, mentions, rel_out, collect({other:y.entity_id, props:properties(ri)}) AS rel_in
OPTIONAL MATCH (d)-[sl:RELATES_TO]->(d)
WITH d, mentions, rel_out, rel_in, collect({props:properties(sl)}) AS self_loops
OPTIONAL MATCH (d)-[hf:HAS_FACT]->(f:Fact)
WITH d, mentions, rel_out, rel_in, self_loops, collect({fact:f.fact_id, corpus_id:f.corpus_id, props:properties(hf)}) AS facts
RETURN properties(d) AS dprops, mentions, rel_out, rel_in, self_loops, facts
"""


async def _snapshot(sess, dup_id: str) -> dict | None:
    rec = await (await sess.run(_SNAPSHOT, did=dup_id)).single()
    if not rec or not rec["dprops"]:
        return None
    return {
        "dup_id": dup_id,
        "dprops": dict(rec["dprops"]),
        "mentions": [dict(m) for m in rec["mentions"] if m.get("chunk")],
        "rel_out": [dict(x) for x in rec["rel_out"] if x.get("other")],
        "rel_in": [dict(x) for x in rec["rel_in"] if x.get("other")],
        # OPTIONAL MATCH + collect yields [{props: null}] (not []) when no
        # self-loop exists; drop those so undo never does SET r = null.
        "self_loops": [dict(x) for x in rec["self_loops"] if x.get("props")],
        "facts": [dict(x) for x in rec["facts"] if x.get("fact")],
    }


# ── re-point queries (each tags created edges with run+merged_from) ──────────
_REPOINT = [
    # incoming MENTIONS: (c)-[:MENTIONS]->(d)  =>  (c)-[:MENTIONS]->(s)
    """
    MATCH (c:Chunk)-[m:MENTIONS]->(d:Entity {entity_id:$did})
    MATCH (s:Entity {entity_id:$sid})
    WITH c, m, s LIMIT $batch_size
    MERGE (c)-[m2:MENTIONS]->(s)
      ON CREATE SET m2 = properties(m), m2.merge_run=$run, m2.merged_from=$did
    DELETE m
    RETURN count(m) AS changed
    """,
    # outgoing RELATES_TO: (d)->(x)  =>  (s)->(x), collapse by predicate
    """
    MATCH (d:Entity {entity_id:$did})-[ro:RELATES_TO]->(x:Entity)
    WHERE x.entity_id <> $sid AND x.entity_id <> $did
    MATCH (s:Entity {entity_id:$sid})
    WITH d, ro, x, s LIMIT $batch_size
    MERGE (s)-[r2:RELATES_TO {predicate: coalesce(ro.predicate,'')}]->(x)
      ON CREATE SET r2 = properties(ro), r2.merge_run=$run, r2.merged_from=$did
    DELETE ro
    RETURN count(ro) AS changed
    """,
    # incoming RELATES_TO: (y)->(d)  =>  (y)->(s)
    """
    MATCH (y:Entity)-[ri:RELATES_TO]->(d:Entity {entity_id:$did})
    WHERE y.entity_id <> $sid AND y.entity_id <> $did
    MATCH (s:Entity {entity_id:$sid})
    WITH y, ri, d, s LIMIT $batch_size
    MERGE (y)-[r2:RELATES_TO {predicate: coalesce(ri.predicate,'')}]->(s)
      ON CREATE SET r2 = properties(ri), r2.merge_run=$run, r2.merged_from=$did
    DELETE ri
    RETURN count(ri) AS changed
    """,
    # HAS_FACT: (d)->(f)  =>  (s)->(f)
    """
    MATCH (d:Entity {entity_id:$did})-[hf:HAS_FACT]->(f:Fact)
    MATCH (s:Entity {entity_id:$sid})
    WITH d, hf, f, s LIMIT $batch_size
    MERGE (s)-[h2:HAS_FACT]->(f)
      ON CREATE SET h2 = properties(hf), h2.merge_run=$run, h2.merged_from=$did
    DELETE hf
    RETURN count(hf) AS changed
    """,
]

_TOMBSTONE = """
MATCH (d:Entity {entity_id:$did})
CREATE (t:Entity {entity_id:'tombstone:'+$did, original_entity_id:$did,
                  merged_into:$sid, tombstone:true, merge_run:$run,
                  tombstoned_at:$ts})
DETACH DELETE d
"""


async def _apply_one(sess, dup_id: str, sur_id: str, run: str) -> dict | None:
    snap = await _snapshot(sess, dup_id)
    if snap is None:
        return None
    snap["survivor_id"] = sur_id
    snap["merge_run"] = run
    for q in _REPOINT:
        while True:
            result = await sess.run(
                q,
                did=dup_id,
                sid=sur_id,
                run=run,
                batch_size=ENTITY_DEDUP_WRITE_BATCH_SIZE,
            )
            row = await result.single()
            if not row or int(row.get("changed") or 0) == 0:
                break
    await sess.run(
        _TOMBSTONE,
        did=dup_id,
        sid=sur_id,
        run=run,
        ts=datetime.now(timezone.utc).isoformat(),
    )
    return snap


# ── undo (replay snapshot) ───────────────────────────────────────────────────
_UNDO_DELETE_CREATED = """
MATCH (s:Entity {entity_id:$sid})-[r]-()
WHERE r.merge_run=$run AND r.merged_from=$did
WITH r LIMIT $batch_size
DELETE r
RETURN count(r) AS changed
"""
_UNDO_DROP_TOMBSTONE = "MATCH (t:Entity {entity_id:'tombstone:'+$did}) DETACH DELETE t"
_UNDO_RECREATE_NODE = "CREATE (d:Entity) SET d = $dprops"
_UNDO_MENTIONS = """
UNWIND $rows AS row
MATCH (c:Chunk {corpus_id:row.corpus_id, chunk_id:row.chunk})
MATCH (d:Entity {entity_id:$did})
CREATE (c)-[r:MENTIONS]->(d) SET r = row.props
"""
_UNDO_REL_OUT = """
UNWIND $rows AS row
MATCH (d:Entity {entity_id:$did}) MATCH (x:Entity {entity_id:row.other})
CREATE (d)-[r:RELATES_TO]->(x) SET r = row.props
"""
_UNDO_REL_IN = """
UNWIND $rows AS row
MATCH (y:Entity {entity_id:row.other}) MATCH (d:Entity {entity_id:$did})
CREATE (y)-[r:RELATES_TO]->(d) SET r = row.props
"""
_UNDO_SELF = """
UNWIND $rows AS row
MATCH (d:Entity {entity_id:$did})
CREATE (d)-[r:RELATES_TO]->(d) SET r = row.props
"""
_UNDO_FACTS = """
UNWIND $rows AS row
MATCH (d:Entity {entity_id:$did})
MATCH (f:Fact {corpus_id:row.corpus_id, fact_id:row.fact})
CREATE (d)-[r:HAS_FACT]->(f) SET r = row.props
"""


async def _undo_one(sess, snap: dict) -> None:
    did, sid, run = snap["dup_id"], snap["survivor_id"], snap["merge_run"]
    while True:
        result = await sess.run(
            _UNDO_DELETE_CREATED,
            sid=sid,
            did=did,
            run=run,
            batch_size=ENTITY_DEDUP_WRITE_BATCH_SIZE,
        )
        row = await result.single()
        if not row or int(row.get("changed") or 0) == 0:
            break
    await sess.run(_UNDO_DROP_TOMBSTONE, did=did)
    await sess.run(_UNDO_RECREATE_NODE, dprops=snap["dprops"])
    for query, rows in (
        (_UNDO_MENTIONS, snap["mentions"]),
        (_UNDO_REL_OUT, snap["rel_out"]),
        (_UNDO_REL_IN, snap["rel_in"]),
        (_UNDO_SELF, snap["self_loops"]),
        (_UNDO_FACTS, snap["facts"]),
    ):
        for idx in range(0, len(rows), ENTITY_DEDUP_WRITE_BATCH_SIZE):
            await sess.run(
                query,
                did=did,
                rows=rows[idx : idx + ENTITY_DEDUP_WRITE_BATCH_SIZE],
            )


# ── proposal loading ─────────────────────────────────────────────────────────
async def _load_proposals(
    db, corpus_id: str, decisions: set[str], limit: int | None
) -> list[dict]:
    doc = await db[PREVIEW].find_one(
        {"corpus_id": corpus_id}, sort=[("created_at", -1)]
    )
    if not doc:
        raise SystemExit("No preview doc found — run the dry run first.")
    props = [p for p in doc["proposals"] if p["decision"] in decisions]
    # apply low-mention dup first (safest blast radius), deterministic order
    props.sort(key=lambda p: (p["dup_mentions"], p["dup_id"]))
    return props[:limit] if limit else props


# ── selftest (reversibility gate) ────────────────────────────────────────────
async def run_selftest(corpus_id: str, n: int) -> bool:
    mc, db = _mongo()
    drv = _driver()
    ok = False
    try:
        props = await _load_proposals(db, corpus_id, {"auto"}, n)
        async with drv.session() as sess:
            before = await baseline_counts(sess)
            print(f"baseline: {before}")
            snaps = []
            for p in props:
                snap = await _apply_one(sess, p["dup_id"], p["survivor_id"], "selftest")
                if snap:
                    snaps.append(snap)
                    print(f"  applied {p['dup_cn']!r} -> {p['survivor_cn']!r}")
            during = await baseline_counts(sess)
            print(f"after apply ({len(snaps)}): {during}")
            for snap in reversed(snaps):
                await _undo_one(sess, snap)
            after = await baseline_counts(sess)
            print(f"after undo: {after}")
            ok = after == before
            print(f"\nROUND-TRIP EXACT: {ok}")
            if not ok:
                print(
                    "  DIFF:",
                    {k: (before[k], after[k]) for k in before if before[k] != after[k]},
                )
        return ok
    finally:
        await drv.close()
        mc.close()


# ── real apply / undo ────────────────────────────────────────────────────────
async def run_apply(corpus_id: str, decisions: set[str], limit: int | None) -> str:
    mc, db = _mongo()
    drv = _driver()
    run = "dedup-" + uuid.uuid4().hex[:12]
    try:
        props = await _load_proposals(db, corpus_id, decisions, limit)
        await db[MERGE_LOG].insert_one(
            {
                "merge_run": run,
                "corpus_id": corpus_id,
                "kind": "run_header",
                "decisions": sorted(decisions),
                "planned": len(props),
                "created_at": datetime.now(timezone.utc),
            }
        )
        applied = 0
        async with drv.session() as sess:
            for p in props:
                snap = await _apply_one(sess, p["dup_id"], p["survivor_id"], run)
                if snap:
                    await db[MERGE_LOG].insert_one(
                        {**snap, "kind": "merge", "corpus_id": corpus_id}
                    )
                    applied += 1
                    if applied % 500 == 0:
                        print(f"  applied {applied}/{len(props)}")
        cleared = await invalidate_caches(db, corpus_id)
        await db[MERGE_LOG].update_one(
            {"merge_run": run, "kind": "run_header"},
            {
                "$set": {
                    "applied": applied,
                    "caches_cleared": cleared,
                    "finished_at": datetime.now(timezone.utc),
                }
            },
        )
        print(f"APPLIED {applied} merges under run {run}; caches cleared: {cleared}")
        return run
    finally:
        await drv.close()
        mc.close()


async def invalidate_caches(db, corpus_id: str) -> dict:
    """Phase 6 — corpus_change_signature is a hash of doc_ids+updated_at, so it
    does NOT change when entities merge. Merging restructures the graph, so the
    derived caches are stale and must be dropped to force recompute from the
    merged graph on the next read (brain-view self-heals via _kick_cache_rebuild).
    The in-memory brain-view cache (routers/graph) rebuilds on demand."""
    cleared = {}
    for coll in ("graph_metrics_cache", "graph_domain_cache"):
        try:
            r = await db[coll].delete_many({"corpus_id": corpus_id})
            cleared[coll] = r.deleted_count
        except Exception as exc:  # noqa: BLE001
            cleared[coll] = f"error: {exc!r}"
    return cleared


async def run_undo(run: str) -> int:
    mc, db = _mongo()
    drv = _driver()
    try:
        header = await db[MERGE_LOG].find_one({"merge_run": run, "kind": "run_header"})
        snaps = [
            d async for d in db[MERGE_LOG].find({"merge_run": run, "kind": "merge"})
        ]
        n = 0
        async with drv.session() as sess:
            for snap in reversed(snaps):  # reverse application order
                await _undo_one(sess, snap)
                n += 1
        if header and header.get("corpus_id"):
            cleared = await invalidate_caches(db, header["corpus_id"])
            print(f"caches cleared after undo: {cleared}")
        print(f"UNDID {n} merges from run {run}")
        return n
    finally:
        await drv.close()
        mc.close()


# ── cleanup: reverse leftover merges from a crashed run, via markers ─────────
# Recovers when in-memory selftest snapshots were lost. Edge props are restored
# exactly from the markers on the survivor; the dup node's identity props are
# recovered from the preview proposal (cn/pt). Collapsed (unmarked) edges are
# not recoverable, but fragments rarely have any.
_CLEANUP_MOVES = [
    (
        "MATCH (c:Chunk)-[m:MENTIONS {merged_from:$did, merge_run:$run}]->(:Entity {entity_id:$sid}) "
        "MATCH (d:Entity {entity_id:$did}) "
        "WITH c, m, d LIMIT $batch_size "
        "CREATE (c)-[m2:MENTIONS]->(d) "
        "SET m2=properties(m) REMOVE m2.merged_from, m2.merge_run DELETE m "
        "RETURN count(m) AS changed"
    ),
    (
        "MATCH (:Entity {entity_id:$sid})-[r:RELATES_TO {merged_from:$did, merge_run:$run}]->(x) "
        "MATCH (d:Entity {entity_id:$did}) WITH r, x, d LIMIT $batch_size "
        "CREATE (d)-[r2:RELATES_TO]->(x) "
        "SET r2=properties(r) REMOVE r2.merged_from, r2.merge_run DELETE r "
        "RETURN count(r) AS changed"
    ),
    (
        "MATCH (y)-[r:RELATES_TO {merged_from:$did, merge_run:$run}]->(:Entity {entity_id:$sid}) "
        "MATCH (d:Entity {entity_id:$did}) WITH y, r, d LIMIT $batch_size "
        "CREATE (y)-[r2:RELATES_TO]->(d) "
        "SET r2=properties(r) REMOVE r2.merged_from, r2.merge_run DELETE r "
        "RETURN count(r) AS changed"
    ),
    (
        "MATCH (:Entity {entity_id:$sid})-[h:HAS_FACT {merged_from:$did, merge_run:$run}]->(f) "
        "MATCH (d:Entity {entity_id:$did}) WITH h, f, d LIMIT $batch_size "
        "CREATE (d)-[h2:HAS_FACT]->(f) "
        "SET h2=properties(h) REMOVE h2.merged_from, h2.merge_run DELETE h "
        "RETURN count(h) AS changed"
    ),
]


async def run_cleanup(corpus_id: str, run: str = "selftest") -> int:
    mc, db = _mongo()
    drv = _driver()
    try:
        doc = await db[PREVIEW].find_one(
            {"corpus_id": corpus_id}, sort=[("created_at", -1)]
        )
        prop_by_dup = {p["dup_id"]: p for p in (doc["proposals"] if doc else [])}
        n = 0
        async with drv.session() as sess:
            tombs = [
                dict(r)
                async for r in await sess.run(
                    "MATCH (t:Entity {tombstone:true, merge_run:$run}) "
                    "RETURN t.original_entity_id AS did, t.merged_into AS sid",
                    run=run,
                )
            ]
            for t in tombs:
                did, sid = t["did"], t["sid"]
                p = prop_by_dup.get(did, {})
                await sess.run(
                    "MERGE (d:Entity {entity_id:$did}) SET d.canonical_name=$cn, "
                    "d.primary_entity_type=$pt, d.normalized_name=$nn, d.restored_from=$run",
                    did=did,
                    cn=p.get("dup_cn", did),
                    pt=p.get("dup_pt", "other"),
                    nn=(p.get("dup_cn") or did).lower(),
                    run=run,
                )
                for q in _CLEANUP_MOVES:
                    while True:
                        result = await sess.run(
                            q,
                            did=did,
                            sid=sid,
                            run=run,
                            batch_size=ENTITY_DEDUP_WRITE_BATCH_SIZE,
                        )
                        row = await result.single()
                        if not row or int(row.get("changed") or 0) == 0:
                            break
                await sess.run(
                    "MATCH (t:Entity {entity_id:'tombstone:'+$did}) DETACH DELETE t",
                    did=did,
                )
                n += 1
                print(f"  reversed {did} <- {sid}")
        print(f"CLEANUP reversed {n} leftover '{run}' merges")
        return n
    finally:
        await drv.close()
        mc.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Entity dedup apply/undo")
    ap.add_argument("--corpus", required=True)
    ap.add_argument(
        "--selftest", type=int, metavar="N", help="apply+undo N, assert round-trip"
    )
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo", metavar="RUN_ID")
    ap.add_argument(
        "--cleanup",
        metavar="RUN_ID",
        help="reverse leftover merges by marker (e.g. selftest)",
    )
    ap.add_argument(
        "--decisions", default="auto", help="comma list: auto,auto_cross_type"
    )
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    if args.selftest:
        ok = asyncio.run(run_selftest(args.corpus, args.selftest))
        raise SystemExit(0 if ok else 1)
    if args.undo:
        asyncio.run(run_undo(args.undo))
        return
    if args.cleanup:
        asyncio.run(run_cleanup(args.corpus, args.cleanup))
        return
    if args.apply:
        decisions = {d.strip() for d in args.decisions.split(",") if d.strip()}
        asyncio.run(run_apply(args.corpus, decisions, args.limit))
        return
    ap.error("one of --selftest N / --apply / --undo RUN_ID is required")


if __name__ == "__main__":
    main()
