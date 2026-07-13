#!/usr/bin/env python3
"""Reproducible RAPTOR baseline census (read-only).

Captures the durable-state baseline required by
docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md before behavioral edits:

  - Mongo: corpora + status, summary_jobs by status, summary_tree shape,
    per-corpus document/parent/chunk/lexicon/extraction counts, readiness rows.
  - Qdrant: every collection's point count; per-corpus summary-point totals,
    explicit empty-model placeholder counts, noisy-kind summary counts;
    Tier-0 doc-summary counts; lexicon projection counts.
  - Neo4j: label totals plus per-corpus Chunk/Document/Fact counts.

Writes docs/baselines/BASELINE_<UTC date>.json and prints a digest.
Run from the deployment host (uses 127.0.0.1 service ports and repo .env):

    python3 backend/scripts/capture_raptor_baseline.py

The script performs no writes to any store.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
QDRANT = "http://127.0.0.1:6333"
NEO4J_HTTP = "http://127.0.0.1:7474/db/neo4j/tx/commit"


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
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


def _qdrant_count(collection: str, flt: dict | None = None) -> int:
    body: dict[str, Any] = {"exact": True}
    if flt:
        body["filter"] = flt
    res = _qdrant("POST", f"/collections/{collection}/points/count", body)
    return int(res["result"]["count"])


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
    with urllib.request.urlopen(req, timeout=300) as resp:
        res = json.loads(resp.read().decode())
    if res.get("errors"):
        raise RuntimeError(res["errors"][0].get("message") or str(res["errors"][0]))
    return res["results"][0]["data"]


NOISY_KINDS = [
    "toc",
    "bibliography",
    "index",
    "appendix",
    "front_matter",
    "back_matter",
    "links",
]


def main() -> int:
    sys.path.insert(0, str(REPO / ".tmp_pkgs"))
    env = _env()
    client, db = _mongo(env)

    commit = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    baseline: dict[str, Any] = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "git_commit": commit,
    }

    # --- Corpora (authoritative discovery; nothing hardcoded) ---
    corpora = list(
        db.corpora.find(
            {},
            {
                "_id": 0,
                "corpus_id": 1,
                "name": 1,
                "status": 1,
                "cleanup_status": 1,
                "cleanup_retry_at": 1,
            },
        )
    )
    baseline["corpora"] = corpora
    active = [
        c
        for c in corpora
        if (c.get("status") in (None, "active")) and c.get("corpus_id")
    ]
    prefix = lambda cid: cid.replace("-", "")[:8]  # noqa: E731

    # --- Qdrant collection census ---
    cols = [c["name"] for c in _qdrant("GET", "/collections")["result"]["collections"]]
    col_counts: dict[str, int] = {}
    for name in sorted(cols):
        col_counts[name] = _qdrant_count(name)
    baseline["qdrant_collections"] = col_counts

    # --- Per-active-corpus summary integrity counts ---
    summary_stats: dict[str, Any] = {}
    for c in active:
        pfx = prefix(c["corpus_id"])
        hrag = f"corpus_{pfx}_hrag"
        if hrag not in cols:
            summary_stats[c["name"]] = {"error": f"missing collection {hrag}"}
            continue
        is_summary = {"key": "chunk_type", "match": {"value": "summary"}}
        total = _qdrant_count(hrag, {"must": [is_summary]})
        empty_model = _qdrant_count(
            hrag,
            {
                "must": [
                    is_summary,
                    {"key": "summary_model", "match": {"value": ""}},
                ]
            },
        )
        noisy = _qdrant_count(
            hrag,
            {
                "must": [
                    is_summary,
                    {"key": "chunk_kind", "match": {"any": NOISY_KINDS}},
                ]
            },
        )
        schemas = f"corpus_{pfx}_schemas"
        lexicon_points = (
            _qdrant_count(
                schemas, {"must": [{"key": "kind", "match": {"value": "entity_lexicon"}}]}
            )
            if schemas in cols
            else None
        )
        tier0 = _qdrant_count(
            "polymath_doc_summaries",
            {"must": [{"key": "corpus_id", "match": {"value": c["corpus_id"]}}]},
        ) if "polymath_doc_summaries" in cols else None
        summary_stats[c["name"]] = {
            "corpus_id": c["corpus_id"],
            "summary_points": total,
            "summary_points_empty_model": empty_model,
            "summary_points_noisy_kind": noisy,
            "lexicon_points": lexicon_points,
            "tier0_doc_cards": tier0,
        }
    baseline["summary_integrity"] = summary_stats

    # --- Mongo job/tree/artifact census ---
    jobs = {
        str(r["_id"]): r["n"]
        for r in db.summary_jobs.aggregate(
            [{"$group": {"_id": "$status", "n": {"$sum": 1}}}]
        )
    }
    baseline["summary_jobs_by_status"] = jobs

    tree_by_type = {
        str(r["_id"]): r["n"]
        for r in db.summary_tree.aggregate(
            [{"$group": {"_id": "$node_type", "n": {"$sum": 1}}}]
        )
    }
    one_child = db.summary_tree.count_documents(
        {"node_type": "section", "child_node_ids": {"$size": 1}}
    )
    baseline["summary_tree"] = {"by_type": tree_by_type, "one_child_sections": one_child}

    per_corpus: dict[str, Any] = {}
    for c in active:
        cid = c["corpus_id"]
        lex_filter = {"corpus_id": cid}
        per_corpus[c["name"]] = {
            "documents": db.documents.count_documents({"corpus_id": cid}),
            "parent_chunks": db.parent_chunks.count_documents({"corpus_id": cid}),
            "chunks": db.chunks.count_documents({"corpus_id": cid}),
            "ghost_b_extractions": db.ghost_b_extractions.count_documents(
                {"corpus_id": cid}
            ),
            "lexicon_entries": db.corpus_lexicon.count_documents(lex_filter),
            "lexicon_retrieval_eligible": db.corpus_lexicon.count_documents(
                {**lex_filter, "retrieval_eligible": True}
            ),
        }
    baseline["mongo_per_corpus"] = per_corpus

    readiness = []
    for row in db.corpus_readiness.find({}, {"_id": 0}):
        readiness.append(
            {
                k: v
                for k, v in row.items()
                if isinstance(v, (str, int, float, bool)) or v is None
            }
        )
    baseline["corpus_readiness"] = readiness

    # --- Neo4j ---
    labels = _neo4j(env, "CALL db.labels() YIELD label RETURN label")
    label_counts: dict[str, int] = {}
    for row in labels:
        label = row["row"][0]
        data = _neo4j(env, f"MATCH (n:`{label}`) RETURN count(n)")
        label_counts[label] = int(data[0]["row"][0])
    neo_per_corpus: dict[str, Any] = {}
    for c in active:
        cid = c["corpus_id"]
        counts = {}
        for label in ("Chunk", "Document", "Fact"):
            if label not in label_counts:
                counts[label] = 0
                continue
            data = _neo4j(
                env,
                f"MATCH (n:`{label}` {{corpus_id: $cid}}) RETURN count(n)",
                {"cid": cid},
            )
            counts[label] = int(data[0]["row"][0])
        neo_per_corpus[c["name"]] = counts
    baseline["neo4j"] = {"labels": label_counts, "per_corpus": neo_per_corpus}

    out_dir = REPO / "docs" / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"BASELINE_{time.strftime('%Y-%m-%d', time.gmtime())}.json"
    out_path.write_text(json.dumps(baseline, indent=2, default=str) + "\n")

    print(f"WROTE {out_path}")
    print(json.dumps(baseline["summary_integrity"], indent=2, default=str))
    print("summary_jobs_by_status", jobs)
    print("summary_tree", baseline["summary_tree"])
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
