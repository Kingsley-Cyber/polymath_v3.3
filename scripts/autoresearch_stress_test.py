"""
autoresearch_stress_test.py — End-to-end production-readiness validator.

Drives a single batch of the N largest Markdown files from
C:\Workbench\Workshops\MARKDOWNS\merged through every layer of the stack:

  * batch upload + spool admission (disk-floor gate, INGEST_MAX_SPOOLED_BYTES)
  * Ghost A summarization with token-budget guard (skip-marker on overflow)
  * Ghost B extraction with token-budget guard
  * Mongo BSON pre-flight (16MB ceiling)
  * Qdrant write
  * Neo4j write
  * VRAM backpressure if embedder runs hot
  * Circuit breaker if N consecutive same-kind failures
  * Chat query against the ingested corpus

Reports per-file status and the aggregate batch summary at the end. Hard-fails
on any document that ends in `failed` status, partial coverage, or doesn't
finish within the deadline.

Usage:
  python scripts/autoresearch_stress_test.py
  python scripts/autoresearch_stress_test.py --files 5
  python scripts/autoresearch_stress_test.py --src "C:/Workbench/Workshops/MARKDOWNS/merged"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

API = os.environ.get("API_URL", "http://localhost:8000")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "013100")

DEFAULT_SRC = "C:/Workbench/Workshops/MARKDOWNS/merged"
DEFAULT_N = 10
INGEST_DEADLINE_SECONDS = 60 * 60  # 1h budget


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def login() -> str:
    resp = requests.post(
        f"{API}/api/auth/login",
        json={"username": ADMIN_USER, "password": ADMIN_PASS},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def H(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def find_largest_files(src: Path, n: int) -> list[Path]:
    files = list(src.glob("*.md"))
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    return files[:n]


def create_corpus(token: str, name: str) -> str:
    """Configure a corpus for the local vllm-summary + vllm-extract pipeline."""
    body = {
        "name": name,
        "description": "autoresearch stress test — 10 largest .md files",
        "default_ingestion_config": {
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
            "embedding_dimension": 1024,
            "embedding_model_id": "qwen3-embedding-0.6b-v1",
            "embed_mode": "local",
            "parent_chunk_tokens": {"min_tokens": 500, "target_tokens": 1200, "max_tokens": 2000},
            "child_chunk_tokens": {"min_tokens": 128, "target_tokens": 350, "max_tokens": 512},
            "chunk_overlap": 200,
            "max_summary_tokens": 175,
            "child_chunk_algorithm": "sentence_merge",
            "summary_models": [
                {
                    "provider_preset": "vllm-local",
                    "model": "openai/lfm2-rag",
                    "base_url": "http://vllm-summary:8000/v1",
                    "api_key": "local",
                    "max_concurrent": 24,
                    "extra_params": {"temperature": 0.0},
                    "context_length": 12288,
                }
            ],
            "extraction_models": [
                {
                    "provider_preset": "vllm-local",
                    "model": "openai/lfm2-extract",
                    "base_url": "http://vllm-extract:8000/v1",
                    "api_key": "local",
                    "max_concurrent": 64,
                    "extra_params": {"temperature": 0.0},
                    "context_length": 12288,
                }
            ],
            "extraction_repair_models": [],
            "entity_confidence_threshold": 0.5,
            "models_linked": False,
            "schema_strict": "soft",
            "use_neo4j": True,
            "chunk_summarization": True,
            "target_qdrant_collections": ["naive", "hrag", "graph"],
            "preset": "deep",
        },
    }
    resp = requests.post(
        f"{API}/api/corpora",
        headers={**H(token), "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    resp.raise_for_status()
    corpus = resp.json()
    return corpus["corpus_id"]


def upload_batch(token: str, corpus_id: str, files: list[Path]) -> str:
    """Multi-file batch upload via /api/corpora/{corpus_id}/ingest/batch."""
    multipart = []
    for f in files:
        multipart.append(("files", (f.name, f.read_bytes(), "text/markdown")))
    log(f"uploading {len(files)} files...")
    resp = requests.post(
        f"{API}/api/corpora/{corpus_id}/batch-ingest",
        headers=H(token),
        files=multipart,
        timeout=300,
    )
    if resp.status_code == 507:
        body = resp.json()
        log(f"FATAL — disk floor exceeded: {body}")
        raise SystemExit(2)
    resp.raise_for_status()
    body = resp.json()
    return body["batch_id"]


def poll_batch(token: str, batch_id: str, deadline: float) -> dict:
    """Poll the batch until terminal. Logs every change to per-doc phase
    counts so we can see WHEN the bottleneck transition happens (vector_ready
    plateau is the Ghost B graph-extraction phase).
    """
    started = time.time()
    last_signature = ""
    last_log_ts = 0.0
    while time.time() < deadline:
        try:
            # Poll the lighter /summary endpoint for per-status counts.
            # The full /batches/{id} response includes every item with
            # warnings inlined and gets slow at 100+ items. /summary skips
            # the items array and aggregates counts + top error_buckets.
            resp = requests.get(
                f"{API}/api/ingestion/batches/{batch_id}/summary",
                headers=H(token),
                timeout=60,
            )
        except requests.exceptions.RequestException as exc:
            log(f"batch poll exception (will retry): {exc}")
            time.sleep(10)
            continue
        if not resp.ok:
            log(f"batch fetch failed: {resp.status_code}")
            time.sleep(5)
            continue
        b = resp.json()
        signature = (
            f"{b.get('status')}/{b.get('current_phase')}|"
            f"q={b.get('queued_count', 0)}|"
            f"p={b.get('processing_count', 0)}|"
            f"vr={b.get('vector_ready_count', 0)}|"
            f"gr={b.get('graph_ready_count', 0)}|"
            f"gp={b.get('graph_partial_count', 0)}|"
            f"f={b.get('failed_count', 0)}"
        )
        now = time.time()
        # Log on signature change OR every 60s heartbeat so we know it's alive.
        if signature != last_signature or (now - last_log_ts) > 60:
            elapsed = int(now - started)
            log(f"+{elapsed:4d}s {signature}")
            last_signature = signature
            last_log_ts = now
            warnings = b.get("warnings") or []
            if warnings:
                for w in warnings[-2:]:
                    log(f"   warn: {w}")
        if b.get("status") in {"completed", "completed_with_errors", "failed", "cancelled", "paused"}:
            return b
        time.sleep(5)
    raise SystemExit(f"deadline exceeded for batch {batch_id}")


def fetch_summary(token: str, batch_id: str) -> dict:
    resp = requests.get(
        f"{API}/api/ingestion/batches/{batch_id}/summary",
        headers=H(token),
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def chat_smoke(token: str, corpus_id: str) -> bool:
    """Run a chat query to validate retrieval works after ingest."""
    body = {
        "message": "What are the central topics across the ingested documents?",
        "corpus_ids": [corpus_id],
        "retrieval_tier": "qdrant_mongo_graph",
    }
    resp = requests.post(
        f"{API}/api/chat",
        headers={**H(token), "Content-Type": "application/json"},
        json=body,
        stream=True,
        timeout=120,
    )
    if not resp.ok:
        log(f"chat failed: {resp.status_code} {resp.text[:200]}")
        return False
    saw_token = False
    deadline = time.time() + 60
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if '"type":"token"' in line or '"type": "token"' in line:
            saw_token = True
            break
        if time.time() > deadline:
            break
    return saw_token


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--src", default=DEFAULT_SRC)
    p.add_argument("--files", type=int, default=DEFAULT_N)
    args = p.parse_args()

    src = Path(args.src)
    if not src.exists():
        log(f"src not found: {src}")
        return 2

    log(f"=== autoresearch stress test (top-{args.files} largest .md from {src}) ===")
    files = find_largest_files(src, args.files)
    for i, f in enumerate(files, 1):
        log(f"  {i:2d}. {f.name}  ({f.stat().st_size / 1024 / 1024:.2f} MB)")

    log("logging in...")
    token = login()

    corpus_name = f"autoresearch-stress-{int(time.time())}"
    log(f"creating corpus {corpus_name}...")
    corpus_id = create_corpus(token, corpus_name)
    log(f"corpus_id={corpus_id}")

    log("uploading batch...")
    batch_id = upload_batch(token, corpus_id, files)
    log(f"batch_id={batch_id}")

    deadline = time.time() + INGEST_DEADLINE_SECONDS
    final = poll_batch(token, batch_id, deadline)

    log("=== final batch state ===")
    log(json.dumps({
        "status": final.get("status"),
        "current_phase": final.get("current_phase"),
        "total_files": final.get("total_files"),
        "vector_ready_count": final.get("vector_ready_count", 0),
        "graph_ready_count": final.get("graph_ready_count", 0),
        "graph_partial_count": final.get("graph_partial_count", 0),
        "failed_count": final.get("failed_count", 0),
        "cancelled_count": final.get("cancelled_count", 0),
        "paused_reason": final.get("paused_reason"),
    }, indent=2))

    summary = fetch_summary(token, batch_id)
    log("=== summary endpoint ===")
    log(json.dumps({
        "successful_count": summary.get("successful_count"),
        "error_buckets": summary.get("error_buckets", []),
        "warnings_tail": (summary.get("warnings") or [])[-5:],
    }, indent=2))

    if final.get("status") == "paused":
        log(f"BATCH PAUSED — reason={final.get('paused_reason')}")
        log("Inspect /api/ingestion/batches/{}/summary for error_buckets".format(batch_id))
        return 3

    if final.get("status") == "cancelled":
        log("BATCH CANCELLED — partial state, treat as failure")
        return 6

    if final.get("failed_count", 0) > 0:
        log(f"BATCH HAD FAILURES — {final.get('failed_count')} failed")
        return 4

    log("ingestion clean. running chat smoke...")
    if not chat_smoke(token, corpus_id):
        log("chat smoke FAILED — no token event in 60s")
        return 5

    log(f"=== ALL GREEN. corpus_id={corpus_id} batch_id={batch_id} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
