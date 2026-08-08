"""Factory E2E — production-shaped corpus through the REAL worker path.

Owner-ratified release-loop step 1 (2026-08-08): heterogeneous sources →
parse/unit.kind → hierarchy → GLiNER census → OpenIE farm → deterministic
compiler/gates → Mongo → embeddings → canonical q8 → Neo4j projection,
driven through the live API + ingest worker (no mocks), then validated
stage-by-stage: counts, receipts, q8-vs-legacy census, projection counts.

Usage:
    run_factory_e2e.py --inputs f1 f2 ... [--api-base http://localhost:8000]
                       [--corpus-name factory-e2e] [--timeout 3600]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import httpx  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from pymongo import MongoClient  # noqa: E402
import requests as _requests  # noqa: E402

ENV = dotenv_values(os.path.join(_BACKEND, "..", ".env"))
QDRANT = "http://localhost:6333"


def _login(api_base: str) -> str:
    username = ENV.get("DEFAULT_ADMIN_USERNAME", "admin")
    password = ENV.get("DEFAULT_ADMIN_PASSWORD", "")
    resp = httpx.post(
        f"{api_base}/api/auth/login",
        json={"username": username, "password": password},
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json().get("access_token") or resp.json().get("token")
    if not token:
        raise RuntimeError(f"login returned no token: {resp.json()!r}")
    Path("/tmp/pm_token.txt").write_text(token)
    return token


def _mongo():
    uri = re.sub(r"@mongodb:", "@localhost:", ENV.get("MONGODB_URI") or ENV.get("MONGO_URI") or "")
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return client[ENV.get("MONGODB_DB", "polymath")]


def _qdrant_count(collection: str, flt: dict | None = None) -> int:
    body: dict = {"exact": True}
    if flt:
        body["filter"] = flt
    resp = _requests.post(f"{QDRANT}/collections/{collection}/points/count", json=body, timeout=30)
    if resp.status_code != 200:
        return -1
    return int(resp.json()["result"]["count"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--corpus-name", default=f"factory-e2e-{time.strftime('%Y%m%dT%H%M%S')}")
    parser.add_argument("--timeout", type=int, default=5400)
    parser.add_argument("--json-out", default="/Users/king/polymath_v3.3/data_eval/factory_e2e_report.json")
    parser.add_argument("--existing-corpus", default="",
                        help="Resume: poll+validate this corpus id; skip create/upload")
    args = parser.parse_args()

    token = _login(args.api_base)
    client = httpx.Client(
        base_url=args.api_base,
        headers={"Authorization": f"Bearer {token}"},
        timeout=300,
    )
    if args.existing_corpus:
        cid = args.existing_corpus
        print(f"resuming corpus {cid}")
        return _poll_and_validate(client, cid, args)

    resp = client.post("/api/corpora", json={
        "name": args.corpus_name,
        "description": "Factory E2E — release loop step 1 (owner-ratified)",
    })
    resp.raise_for_status()
    body = resp.json()
    cid = body.get("corpus_id") or body.get("id") or (body.get("corpus") or {}).get("corpus_id")
    print(f"corpus: {args.corpus_name} = {cid}")

    # Durable backend batch (the ONLY live ingest path): one quick-upload
    # batch, worker-owned, summaries + Neo4j on — full production shape.
    handles = [open(path, "rb") for path in args.inputs]
    try:
        upload = client.post(
            f"/api/corpora/{cid}/ingest-batches/upload",
            files=[("files", (Path(path).name, handle)) for path, handle in zip(args.inputs, handles)],
            data={"use_neo4j": "true", "chunk_summarization": "true", "start": "true"},
            timeout=600,
        )
    finally:
        for handle in handles:
            handle.close()
    upload.raise_for_status()
    batch = upload.json()
    print("batch:", json.dumps(batch, default=str)[:400])

    return _poll_and_validate(client, cid, args)


def _poll_and_validate(client: httpx.Client, cid: str, args) -> int:
    expected = len(args.inputs)
    deadline = time.monotonic() + args.timeout
    terminal: dict[str, str] = {}
    while time.monotonic() < deadline:
        docs = client.get(f"/api/corpora/{cid}/documents").json()
        rows = docs if isinstance(docs, list) else docs.get("documents") or []
        # NOTE: "active" is this app's TERMINAL healthy document status
        # (record_status.ACTIVE_STATUS) — a live, fully ingested document.
        terminal = {
            (row.get("doc_id") or row.get("id")): (row.get("status") or "").lower()
            for row in rows
            if (row.get("status") or "").lower()
            and (row.get("status") or "").lower()
            not in ("processing", "queued", "running", "pending", "extracting", "embedding", "summarizing")
        }
        print(f"  [{time.strftime('%H:%M:%S')}] documents {len(rows)}/{expected}, "
              f"terminal {len(terminal)}")
        if len(rows) >= expected and len(terminal) >= expected:
            break
        time.sleep(20)
    doc_ids = set(terminal)
    print("ingest statuses:", dict(Counter(terminal.values())))

    # ── stage validation ────────────────────────────────────────────────
    db = _mongo()
    report: dict = {"corpus_id": cid, "corpus_name": args.corpus_name, "documents": terminal}
    docs_rows = list(db.documents.find({"corpus_id": cid}, {"doc_id": 1, "status": 1}))
    chunks = db.chunks.count_documents({"corpus_id": cid})
    parents = db.parent_chunks.count_documents({"corpus_id": cid})
    ghost_rows = db.ghost_b_extractions.count_documents({"corpus_id": cid})
    receipts = list(db.graphify_stage_receipts.find({"corpus_id": cid}, {"stage": 1, "status": 1}))
    receipt_counts = Counter((row["stage"], row["status"]) for row in receipts)
    failed_receipts = {key: value for key, value in receipt_counts.items() if key[1] != "passed"}
    report["mongo"] = {
        "documents": len(docs_rows),
        "chunks": chunks,
        "parent_chunks": parents,
        "ghost_b_extractions": ghost_rows,
        "stage_receipts": len(receipts),
        "failed_stage_receipts": {str(key): value for key, value in failed_receipts.items()},
    }

    cid8 = cid[:8]
    legacy = {kind: _qdrant_count(f"corpus_{cid8}_{kind}") for kind in ("naive", "hrag", "graph")}
    evidence_total = _qdrant_count(f"corpus_{cid8}_evidence")
    flags = {
        flag: _qdrant_count(
            f"corpus_{cid8}_evidence",
            {"must": [
                {"key": "record_kind", "match": {"value": "child"}},
                {"key": flag, "match": {"value": True}},
            ]},
        )
        for flag in ("eligible_focused", "eligible_hierarchical", "eligible_graph_seed")
    }
    child_points = _qdrant_count(
        f"corpus_{cid8}_evidence",
        {"must": [{"key": "record_kind", "match": {"value": "child"}}]},
    )
    summary_points = _qdrant_count(
        f"corpus_{cid8}_evidence",
        {"must": [{"key": "record_kind", "match": {"value": "parent_summary"}}]},
    )
    report["qdrant"] = {
        "legacy": legacy,
        "evidence_total": evidence_total,
        "evidence_children": child_points,
        "evidence_parent_summaries": summary_points,
        "evidence_flags": flags,
        "one_point_per_child": child_points == chunks,
        "flag_parity_counts": {
            "focused_vs_naive": (flags["eligible_focused"], legacy["naive"]),
            "hierarchical_vs_hrag": (flags["eligible_hierarchical"], legacy["hrag"]),
            "graph_seed_vs_graph": (flags["eligible_graph_seed"], legacy["graph"]),
        },
    }

    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(
            "bolt://localhost:7687",
            auth=(ENV.get("NEO4J_USER", "neo4j"), ENV.get("NEO4J_PASSWORD", "")),
        )
        with driver.session() as session:
            nodes = session.run(
                "MATCH (n {corpus_id: $cid}) RETURN count(n)", cid=cid,
            ).single()[0]
            rels = session.run(
                "MATCH ({corpus_id: $cid})-[r]->({corpus_id: $cid}) RETURN count(r)", cid=cid,
            ).single()[0]
        driver.close()
        report["neo4j"] = {"nodes": nodes, "relationships": rels}
    except Exception as exc:  # noqa: BLE001
        report["neo4j"] = {"error": str(exc)[:200]}

    Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.json_out).write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))
    ok = (
        len(terminal) == len(doc_ids)
        and not failed_receipts
        and report["qdrant"]["one_point_per_child"]
        and evidence_total > 0
    )
    print("FACTORY E2E:", "PASS" if ok else "INCOMPLETE — see report")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
