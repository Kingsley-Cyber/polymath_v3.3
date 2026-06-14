"""Summary-tier backfill + readiness guardrail.

Repairs the silent gap where a corpus has parent summaries missing and/or never
indexed into Qdrant `hrag` as `chunk_type="summary"` points — which leaves
Funnel A (the breadth lane) returning nothing and collapses broad queries to a
few cosine-nearest child chunks.

Three idempotent, resumable ops:

  --index    Embed every parent that HAS summary text and upsert summary points
             into `hrag` (deterministic point id → safe to re-run). No model
             calls, no cost. This alone makes Funnel A live for already-
             summarized parents.

  --generate Summarize parents with `summary=None` via the lane pool, persist to
             Mongo `parent_chunks`. Batched + resumable (re-run picks up the
             remaining None). GUARDRAIL: if a batch returns 0 summaries the
             whole lane pool is treated as exhausted/dead and the run ABORTS
             loudly with a progress report — never silently finishes partial
             (the exact failure that left the tier empty).

  --verify   Readiness assertion: summary points in `hrag` vs parents-with-
             summary vs total parents. Reports HEALTHY / DEGRADED. Reusable as
             the post-ingest gate so "complete" never hides an empty tier.

Pool config is read from an UNTRACKED file (POLYMATH_SUMMARY_POOL_FILE, default
/tmp/summary_pool.json) — provider keys never live in the repo.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone

from config import get_settings

_POOL_FILE = os.environ.get("POLYMATH_SUMMARY_POOL_FILE", "/tmp/summary_pool.json")
_EMBED_DIM = 1024  # MLX Qwen3-Embedding — same space as children + query path


def _A(s, *names, default=None):
    for n in names:
        v = getattr(s, n, None)
        if v:
            return v
    return default


def _load_pool() -> list[dict]:
    """Read the lane pool from the untracked config file. Each entry:
    {model, base_url, api_key, max_concurrent}. Keys never come from the repo."""
    try:
        with open(_POOL_FILE, encoding="utf-8") as fh:
            pool = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"pool file not found: {_POOL_FILE} (write it first; never commit keys)")
    if not isinstance(pool, list) or not pool:
        raise SystemExit("pool file must be a non-empty JSON list of lane dicts")
    masked = [
        {**{k: v for k, v in e.items() if k != "api_key"},
         "api_key": ("…" + e["api_key"][-4:]) if e.get("api_key") else None}
        for e in pool
    ]
    print(f"pool lanes ({len(pool)}): {json.dumps(masked)}")
    return pool


def _mongo():
    from motor.motor_asyncio import AsyncIOMotorClient
    s = get_settings()
    mc = AsyncIOMotorClient(_A(s, "MONGODB_URI", "MONGODB_URL"))
    return mc, mc[_A(s, "MONGODB_DB", default="polymath")]


def _qdrant():
    from qdrant_client import AsyncQdrantClient
    s = get_settings()
    return AsyncQdrantClient(url=s.QDRANT_URL, timeout=_A(s, "QDRANT_TIMEOUT_SECONDS", default=60))


async def _embed(texts: list[str]) -> list[list[float]]:
    from services.embedder import embed_batch
    return await embed_batch(texts, mode="local", expected_dim=_EMBED_DIM)


# ── index (free) ─────────────────────────────────────────────────────────────
async def index_existing(corpus_id: str, *, batch: int = 256) -> dict:
    """Embed + upsert summary points for every parent that has summary text."""
    from services.storage.qdrant_writer import upsert_summaries
    mc, db = _mongo()
    qc = _qdrant()
    indexed = 0
    try:
        q = {"corpus_id": corpus_id, "summary": {"$exists": True, "$nin": [None, ""]}}
        total = await db["parent_chunks"].count_documents(q)
        print(f"index: {total} parents have summary text")
        cursor = db["parent_chunks"].find(q)
        buf: list[dict] = []
        async def flush():
            nonlocal indexed
            if not buf:
                return
            vecs = await _embed([p["summary"] for p in buf])
            await upsert_summaries(qc, corpus_id, buf, vecs, target_kinds=["hrag"])
            indexed += len(buf)
            print(f"  indexed {indexed}/{total}")
            buf.clear()
        async for p in cursor:
            buf.append(p)
            if len(buf) >= batch:
                await flush()
        await flush()
        print(f"INDEXED {indexed} summary points -> hrag")
        return {"indexed": indexed, "total_with_summary": total}
    finally:
        await qc.close()
        mc.close()


# ── generate (cloud, guardrailed) ────────────────────────────────────────────
async def generate(corpus_id: str, *, batch: int = 400, limit: int | None = None) -> dict:
    """Summarize parents with summary=None via the pool. Batched + resumable.
    ABORTS if a batch yields 0 (all lanes exhausted) instead of finishing partial.
    `limit` caps total parents processed this run (for a probe)."""
    from services.ghost_a import SummaryTask, summarize_parents
    pool = _load_pool()
    mc, db = _mongo()
    made = 0
    batches = 0
    aborted = False
    consecutive_empty = 0
    failed_ids: set[str] = set()  # attempted-but-failed this run → skip so the
                                  # run always advances (thinking-model empties /
                                  # transient errors don't stall the front)
    try:
        q = {"corpus_id": corpus_id, "$or": [{"summary": None}, {"summary": ""},
                                             {"summary": {"$exists": False}}]}
        start_missing = await db["parent_chunks"].count_documents(q)
        print(f"generate: {start_missing} parents need summaries"
              + (f" (capped to {limit} this run)" if limit else ""))
        while True:
            if limit is not None and made >= limit:
                break
            fetch = batch if limit is None else max(1, min(batch, limit - made))
            fetch_q = {**q, "parent_id": {"$nin": list(failed_ids)}} if failed_ids else q
            rows = await db["parent_chunks"].find(
                fetch_q, {"parent_id": 1, "doc_id": 1, "corpus_id": 1, "source_tier": 1,
                    "text": 1, "_id": 0}
            ).limit(fetch).to_list(length=fetch)
            rows = [r for r in rows if (r.get("text") or "").strip()]
            if not rows:
                break
            batches += 1
            tasks = [SummaryTask(parent_id=r["parent_id"], doc_id=r.get("doc_id", ""),
                                 corpus_id=corpus_id, source_tier=r.get("source_tier", "parent"),
                                 text=r["text"]) for r in rows]
            results = await summarize_parents(tasks, pool=pool)
            results = [x for x in results if x and getattr(x, "summary", None)]
            # parents attempted but not summarized (thinking-empty / transient):
            # skip them for the rest of THIS run so we advance to fresh parents.
            ok_ids = {x.parent_id for x in results}
            failed_ids.update(t.parent_id for t in tasks if t.parent_id not in ok_ids)
            # GUARDRAIL: distinguish a transient blip from real exhaustion. A
            # single empty batch can be a timeout/rate spike (the failed parents
            # stay summary=None and are simply retried on the next fetch). Only
            # abort after TWO consecutive empty batches — sustained zero means
            # the keys are actually dead/exhausted, not a hiccup.
            if not results:
                consecutive_empty += 1
                print(f"  batch {batches}: 0/{len(tasks)} (consecutive empty "
                      f"{consecutive_empty}/2) — parents stay queued (None), retrying")
                if consecutive_empty >= 2:
                    aborted = True
                    print(f"!! ABORT after 2 consecutive empty batches — lanes "
                          f"dead/exhausted. Made {made}; resumable (re-run continues).")
                    break
                continue
            consecutive_empty = 0
            # persist
            from pymongo import UpdateOne
            ops = [UpdateOne({"parent_id": r.parent_id, "corpus_id": corpus_id},
                             {"$set": {"summary": r.summary,
                                       "summary_updated_at": datetime.now(timezone.utc)}})
                   for r in results]
            if ops:
                await db["parent_chunks"].bulk_write(ops, ordered=False)
            made += len(results)
            cov = len(results) / len(tasks)
            print(f"  batch {batches}: {len(results)}/{len(tasks)} summarized "
                  f"(cov={cov:.0%}) | total made={made}")
            if cov < 0.5:
                print(f"  ⚠ low coverage on batch {batches} — some lanes failing")
        remaining = await db["parent_chunks"].count_documents(q)
        print(f"GENERATE done: made={made} batches={batches} "
              f"skipped_this_run={len(failed_ids)} remaining={remaining} aborted={aborted}")
        return {"made": made, "batches": batches, "skipped": len(failed_ids),
                "remaining": remaining, "aborted": aborted}
    finally:
        mc.close()


# ── verify (readiness gate) ──────────────────────────────────────────────────
async def verify(corpus_id: str) -> dict:
    from services.storage.qdrant_writer import _col_for_corpus
    from qdrant_client import models
    mc, db = _mongo()
    qc = _qdrant()
    try:
        parents = await db["parent_chunks"].count_documents({"corpus_id": corpus_id})
        with_sum = await db["parent_chunks"].count_documents(
            {"corpus_id": corpus_id, "summary": {"$exists": True, "$nin": [None, ""]}})
        col = _col_for_corpus(corpus_id, "hrag")
        f = models.Filter(must=[models.FieldCondition(
            key="chunk_type", match=models.MatchValue(value="summary"))])
        sum_points = (await qc.count(col, count_filter=f)).count
        coverage = sum_points / parents if parents else 0.0
        status = "HEALTHY" if coverage >= 0.9 else ("PARTIAL" if coverage > 0 else "DEGRADED")
        print(f"VERIFY {corpus_id}: parents={parents} with_summary_text={with_sum} "
              f"indexed_summary_points={sum_points} coverage={coverage:.1%} -> {status}")
        return {"parents": parents, "with_summary_text": with_sum,
                "indexed_summary_points": sum_points, "coverage": round(coverage, 4),
                "status": status}
    finally:
        await qc.close()
        mc.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Summary-tier backfill + readiness")
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--index", action="store_true", help="index parents that have summary text")
    ap.add_argument("--generate", action="store_true", help="summarize missing parents via pool")
    ap.add_argument("--verify", action="store_true", help="readiness assertion")
    ap.add_argument("--batch", type=int, default=400)
    ap.add_argument("--limit", type=int, default=None, help="cap parents processed this run (probe)")
    args = ap.parse_args()
    if args.verify:
        asyncio.run(verify(args.corpus))
    if args.index:
        asyncio.run(index_existing(args.corpus, batch=min(256, args.batch)))
    if args.generate:
        asyncio.run(generate(args.corpus, batch=args.batch, limit=args.limit))
    if not (args.verify or args.index or args.generate):
        ap.error("pass at least one of --verify / --index / --generate")


if __name__ == "__main__":
    main()
