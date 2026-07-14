#!/usr/bin/env python3
"""One-shot orphan catalog sweeper for the intended 3+1 live corpus set.

Keeps:
  polymath_v2, ecommerce_AI_FILM_SCHOOL, markbuildsbrands_transcripts, UGO_CORPUS

Then:
  1) drops Qdrant collections whose corpus prefix is not active
  2) deletes Tier-0 doc cards for inactive corpus_ids
  3) batched-deletes Neo4j nodes for dead corpus_ids (Facts first, small txs)
  4) removes stale corpus_readiness / summary_tree rows for dead ids
  5) hard-deletes soft-tombstoned Mongo rows for fully-purged corpora

Safe to re-run. Prints a report; does not touch active corpus vectors.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
ACTIVE_NAMES = {
    "polymath_v2",
    "ecommerce_AI_FILM_SCHOOL",
    "markbuildsbrands_transcripts",
    "UGO_CORPUS",
}
NEO4J_BATCH = 2000
QDRANT = "http://127.0.0.1:6333"
NEO4J_HTTP = "http://127.0.0.1:7474/db/neo4j/tx/commit"


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    path = REPO / ".env"
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _mongo(env: dict[str, str]):
    from pymongo import MongoClient

    uri = (env.get("MONGODB_URI") or "").replace("@mongodb:", "@127.0.0.1:")
    if not uri:
        user = env.get("MONGO_USER", "polymath")
        pwd = quote_plus(env.get("MONGO_PASSWORD") or "")
        uri = f"mongodb://{user}:{pwd}@127.0.0.1:27017/polymath?authSource=admin"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    db = client[env.get("MONGODB_DATABASE", "polymath")]
    db.command("ping")
    return client, db


def _qdrant(method: str, path: str, body: dict | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{QDRANT}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read()
        return json.loads(raw.decode()) if raw else {}


def _neo4j(env: dict[str, str], statement: str, params: dict | None = None) -> Any:
    user = env.get("NEO4J_USER") or "neo4j"
    pwd = env.get("NEO4J_PASSWORD") or "neo4j"
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    payload = json.dumps(
        {"statements": [{"statement": statement, "parameters": params or {}}]}
    ).encode()
    req = urllib.request.Request(
        NEO4J_HTTP,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Basic {auth}",
        },
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def _neo4j_count(env: dict[str, str], corpus_id: str) -> int:
    res = _neo4j(
        env,
        "MATCH (n {corpus_id: $cid}) RETURN count(n) AS n",
        {"cid": corpus_id},
    )
    errs = res.get("errors") or []
    if errs:
        raise RuntimeError(errs[0].get("message") or str(errs[0]))
    rows = res["results"][0]["data"]
    return int(rows[0]["row"][0]) if rows else 0


def purge_neo4j_corpus(env: dict[str, str], corpus_id: str) -> dict[str, int]:
    """Small-batch deletes to avoid MemoryPoolOutOfMemoryError."""
    deleted = {"Fact": 0, "Chunk": 0, "Document": 0, "other": 0, "orphan_entities": 0}

    # Facts first — usually the bulk of authentic_library residue.
    while True:
        res = _neo4j(
            env,
            """
            MATCH (f:Fact {corpus_id: $cid})
            WITH f LIMIT $limit
            DETACH DELETE f
            RETURN count(*) AS n
            """,
            {"cid": corpus_id, "limit": NEO4J_BATCH},
        )
        if res.get("errors"):
            raise RuntimeError(res["errors"][0].get("message"))
        n = int(res["results"][0]["data"][0]["row"][0])
        deleted["Fact"] += n
        print(f"  neo4j Fact -{n} total={deleted['Fact']}", flush=True)
        if n == 0:
            break

    while True:
        res = _neo4j(
            env,
            """
            MATCH (c:Chunk {corpus_id: $cid})
            WITH c LIMIT $limit
            DETACH DELETE c
            RETURN count(*) AS n
            """,
            {"cid": corpus_id, "limit": NEO4J_BATCH},
        )
        if res.get("errors"):
            raise RuntimeError(res["errors"][0].get("message"))
        n = int(res["results"][0]["data"][0]["row"][0])
        deleted["Chunk"] += n
        print(f"  neo4j Chunk -{n} total={deleted['Chunk']}", flush=True)
        if n == 0:
            break

    while True:
        res = _neo4j(
            env,
            """
            MATCH (d:Document {corpus_id: $cid})
            WITH d LIMIT $limit
            DETACH DELETE d
            RETURN count(*) AS n
            """,
            {"cid": corpus_id, "limit": NEO4J_BATCH},
        )
        if res.get("errors"):
            raise RuntimeError(res["errors"][0].get("message"))
        n = int(res["results"][0]["data"][0]["row"][0])
        deleted["Document"] += n
        if n == 0:
            break

    while True:
        res = _neo4j(
            env,
            """
            MATCH (n {corpus_id: $cid})
            WITH n LIMIT $limit
            DETACH DELETE n
            RETURN count(*) AS n
            """,
            {"cid": corpus_id, "limit": NEO4J_BATCH},
        )
        if res.get("errors"):
            raise RuntimeError(res["errors"][0].get("message"))
        n = int(res["results"][0]["data"][0]["row"][0])
        deleted["other"] += n
        print(f"  neo4j other -{n} total={deleted['other']}", flush=True)
        if n == 0:
            break

    # Shared RELATES_TO edges that only pointed at this corpus.
    _neo4j(
        env,
        """
        MATCH ()-[r:RELATES_TO]-()
        WHERE $cid IN coalesce(r.corpus_ids, [])
        SET r.corpus_ids = [x IN coalesce(r.corpus_ids, []) WHERE x <> $cid]
        """,
        {"cid": corpus_id},
    )
    _neo4j(
        env,
        """
        MATCH ()-[r:RELATES_TO]-()
        WHERE size(coalesce(r.corpus_ids, [])) = 0
        DELETE r
        """,
    )
    res = _neo4j(
        env,
        """
        MATCH (e:Entity)
        WHERE NOT (e)<-[:MENTIONS]-() AND NOT (e)-[:HAS_FACT]->()
        WITH e LIMIT 5000
        DETACH DELETE e
        RETURN count(*) AS n
        """,
    )
    if not res.get("errors"):
        deleted["orphan_entities"] = int(res["results"][0]["data"][0]["row"][0])
    return deleted


def scrub_tier0(active_ids: set[str]) -> dict[str, int]:
    deleted = 0
    scanned = 0
    offset = None
    while True:
        body: dict[str, Any] = {
            "limit": 256,
            "with_payload": ["corpus_id"],
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        page = _qdrant("POST", "/collections/polymath_doc_summaries/points/scroll", body)
        points = page.get("result", {}).get("points") or []
        if not points:
            break
        doomed = []
        for point in points:
            scanned += 1
            cid = str((point.get("payload") or {}).get("corpus_id") or "")
            if cid and cid not in active_ids:
                doomed.append(point["id"])
        if doomed:
            _qdrant(
                "POST",
                "/collections/polymath_doc_summaries/points/delete",
                {"points": doomed},
            )
            deleted += len(doomed)
            print(f"  tier0 deleted {len(doomed)} (running total {deleted})", flush=True)
        offset = page.get("result", {}).get("next_page_offset")
        if offset is None:
            break
    return {"scanned": scanned, "deleted": deleted}


def main() -> int:
    sys.path.insert(0, str(REPO / ".tmp_pkgs"))
    env = _env()
    client, db = _mongo(env)

    active_rows = list(
        db.corpora.find(
            {
                "$and": [
                    {"name": {"$in": list(ACTIVE_NAMES)}},
                    {
                        "$or": [
                            {"status": {"$exists": False}},
                            {"status": "active"},
                        ]
                    },
                ]
            },
            {"_id": 0, "corpus_id": 1, "name": 1, "status": 1},
        )
    )
    active_ids = {str(row["corpus_id"]) for row in active_rows}
    active_prefixes = {cid.replace("-", "")[:8] for cid in active_ids}
    print("ACTIVE", [(r.get("name"), r.get("corpus_id")[:8]) for r in active_rows])
    if len(active_ids) != 4:
        print("ERROR: expected exactly 4 active corpora; aborting for safety")
        return 2

    # 1) Qdrant collection drops
    cols = _qdrant("GET", "/collections")["result"]["collections"]
    dropped = []
    for col in cols:
        name = col["name"]
        if not name.startswith("corpus_"):
            continue
        prefix = name.split("_")[1]
        if prefix not in active_prefixes:
            try:
                _qdrant("DELETE", f"/collections/{name}")
                dropped.append(name)
                print(f"dropped {name}", flush=True)
            except urllib.error.HTTPError as exc:
                print(f"skip drop {name}: {exc}", flush=True)
    print(f"QDRANT_DROPPED {len(dropped)}")

    # 2) Tier-0 scrub
    print("TIER0_SCRUB")
    tier0 = scrub_tier0(active_ids)
    print("TIER0", tier0)

    # 3) Discover dead corpus ids from Mongo + Neo4j leftovers
    dead_ids: set[str] = set()
    for row in db.corpora.find({}, {"_id": 0, "corpus_id": 1, "status": 1, "name": 1}):
        cid = str(row.get("corpus_id") or "")
        if cid and cid not in active_ids:
            dead_ids.add(cid)
    for cid in db.summary_tree.distinct("corpus_id"):
        if cid and cid not in active_ids:
            dead_ids.add(str(cid))
    for cid in db.corpus_readiness.distinct("corpus_id"):
        if cid and cid not in active_ids:
            dead_ids.add(str(cid))
    # known orphans from audit
    for cid in (
        "a42992d0-216c-400b-8447-43a90e38d9a5",
        "7c8ec461-e83f-4ad8-a878-be9b27cfcef4",
        "0a231647-a170-4fd9-8f4c-c15a50075505",
        "f8a0aa85-6cb4-4f64-a973-f9183f1546bb",
    ):
        dead_ids.add(cid)

    print(f"DEAD_CORPUS_IDS {len(dead_ids)}")
    for cid in sorted(dead_ids):
        before = _neo4j_count(env, cid)
        print(f"NEO4J_PURGE {cid[:8]} before={before}", flush=True)
        if before:
            stats = purge_neo4j_corpus(env, cid)
            after = _neo4j_count(env, cid)
            print(f"  done stats={stats} after={after}", flush=True)
        # Mongo derived cleanup
        db.summary_tree.delete_many({"corpus_id": cid})
        db.corpus_readiness.delete_many({"corpus_id": cid})
        # Hard-delete soft tombstones for this dead corpus (chunks can be huge —
        # delete in batches by _id).
        for coll_name in (
            "chunks",
            "parent_chunks",
            "ghost_b_extractions",
            "documents",
            "relation_support_records",
        ):
            removed = 0
            while True:
                ids = [
                    row["_id"]
                    for row in db[coll_name]
                    .find({"corpus_id": cid}, {"_id": 1})
                    .limit(5000)
                ]
                if not ids:
                    break
                result = db[coll_name].delete_many({"_id": {"$in": ids}})
                removed += int(result.deleted_count or 0)
                print(f"  mongo {coll_name} -{result.deleted_count} total={removed}", flush=True)
            if removed:
                print(f"  mongo {coll_name} removed={removed}")
        # corpus lexicon
        if "corpus_lexicon" in db.list_collection_names():
            lex = db.corpus_lexicon.delete_many({"corpus_id": cid})
            print(f"  lexicon removed={lex.deleted_count}")
        # finalize corpus tombstone
        db.corpora.update_one(
            {"corpus_id": cid},
            {
                "$set": {
                    "cleanup_status": "complete",
                    "cleanup_completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "status": "deleted",
                },
                "$unset": {
                    "cleanup_owner": "",
                    "cleanup_lease_until": "",
                    "cleanup_retry_at": "",
                    "cleanup_warnings": "",
                },
            },
        )

    # Optional: remove deleted corpus rows entirely once empty
    for cid in list(dead_ids):
        leftovers = {
            "chunks": db.chunks.count_documents({"corpus_id": cid}),
            "parents": db.parent_chunks.count_documents({"corpus_id": cid}),
            "docs": db.documents.count_documents({"corpus_id": cid}),
            "neo4j": _neo4j_count(env, cid),
        }
        print(f"VERIFY {cid[:8]} {leftovers}")
        if all(v == 0 for v in leftovers.values()):
            db.corpora.delete_one({"corpus_id": cid})
            print(f"  removed corpora row {cid[:8]}")

    print("DONE")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
