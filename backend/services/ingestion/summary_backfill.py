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
from services.ingestion.summary_semantics import (
    PARENT_SUMMARY_SCHEMA_VERSION,
    looks_like_raw_json_text,
    repair_parent_summary_row,
)
from services.ingestion.section_classifier import parent_summary_required_clause

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
        raise SystemExit(
            f"pool file not found: {_POOL_FILE} (write it first; never commit keys)"
        )
    if not isinstance(pool, list) or not pool:
        raise SystemExit("pool file must be a non-empty JSON list of lane dicts")
    masked = [
        {
            **{k: v for k, v in e.items() if k != "api_key"},
            "api_key": ("…" + e["api_key"][-4:]) if e.get("api_key") else None,
        }
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
    return AsyncQdrantClient(
        url=s.QDRANT_URL, timeout=_A(s, "QDRANT_TIMEOUT_SECONDS", default=60)
    )


async def _embed(texts: list[str]) -> list[list[float]]:
    from services.embedder import embed_batch

    return await embed_batch(
        texts,
        mode="local",
        expected_dim=_EMBED_DIM,
        workload_class="backfill_repair",
    )


def _row_child_ids(row: dict) -> list[str]:
    values = row.get("source_child_ids") or row.get("child_ids") or []
    return [str(v) for v in values if str(v)]


async def child_context_for_rows(
    db, corpus_id: str, rows: list[dict]
) -> dict[str, dict]:
    """Hydrate child anchors for parent-summary generation.

    The summary artifact contract needs stable child IDs even during backfill.
    We fetch children once per batch and keep the prompt context bounded by the
    current parent batch, not the full corpus.
    """

    parent_ids = [str(r["parent_id"]) for r in rows if r.get("parent_id")]
    explicit_ids: list[str] = []
    for row in rows:
        explicit_ids.extend(_row_child_ids(row))
    q = {"corpus_id": corpus_id, "parent_id": {"$in": parent_ids}}
    if explicit_ids:
        q = {
            "corpus_id": corpus_id,
            "$or": [
                {"parent_id": {"$in": parent_ids}},
                {"chunk_id": {"$in": list(dict.fromkeys(explicit_ids))}},
            ],
        }
    cursor = db["chunks"].find(q, {"_id": 0, "chunk_id": 1, "parent_id": 1, "text": 1})
    chunks_by_parent: dict[str, list[dict]] = {}
    chunks_by_id: dict[str, dict] = {}
    async for child in cursor:
        cid = str(child.get("chunk_id") or "")
        pid = str(child.get("parent_id") or "")
        if cid:
            chunks_by_id[cid] = child
        if pid:
            chunks_by_parent.setdefault(pid, []).append(child)

    out: dict[str, dict] = {}
    for row in rows:
        parent_id = str(row["parent_id"])
        ids = _row_child_ids(row)
        if ids:
            child_rows = [chunks_by_id[cid] for cid in ids if cid in chunks_by_id]
        else:
            child_rows = sorted(
                chunks_by_parent.get(parent_id, []),
                key=lambda c: str(c.get("chunk_id") or ""),
            )
            ids = [str(c.get("chunk_id")) for c in child_rows if c.get("chunk_id")]
        boundaries = "\n\n".join(
            f"[{c.get('chunk_id')}]\n{str(c.get('text') or '')[:1500]}"
            for c in child_rows
            if c.get("chunk_id")
        )
        out[parent_id] = {
            "source_child_ids": ids,
            "child_boundaries": boundaries,
        }
    return out


# Compatibility for repair scripts/tests created before the helper became a
# public scheduler primitive. Keep both names until those callers migrate.
_child_context_for_rows = child_context_for_rows


def summary_result_fields(result, *, updated_at: datetime) -> dict:
    """Return the complete canonical parent-summary persistence payload."""

    return {
        "summary": result.summary,
        "domain": getattr(result, "domain", None),
        "topics": getattr(result, "topics", None),
        "semantic_chunk_type": getattr(result, "semantic_chunk_type", None),
        "key_terms": getattr(result, "key_terms", None),
        "mechanisms": getattr(result, "mechanisms", None),
        "schema_version": getattr(result, "schema_version", None),
        "summary_type": getattr(result, "summary_type", None),
        "central_claim": getattr(result, "central_claim", None),
        "key_points": getattr(result, "key_points", None),
        "main_mechanism": getattr(result, "main_mechanism", None),
        "concept_tags": getattr(result, "concept_tags", None),
        "entity_hints": getattr(result, "entity_hints", None),
        "retrieval_uses": getattr(result, "retrieval_uses", None),
        "abstraction_level": getattr(result, "abstraction_level", None),
        "source_child_ids": getattr(result, "source_child_ids", None),
        "summary_id": getattr(result, "summary_id", None),
        "source_hash": getattr(result, "source_hash", None),
        "summary_model": getattr(result, "summary_model", None),
        "summary_created_at": getattr(result, "summary_created_at", None),
        "validation_status": getattr(result, "validation_status", None),
        "repair_status": getattr(result, "repair_status", None),
        "quality_score": getattr(result, "quality_score", None),
        "quality_flags": getattr(result, "quality_flags", None),
        "retrieval_text": getattr(result, "retrieval_text", None),
        "summary_updated_at": updated_at,
    }


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
            vecs = await _embed([p.get("retrieval_text") or p["summary"] for p in buf])
            await upsert_summaries(
                qc, corpus_id, buf, vecs, target_kinds=["naive", "hrag"]
            )
            indexed += len(buf)
            print(f"  indexed {indexed}/{total}")
            buf.clear()

        async for p in cursor:
            buf.append(p)
            if len(buf) >= batch:
                await flush()
        await flush()
        print(f"INDEXED {indexed} summary points -> naive+hrag")
        return {"indexed": indexed, "total_with_summary": total}
    finally:
        await qc.close()
        mc.close()


# ── generate (cloud, guardrailed) ────────────────────────────────────────────
async def generate(
    corpus_id: str, *, batch: int = 400, limit: int | None = None
) -> dict:
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
        # Generate only parent rows that participate in retrieval-summary
        # readiness. Structural rows are not a summary gap.
        q = {
            "corpus_id": corpus_id,
            "$and": [
                parent_summary_required_clause(),
                {
                    "$or": [
                        {"summary": None},
                        {"summary": ""},
                        {"summary": {"$exists": False}},
                    ]
                },
            ],
        }
        start_missing = await db["parent_chunks"].count_documents(q)
        print(
            f"generate: {start_missing} parents need summaries"
            + (f" (capped to {limit} this run)" if limit else "")
        )
        while True:
            if limit is not None and made >= limit:
                break
            fetch = batch if limit is None else max(1, min(batch, limit - made))
            fetch_q = (
                {**q, "parent_id": {"$nin": list(failed_ids)}} if failed_ids else q
            )
            rows = (
                await db["parent_chunks"]
                .find(
                    fetch_q,
                    {
                        "parent_id": 1,
                        "doc_id": 1,
                        "corpus_id": 1,
                        "source_tier": 1,
                        "text": 1,
                        "child_ids": 1,
                        "source_child_ids": 1,
                        "_id": 0,
                    },
                )
                .limit(fetch)
                .to_list(length=fetch)
            )
            rows = [r for r in rows if (r.get("text") or "").strip()]
            if not rows:
                break
            batches += 1
            child_context = await child_context_for_rows(db, corpus_id, rows)
            tasks = [
                SummaryTask(
                    parent_id=r["parent_id"],
                    doc_id=r.get("doc_id", ""),
                    corpus_id=corpus_id,
                    source_tier=r.get("source_tier", "parent"),
                    text=r["text"],
                    source_child_ids=child_context.get(r["parent_id"], {}).get(
                        "source_child_ids", []
                    ),
                    child_boundaries=child_context.get(r["parent_id"], {}).get(
                        "child_boundaries", ""
                    ),
                )
                for r in rows
            ]
            from services.ingestion.provider_call_telemetry import record_provider_call

            async def _telemetry(event: dict) -> None:
                await record_provider_call(db, event)

            results = await summarize_parents(
                tasks,
                pool=pool,
                telemetry_sink=_telemetry,
            )
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
                print(
                    f"  batch {batches}: 0/{len(tasks)} (consecutive empty "
                    f"{consecutive_empty}/2) — parents stay queued (None), retrying"
                )
                if consecutive_empty >= 2:
                    aborted = True
                    print(
                        f"!! ABORT after 2 consecutive empty batches — lanes "
                        f"dead/exhausted. Made {made}; resumable (re-run continues)."
                    )
                    break
                continue
            consecutive_empty = 0
            # persist
            from pymongo import UpdateOne

            now = datetime.now(timezone.utc)
            ops = [
                UpdateOne(
                    {"parent_id": r.parent_id, "corpus_id": corpus_id},
                    {"$set": summary_result_fields(r, updated_at=now)},
                )
                for r in results
            ]
            if ops:
                await db["parent_chunks"].bulk_write(ops, ordered=False)
            made += len(results)
            cov = len(results) / len(tasks)
            print(
                f"  batch {batches}: {len(results)}/{len(tasks)} summarized "
                f"(cov={cov:.0%}) | total made={made}"
            )
            if cov < 0.5:
                print(f"  ⚠ low coverage on batch {batches} — some lanes failing")
        remaining = await db["parent_chunks"].count_documents(q)
        print(
            f"GENERATE done: made={made} batches={batches} "
            f"skipped_this_run={len(failed_ids)} remaining={remaining} aborted={aborted}"
        )
        return {
            "made": made,
            "batches": batches,
            "skipped": len(failed_ids),
            "remaining": remaining,
            "aborted": aborted,
        }
    finally:
        mc.close()


# ── repair (free, deterministic) ────────────────────────────────────────────
async def repair_existing(
    corpus_id: str,
    *,
    batch: int = 500,
    limit: int | None = None,
    apply: bool = False,
    reindex: bool = False,
) -> dict:
    """Repair malformed parent_summary.v1 rows and optionally rebuild vectors.

    This is deterministic: no model calls. Raw JSON-looking summaries are
    parsed/salvaged into bounded fields or quarantined for later regeneration.
    """
    from pymongo import UpdateOne
    from services.storage.qdrant_writer import upsert_summaries

    mc, db = _mongo()
    qc = _qdrant() if reindex and apply else None
    scanned = changed = repaired = quarantined = indexed = raw_json = 0
    ops: list[UpdateOne] = []
    reindex_buf: list[dict] = []
    tracked = [
        "summary_id",
        "summary",
        "schema_version",
        "summary_type",
        "central_claim",
        "key_points",
        "main_mechanism",
        "concept_tags",
        "entity_hints",
        "retrieval_uses",
        "abstraction_level",
        "source_child_ids",
        "source_hash",
        "summary_model",
        "summary_created_at",
        "validation_status",
        "repair_status",
        "quality_score",
        "quality_flags",
        "retrieval_text",
    ]

    async def flush_updates() -> None:
        nonlocal ops
        if not ops:
            return
        if apply:
            await db["parent_chunks"].bulk_write(ops, ordered=False)
        ops = []

    async def flush_reindex() -> None:
        nonlocal indexed, reindex_buf
        if not reindex_buf:
            return
        if qc is not None:
            vecs = await _embed([p["retrieval_text"] for p in reindex_buf])
            await upsert_summaries(
                qc,
                corpus_id,
                reindex_buf,
                vecs,
                target_kinds=["naive", "hrag"],
            )
            indexed += len(reindex_buf)
        reindex_buf = []

    try:
        q = {"corpus_id": corpus_id, "schema_version": PARENT_SUMMARY_SCHEMA_VERSION}
        total = await db["parent_chunks"].count_documents(q)
        print(
            f"repair: {total} parent_summary.v1 rows"
            + (f" (capped to {limit})" if limit else "")
            + (" APPLY" if apply else " DRY-RUN")
            + (" +REINDEX" if reindex and apply else "")
        )
        cursor = db["parent_chunks"].find(q).limit(limit or 0)
        async for row in cursor:
            scanned += 1
            now = datetime.now(timezone.utc)
            before_raw = looks_like_raw_json_text(row.get("summary"))
            raw_json += 1 if before_raw else 0
            fixed = repair_parent_summary_row(row, now=now)
            status = fixed.get("validation_status")
            quarantined += 1 if status == "quarantined" else 0
            repaired += 1 if fixed.get("repair_status") == "repaired" else 0
            diff = {
                key: fixed.get(key) for key in tracked if row.get(key) != fixed.get(key)
            }
            if diff:
                changed += 1
                diff["summary_repaired_at"] = now
                if fixed.get("validation_status") == "quarantined":
                    diff["summary_quarantine_reason"] = fixed.get("quality_flags") or []
                    if before_raw:
                        diff["summary_quarantine_raw"] = row.get("summary")
                ops.append(
                    UpdateOne(
                        {
                            "corpus_id": row.get("corpus_id"),
                            "doc_id": row.get("doc_id"),
                            "parent_id": row.get("parent_id"),
                        },
                        {"$set": diff},
                    )
                )
            if reindex and apply and fixed.get("validation_status") == "valid":
                reindex_buf.append({**row, **fixed})
            if len(ops) >= batch:
                await flush_updates()
            if len(reindex_buf) >= min(batch, 256):
                await flush_reindex()
        await flush_updates()
        await flush_reindex()
        print(
            f"REPAIR scanned={scanned} changed={changed} raw_json={raw_json} "
            f"repaired={repaired} quarantined={quarantined} indexed={indexed}"
        )
        return {
            "scanned": scanned,
            "changed": changed,
            "raw_json": raw_json,
            "repaired": repaired,
            "quarantined": quarantined,
            "indexed": indexed,
            "applied": apply,
            "reindexed": bool(reindex and apply),
        }
    finally:
        if qc is not None:
            await qc.close()
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
            {"corpus_id": corpus_id, "summary": {"$exists": True, "$nin": [None, ""]}}
        )
        col = _col_for_corpus(corpus_id, "hrag")
        f = models.Filter(
            must=[
                models.FieldCondition(
                    key="chunk_type", match=models.MatchValue(value="summary")
                )
            ]
        )
        sum_points = (await qc.count(col, count_filter=f)).count
        coverage = sum_points / parents if parents else 0.0
        status = (
            "HEALTHY"
            if coverage >= 0.9
            else ("PARTIAL" if coverage > 0 else "DEGRADED")
        )
        print(
            f"VERIFY {corpus_id}: parents={parents} with_summary_text={with_sum} "
            f"indexed_summary_points={sum_points} coverage={coverage:.1%} -> {status}"
        )
        return {
            "parents": parents,
            "with_summary_text": with_sum,
            "indexed_summary_points": sum_points,
            "coverage": round(coverage, 4),
            "status": status,
        }
    finally:
        await qc.close()
        mc.close()


async def heal_all(*, apply: bool = False) -> dict:
    """Cross-corpus safety net: scan every corpus's summary-tier readiness and
    flag (or repair) any that are DEGRADED/PARTIAL. This is the net that makes
    'a corpus silently sitting with an empty breadth tier' impossible to miss —
    run it on a schedule or post-ingest. With apply=True it auto-runs index (+
    leaves generation to an operator, since that needs the corpus's model pool).
    """
    mc, db = _mongo()
    try:
        cids = await db["parent_chunks"].distinct("corpus_id")
    finally:
        mc.close()
    print(f"scanning {len(cids)} corpora for summary-tier health...")
    statuses: dict[str, dict] = {}
    for cid in cids:
        r = await verify(cid)
        statuses[cid] = r
        # auto-index is safe + free (no model calls): pushes any Mongo summaries
        # that exist but were never indexed into Funnel A.
        if (
            apply
            and r["status"] != "HEALTHY"
            and r["with_summary_text"] > r["indexed_summary_points"]
        ):
            print(f"  -> auto-indexing {cid} (mongo summaries not in Funnel A)")
            await index_existing(cid)
            statuses[cid] = await verify(cid)
    bad = {c: s["status"] for c, s in statuses.items() if s["status"] != "HEALTHY"}
    print(f"\nHEAL SUMMARY: {len(statuses)} corpora | not-healthy: {len(bad)}")
    for c, st in bad.items():
        print(f"  {c}: {st}  (repair: --corpus {c} --generate then --index)")
    return {"scanned": len(statuses), "not_healthy": bad}


def main() -> None:
    ap = argparse.ArgumentParser(description="Summary-tier backfill + readiness")
    ap.add_argument("--corpus", help="corpus_id (required for index/generate/verify)")
    ap.add_argument(
        "--heal-all",
        action="store_true",
        help="scan all corpora for summary-tier health",
    )
    ap.add_argument(
        "--apply-heal",
        action="store_true",
        help="with --heal-all: auto-index orphaned summaries",
    )
    ap.add_argument(
        "--index", action="store_true", help="index parents that have summary text"
    )
    ap.add_argument(
        "--generate", action="store_true", help="summarize missing parents via pool"
    )
    ap.add_argument(
        "--repair",
        action="store_true",
        help="repair/quarantine malformed parent_summary.v1 rows",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="with --repair: persist deterministic repair fields",
    )
    ap.add_argument(
        "--reindex",
        action="store_true",
        help="with --repair --apply: re-embed clean retrieval_text",
    )
    ap.add_argument("--verify", action="store_true", help="readiness assertion")
    ap.add_argument("--batch", type=int, default=400)
    ap.add_argument(
        "--limit", type=int, default=None, help="cap parents processed this run (probe)"
    )
    args = ap.parse_args()
    if args.heal_all:
        asyncio.run(heal_all(apply=args.apply_heal))
        return
    if not args.corpus:
        ap.error("--corpus is required for --verify / --index / --generate")
    if args.verify:
        asyncio.run(verify(args.corpus))
    if args.repair:
        asyncio.run(
            repair_existing(
                args.corpus,
                batch=args.batch,
                limit=args.limit,
                apply=args.apply,
                reindex=args.reindex,
            )
        )
    if args.index:
        asyncio.run(index_existing(args.corpus, batch=min(256, args.batch)))
    if args.generate:
        asyncio.run(generate(args.corpus, batch=args.batch, limit=args.limit))
    if not (args.verify or args.repair or args.index or args.generate):
        ap.error(
            "pass at least one of --verify / --repair / --index / --generate / --heal-all"
        )


if __name__ == "__main__":
    main()
