#!/usr/bin/env python3
"""Dry-run ownership manifest for historical artifacts (checklist P0.6).

Read-only. Discovers active corpora from Mongo (nothing hardcoded), then
enumerates every artifact whose owner is not an active corpus:

  - Qdrant per-corpus collections (corpus_{cid8}_{naive|hrag|graph|schemas})
  - Tier-0 doc cards in polymath_doc_summaries
  - summary_tree / corpus_readiness rows
  - Mongo rows in chunks, parent_chunks, ghost_b_extractions, documents,
    relation_support_records, corpus_lexicon, corpus_lexicon_sources
  - Neo4j node residue per orphan corpus_id

Writes docs/baselines/ORPHAN_MANIFEST_<UTC date>.json. This manifest is the
review artifact for the one-time cleanup; nothing is deleted here.
"""

from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
QDRANT = "http://127.0.0.1:6333"
NEO4J_HTTP = "http://127.0.0.1:7474/db/neo4j/tx/commit"
MONGO_COLLECTIONS = [
    "chunks",
    "parent_chunks",
    "ghost_b_extractions",
    "documents",
    "relation_support_records",
    "summary_tree",
    "corpus_readiness",
    "corpus_lexicon",
    "corpus_lexicon_sources",
]


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _mongo(env):
    sys.path.insert(0, str(REPO / ".tmp_pkgs"))
    from pymongo import MongoClient

    uri = (env.get("MONGODB_URI") or "").replace("@mongodb:", "@127.0.0.1:")
    if not uri:
        user = env.get("MONGO_USER", "polymath")
        pwd = quote_plus(env.get("MONGO_PASSWORD") or "")
        uri = f"mongodb://{user}:{pwd}@127.0.0.1:27017/polymath?authSource=admin"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return client, client[env.get("MONGODB_DATABASE", "polymath")]


def _qdrant(path: str, body: dict | None = None) -> Any:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(
        f"{QDRANT}{path}",
        data=data,
        method="POST" if body is not None else "GET",
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def _neo4j_count(env, corpus_id: str) -> int:
    user = env.get("NEO4J_USER") or "neo4j"
    pwd = env.get("NEO4J_PASSWORD") or "neo4j"
    auth = base64.b64encode(f"{user}:{pwd}".encode()).decode()
    payload = json.dumps(
        {
            "statements": [
                {
                    "statement": "MATCH (n {corpus_id: $cid}) RETURN count(n)",
                    "parameters": {"cid": corpus_id},
                }
            ]
        }
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
        res = json.loads(resp.read().decode())
    rows = res["results"][0]["data"]
    return int(rows[0]["row"][0]) if rows else 0


def main() -> int:
    env = _env()
    client, db = _mongo(env)
    active = list(
        db.corpora.find(
            {"$or": [{"status": {"$exists": False}}, {"status": "active"}]},
            {"_id": 0, "corpus_id": 1, "name": 1},
        )
    )
    active_ids = {c["corpus_id"] for c in active}
    active_prefixes = {cid.replace("-", "")[:8] for cid in active_ids}

    manifest: dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "active_corpora": active,
        "orphans": {},
    }

    cols = [c["name"] for c in _qdrant("/collections")["result"]["collections"]]
    orphan_cols = []
    for name in sorted(cols):
        if not name.startswith("corpus_"):
            continue
        prefix = name.split("_")[1]
        if prefix not in active_prefixes:
            count = _qdrant(f"/collections/{name}/points/count", {"exact": True})[
                "result"
            ]["count"]
            orphan_cols.append({"collection": name, "points": count})
    manifest["orphans"]["qdrant_collections"] = orphan_cols

    tier0_orphans: dict[str, int] = {}
    offset = None
    while True:
        body: dict[str, Any] = {
            "limit": 512,
            "with_payload": ["corpus_id"],
            "with_vector": False,
        }
        if offset is not None:
            body["offset"] = offset
        page = _qdrant("/collections/polymath_doc_summaries/points/scroll", body)
        points = page.get("result", {}).get("points") or []
        if not points:
            break
        for point in points:
            cid = str((point.get("payload") or {}).get("corpus_id") or "")
            if cid and cid not in active_ids:
                tier0_orphans[cid] = tier0_orphans.get(cid, 0) + 1
        offset = page.get("result", {}).get("next_page_offset")
        if offset is None:
            break
    manifest["orphans"]["tier0_doc_cards"] = tier0_orphans

    dead_ids: set[str] = set(tier0_orphans)
    for row in db.corpora.find({}, {"_id": 0, "corpus_id": 1, "status": 1}):
        cid = str(row.get("corpus_id") or "")
        if cid and cid not in active_ids:
            dead_ids.add(cid)
    mongo_residue: dict[str, dict[str, int]] = {}
    for coll in MONGO_COLLECTIONS:
        if coll not in db.list_collection_names():
            continue
        for cid in db[coll].distinct("corpus_id"):
            cid = str(cid or "")
            if cid and cid not in active_ids:
                dead_ids.add(cid)
                mongo_residue.setdefault(cid, {})[coll] = db[coll].count_documents(
                    {"corpus_id": cid}
                )
    manifest["orphans"]["mongo_residue"] = mongo_residue

    neo4j_residue: dict[str, int] = {}
    for cid in sorted(dead_ids):
        try:
            count = _neo4j_count(env, cid)
        except Exception as exc:  # noqa: BLE001
            count = -1
            manifest.setdefault("errors", []).append(f"neo4j {cid}: {exc}")
        if count:
            neo4j_residue[cid] = count
    manifest["orphans"]["neo4j_nodes"] = neo4j_residue
    manifest["orphans"]["dead_corpus_ids"] = sorted(dead_ids)

    out_dir = REPO / "docs" / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (
        f"ORPHAN_MANIFEST_{time.strftime('%Y-%m-%d', time.gmtime())}.json"
    )
    out_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    print(f"WROTE {out_path}")
    print("orphan qdrant collections:", len(orphan_cols))
    print("tier0 orphan cards:", sum(tier0_orphans.values()))
    print("dead corpus ids:", len(dead_ids))
    for cid in sorted(dead_ids):
        print(
            f"  {cid[:8]} mongo={sum((mongo_residue.get(cid) or {}).values())} "
            f"neo4j={neo4j_residue.get(cid, 0)}"
        )
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
