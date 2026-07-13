#!/usr/bin/env python3
"""Three-tier live latency probe against the deployed backend (read-only).

Runs one streaming /api/chat request per configuration and records:
  - wall-clock to first SSE event, first token, and stream completion
  - every trace event title/status with its arrival offset
  - the "Local RAG retrieval" DONE metadata (retrieval diagnostics/timings)
  - chunks_returned / answer length / refusal signal

Configurations (corpora discovered from Mongo, never hardcoded):
  1. Fast    (qdrant_only)        — largest active corpus
  2. Hybrid  (qdrant_mongo)       — largest active corpus
  3. Graph   (qdrant_mongo_graph) — largest active corpus
  4. Hybrid cross-corpus          — two largest active corpora
  5. Hybrid negative control      — deliberately unanswerable query

Usage:
    TOKEN=$(docker exec polymath_v33-backend-1 cat /tmp/probe_token) \
        python3 backend/scripts/probe_tier_latency.py

Writes docs/baselines/LATENCY_<UTC date>.json. Performs no store writes;
each probe does create a throwaway conversation via the normal chat path.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus

REPO = Path(__file__).resolve().parents[2]
API = os.environ.get("POLYMATH_API", "http://127.0.0.1:8000")

PROBE_QUERY = (
    "What practical advice does this material give about improving focus "
    "and attention?"
)
NEGATIVE_QUERY = "What is the boiling point of tungsten in kelvin?"


def _env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (REPO / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _active_corpora() -> list[dict]:
    sys.path.insert(0, str(REPO / ".tmp_pkgs"))
    from pymongo import MongoClient

    env = _env()
    uri = (env.get("MONGODB_URI") or "").replace("@mongodb:", "@127.0.0.1:")
    if not uri:
        user = env.get("MONGO_USER", "polymath")
        pwd = quote_plus(env.get("MONGO_PASSWORD") or "")
        uri = f"mongodb://{user}:{pwd}@127.0.0.1:27017/polymath?authSource=admin"
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    db = client[env.get("MONGODB_DATABASE", "polymath")]
    rows = list(
        db.corpora.find(
            {"$or": [{"status": {"$exists": False}}, {"status": "active"}]},
            {"_id": 0, "corpus_id": 1, "name": 1},
        )
    )
    for row in rows:
        row["documents"] = db.documents.count_documents(
            {"corpus_id": row["corpus_id"]}
        )
    client.close()
    return sorted(rows, key=lambda r: -r["documents"])


def probe(token: str, label: str, message: str, corpus_ids: list[str], tier: str) -> dict:
    body = json.dumps(
        {
            "message": message,
            "corpus_ids": corpus_ids,
            "retrieval_tier": tier,
        }
    ).encode()
    req = urllib.request.Request(
        f"{API}/api/chat",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream",
        },
    )
    started = time.monotonic()
    result: dict = {
        "label": label,
        "tier": tier,
        "corpus_ids": corpus_ids,
        "query": message,
        "trace": [],
        "first_event_s": None,
        "first_token_s": None,
        "total_s": None,
        "chunks_returned": None,
        "answer_chars": 0,
        "retrieval_done_metadata": None,
        "error": None,
    }
    answer_parts: list[str] = []
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
                    if result["first_event_s"] is None:
                        result["first_event_s"] = now
                    etype = event.get("type")
                    if etype == "token":
                        if result["first_token_s"] is None:
                            result["first_token_s"] = now
                        answer_parts.append(event.get("content") or "")
                    trace = event.get("trace_event")
                    if trace:
                        result["trace"].append(
                            {
                                "t": now,
                                "lane": trace.get("lane"),
                                "title": trace.get("title"),
                                "status": trace.get("status"),
                            }
                        )
                        if (
                            trace.get("title") == "Local RAG retrieval"
                            and trace.get("status") == "done"
                        ):
                            result["retrieval_done_metadata"] = trace.get("metadata")
                    if event.get("chunks_returned") is not None:
                        result["chunks_returned"] = event["chunks_returned"]
                    if etype == "done":
                        result["total_s"] = now
    except Exception as exc:  # noqa: BLE001 — probe must record, not crash
        result["error"] = f"{type(exc).__name__}: {exc}"
    if result["total_s"] is None:
        result["total_s"] = round(time.monotonic() - started, 3)
    result["answer_chars"] = len("".join(answer_parts))
    result["answer_head"] = "".join(answer_parts)[:300]
    return result


def main() -> int:
    token = os.environ.get("TOKEN") or ""
    if not token:
        print("ERROR: set TOKEN env var (bearer JWT)")
        return 1
    corpora = _active_corpora()
    if not corpora:
        print("ERROR: no active corpora discovered")
        return 1
    main_corpus = corpora[0]
    second = corpora[1] if len(corpora) > 1 else None
    print(
        "PROBE corpora:",
        [(c["name"], c["documents"]) for c in corpora],
    )

    runs = [
        ("fast_single", PROBE_QUERY, [main_corpus["corpus_id"]], "qdrant_only"),
        ("hybrid_single", PROBE_QUERY, [main_corpus["corpus_id"]], "qdrant_mongo"),
        ("graph_single", PROBE_QUERY, [main_corpus["corpus_id"]], "qdrant_mongo_graph"),
    ]
    if second:
        runs.append(
            (
                "hybrid_cross",
                PROBE_QUERY,
                [main_corpus["corpus_id"], second["corpus_id"]],
                "qdrant_mongo",
            )
        )
    runs.append(
        ("hybrid_negative", NEGATIVE_QUERY, [main_corpus["corpus_id"]], "qdrant_mongo")
    )

    results = []
    for label, message, cids, tier in runs:
        print(f"RUN {label} tier={tier} corpora={len(cids)} ...", flush=True)
        res = probe(token, label, message, cids, tier)
        print(
            f"  total={res['total_s']}s first_token={res['first_token_s']}s "
            f"chunks={res['chunks_returned']} answer_chars={res['answer_chars']} "
            f"error={res['error']}",
            flush=True,
        )
        results.append(res)

    out = {
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "api": API,
        "corpora_ranked": [
            {"name": c["name"], "corpus_id": c["corpus_id"], "documents": c["documents"]}
            for c in corpora
        ],
        "runs": results,
    }
    out_dir = REPO / "docs" / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"LATENCY_{time.strftime('%Y-%m-%d', time.gmtime())}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(f"WROTE {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
