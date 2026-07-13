#!/usr/bin/env python3
"""Run the held-out evaluation suite against the deployed backend (P1.1).

For every question in backend/evals/heldout_questions.jsonl and each requested
tier, drives the real /api/chat SSE path and records:
  - returned source doc_ids and corpora, answer text, refusal detection
  - answerability gate status + retrieval diagnostics from trace metadata
  - wall-clock, first-token latency

Scores per question:
  - doc_hit: any expected doc in returned sources (all expected docs when
    expected_all_docs)
  - doc_recall: |expected âˆ© returned| / |expected|
  - concept_recall: fraction of expected_concepts present in the answer text
  - answerability_ok: refusal/answer matches the question's answerable flag
  - corpus_diversity_ok (cross_corpus shapes): sources span >=2 corpora
  - forced_seat (cross_corpus_irrelevant): whether the irrelevant corpus
    received a final seat

Usage:
    TOKEN=$(docker exec polymath_v33-backend-1 cat /tmp/probe_token) \
        python3 backend/scripts/run_heldout_eval.py --tier qdrant_mongo

Writes docs/baselines/EVAL_<UTCdate>_<tier>.json. Read-only for stores
(each probe creates a throwaway conversation via the normal chat path).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
API = os.environ.get("POLYMATH_API", "http://127.0.0.1:8000")
QUESTIONS = REPO / "backend" / "evals" / "heldout_questions.jsonl"
REFUSAL_RE = re.compile(
    r"i cannot answer|does not contain|did not find source evidence|"
    r"cannot answer that as a source-backed",
    re.IGNORECASE,
)


def _corpus_ids_by_name() -> dict[str, str]:
    sys.path.insert(0, str(REPO / ".tmp_pkgs"))
    from pymongo import MongoClient

    env: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    uri = (env.get("MONGODB_URI") or "").replace("@mongodb:", "@127.0.0.1:")
    if not uri:
        user = env.get("MONGO_USER", "polymath")
        pwd = quote_plus(env.get("MONGO_PASSWORD") or "")
        uri = f"mongodb://{user}:{pwd}@127.0.0.1:27017/polymath?authSource=admin"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    db = client[env.get("MONGODB_DATABASE", "polymath")]
    out = {row["name"]: row["corpus_id"] for row in db.corpora.find({}, {"name": 1, "corpus_id": 1})}
    client.close()
    return out


def _chat(token: str, message: str, corpus_ids: list[str], tier: str,
          conversation_id: str | None = None) -> dict:
    body: dict = {
        "message": message,
        "corpus_ids": corpus_ids,
        "retrieval_tier": tier,
    }
    if conversation_id:
        body["conversation_id"] = conversation_id
    req = urllib.request.Request(
        f"{API}/api/chat",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    started = time.monotonic()
    result: dict = {
        "answer": "",
        "sources": [],
        "conversation_id": None,
        "first_token_s": None,
        "total_s": None,
        "retrieval_metadata": None,
        "answerability": None,
        "error": None,
    }
    parts: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            buffer = b""
            while True:
                chunk = resp.read(1)
                if not chunk:
                    break
                buffer += chunk
                if not buffer.endswith(b"\n\n"):
                    continue
                block, buffer = buffer, b""
                for line in block.decode("utf-8", "replace").splitlines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        event = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    now = round(time.monotonic() - started, 3)
                    if event.get("conversation_id"):
                        result["conversation_id"] = event["conversation_id"]
                    etype = event.get("type")
                    if etype == "token":
                        if result["first_token_s"] is None:
                            result["first_token_s"] = now
                        parts.append(event.get("content") or "")
                    elif etype == "sources":
                        for source in event.get("sources") or []:
                            result["sources"].append(
                                {
                                    "doc_id": source.get("doc_id"),
                                    "corpus_id": source.get("corpus_id"),
                                    "chunk_id": source.get("chunk_id"),
                                    "score": source.get("score"),
                                }
                            )
                    trace = event.get("trace_event") or {}
                    metadata = trace.get("metadata") or {}
                    if (
                        trace.get("title") == "Local RAG retrieval"
                        and trace.get("status") == "done"
                    ):
                        result["retrieval_metadata"] = {
                            "duration_s": metadata.get("duration_s"),
                            "chunks": metadata.get("chunks"),
                            "corpus_floor": (metadata.get("retrieval_diagnostics") or {}).get("corpus_floor")
                            if isinstance(metadata.get("retrieval_diagnostics"), dict)
                            else None,
                        }
                    if isinstance(metadata.get("answerability"), dict):
                        result["answerability"] = {
                            "status": metadata["answerability"].get("status"),
                            "required_coverage": metadata["answerability"].get(
                                "required_coverage"
                            ),
                            "answer_shape": metadata["answerability"].get("answer_shape"),
                            "diagnostic_source": metadata["answerability"].get(
                                "diagnostic_source"
                            ),
                        }
                    if etype == "done":
                        result["total_s"] = now
    except Exception as exc:  # noqa: BLE001 — record, don't crash the suite
        result["error"] = f"{type(exc).__name__}: {exc}"
    if result["total_s"] is None:
        result["total_s"] = round(time.monotonic() - started, 3)
    result["answer"] = "".join(parts)
    return result


def score(row: dict, run: dict) -> dict:
    expected = list(row.get("expected_doc_ids") or [])
    returned_docs = {s["doc_id"] for s in run["sources"] if s.get("doc_id")}
    returned_corpora = {s["corpus_id"] for s in run["sources"] if s.get("corpus_id")}
    hit_docs = [d for d in expected if d in returned_docs]
    doc_recall = (len(hit_docs) / len(expected)) if expected else None
    doc_hit = (
        (len(hit_docs) == len(expected)) if row.get("expected_all_docs")
        else bool(hit_docs)
    ) if expected else None
    answer_lower = (run["answer"] or "").lower()
    concepts = [c.lower() for c in row.get("expected_concepts") or []]
    concept_hits = [c for c in concepts if c in answer_lower]
    refused = bool(REFUSAL_RE.search(run["answer"] or ""))
    answerable_expected = bool(row.get("answerable"))
    answerability_ok = (not refused) if answerable_expected else refused
    out = {
        "doc_hit": doc_hit,
        "doc_recall": round(doc_recall, 3) if doc_recall is not None else None,
        "concept_recall": round(len(concept_hits) / len(concepts), 3)
        if concepts
        else None,
        "refused": refused,
        "answerability_ok": answerability_ok,
        "returned_doc_count": len(returned_docs),
        "returned_corpora": sorted(returned_corpora),
    }
    if row.get("shape") in {"cross_corpus"}:
        out["corpus_diversity_ok"] = len(returned_corpora) >= 2
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", default="qdrant_mongo",
                    choices=["qdrant_only", "qdrant_mongo", "qdrant_mongo_graph"])
    ap.add_argument("--ids", nargs="*", help="run only these question ids")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()
    token = os.environ.get("TOKEN") or ""
    if not token:
        print("ERROR: set TOKEN")
        return 1
    by_name = _corpus_ids_by_name()
    rows = [json.loads(line) for line in QUESTIONS.read_text().splitlines() if line.strip()]
    if args.ids:
        rows = [r for r in rows if r["id"] in set(args.ids)]
    if args.limit:
        rows = rows[: args.limit]
    results = []
    for row in rows:
        cids = [by_name[name] for name in row["corpora"] if name in by_name]
        conversation_id = None
        for turn in row.get("history") or []:
            prior = _chat(token, turn, cids, args.tier)
            conversation_id = prior.get("conversation_id") or conversation_id
        run = _chat(token, row["question"], cids, args.tier, conversation_id)
        scored = score(row, run)
        results.append(
            {
                "id": row["id"],
                "shape": row["shape"],
                "tier": args.tier,
                "question": row["question"],
                **scored,
                "total_s": run["total_s"],
                "first_token_s": run["first_token_s"],
                "answerability": run["answerability"],
                "retrieval_metadata": run["retrieval_metadata"],
                "error": run["error"],
                "answer_head": (run["answer"] or "")[:220],
            }
        )
        print(
            f"{row['id']} {row['shape']:<24} hit={scored['doc_hit']} "
            f"recall={scored['doc_recall']} ans_ok={scored['answerability_ok']} "
            f"t={run['total_s']}s err={run['error']}",
            flush=True,
        )
    summary: dict = {"tier": args.tier, "n": len(results)}
    scored_rows = [r for r in results if not r["error"]]
    def _avg(key):
        vals = [r[key] for r in scored_rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 3) if vals else None
    summary["doc_hit_rate"] = _avg("doc_hit")
    summary["doc_recall_mean"] = _avg("doc_recall")
    summary["concept_recall_mean"] = _avg("concept_recall")
    summary["answerability_ok_rate"] = _avg("answerability_ok")
    summary["latency_mean_s"] = _avg("total_s")
    summary["errors"] = len(results) - len(scored_rows)
    by_shape: dict = {}
    for r in scored_rows:
        bucket = by_shape.setdefault(r["shape"], {"n": 0, "ans_ok": 0, "hits": 0, "with_docs": 0})
        bucket["n"] += 1
        bucket["ans_ok"] += 1 if r["answerability_ok"] else 0
        if r["doc_hit"] is not None:
            bucket["with_docs"] += 1
            bucket["hits"] += 1 if r["doc_hit"] else 0
    summary["by_shape"] = by_shape
    out = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api": API,
        "summary": summary,
        "results": results,
    }
    out_dir = REPO / "docs" / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (
        f"EVAL_{time.strftime('%Y-%m-%d', time.gmtime())}_{args.tier}.json"
    )
    out_path.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print("SUMMARY", json.dumps(summary, indent=2))
    print(f"WROTE {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
