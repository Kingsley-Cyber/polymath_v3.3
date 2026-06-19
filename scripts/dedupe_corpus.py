#!/usr/bin/env python3
"""DETECT (and optionally CORRECT) near-duplicate documents in a corpus.

Deterministic shingle + containment scan — see services/ingestion/dedup.py.
Finds every cluster of near-duplicate documents already inside a corpus (the
stage filename-idempotency misses: the same book ingested as PDF *and* MD), and
can cascade-delete the redundant copies while keeping one canonical per cluster.

Safe by construction:
  * dry-run by default — prints what WOULD be deleted, touches nothing,
  * confidence tiers (certain / likely / review); --min-confidence gates which
    copies are eligible (default: only `certain` near-identical copies),
  * every real delete BACKS UP the doc record (dedup_deleted_backup) and VERIFIES
    no orphan chunks remain,
  * re-running after a delete is a no-op (the duplicates are gone).

Run inside the backend container:
  # detect only (read-only):
  docker exec -w /app polymath_v33-backend-1 python /app/scripts/dedupe_corpus.py <corpus_id>
  # correct the near-identical (certain) copies only — the safe default:
  docker exec -w /app polymath_v33-backend-1 python /app/scripts/dedupe_corpus.py <corpus_id> --apply
  # include the 'likely' tier too (still skips 'review'/distinct-content):
  docker exec -w /app polymath_v33-backend-1 python /app/scripts/dedupe_corpus.py <corpus_id> --apply --min-confidence likely
  # machine-readable:
  docker exec -w /app polymath_v33-backend-1 python /app/scripts/dedupe_corpus.py <corpus_id> --json
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, "/app")

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from services.ingestion import dedup  # noqa: E402
from services.ingestion_service import ingestion_service  # noqa: E402


def _arg_value(flag: str, default=None):
    for i, a in enumerate(sys.argv):
        if a == flag and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(flag + "="):
            return a.split("=", 1)[1]
    return default


async def main() -> int:
    corpus_id = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if not corpus_id:
        print("usage: dedupe_corpus.py <corpus_id> [--apply] "
              "[--min-confidence certain|likely|review] [--threshold 0.10] [--json]")
        return 2
    apply = "--apply" in sys.argv
    as_json = "--json" in sys.argv
    threshold = float(_arg_value("--threshold", dedup.DEFAULT_DUPLICATE_THRESHOLD))
    # Default eligibility for deletion: only near-identical ("certain") copies.
    min_confidence = _arg_value("--min-confidence", dedup.DUP_CERTAIN)

    client = AsyncIOMotorClient(os.environ["MONGODB_URI"])
    db = client.get_default_database()
    await ingestion_service.connect(db)

    clusters = await dedup.find_duplicate_clusters(
        ingestion_service.db, corpus_id, threshold=threshold
    )

    if as_json:
        summary = dedup.summarize_clusters(clusters)
        if apply:
            summary["resolution"] = await dedup.resolve_duplicate_clusters(
                ingestion_service, corpus_id, clusters,
                apply=True, min_confidence=min_confidence,
            )
        print(json.dumps(summary, indent=2))
        return 0

    print(f"\n=== Near-duplicate DETECT — corpus {corpus_id} (threshold={threshold}) ===")
    if not clusters:
        print("No near-duplicate document clusters found. Corpus is clean.\n")
        return 0

    s = dedup.summarize_clusters(clusters)
    bc = s["by_confidence"]
    print(
        f"{s['cluster_count']} cluster(s), {s['duplicate_document_count']} redundant "
        f"document(s), {s['redundant_chunk_count']} redundant chunk(s).\n"
        f"confidence: certain={bc['certain']}  likely={bc['likely']}  review={bc['review']}\n"
    )
    for n, c in enumerate(clusters, 1):
        print(f"Cluster {n}  [{c.confidence.upper()}]  max_sim={c.max_similarity:.3f}:")
        for m in c.members:
            if m.is_canonical:
                print(f"  [KEEP ] {m.doc_id} {m.filename!r}")
                print(f"          chunks={m.chunk_count} retrievable={m.retrievable_count} "
                      f"shingles={m.shingle_count}")
            else:
                print(f"  [drop·{m.confidence}] {m.doc_id} {m.filename!r}")
                print(f"          chunks={m.chunk_count} retrievable={m.retrievable_count} "
                      f"shingles={m.shingle_count} sim={m.similarity_to_canonical:.3f} "
                      f"containment={m.containment_to_canonical:.3f}")
        print()

    if not apply:
        print("Dry-run only. Re-run with --apply to delete eligible 'drop' copies "
              f"(--min-confidence={min_confidence}).\n")
        return 0

    print(f"Applying (min-confidence={min_confidence}) — backup + cascade-delete...\n")
    result = await dedup.resolve_duplicate_clusters(
        ingestion_service, corpus_id, clusters,
        apply=True, min_confidence=min_confidence,
    )
    print(
        f"Done. Deleted {result.get('documents_deleted')} document(s), "
        f"freed {result.get('chunks_freed')} chunk(s), "
        f"skipped {result.get('skipped_low_confidence')} below confidence, "
        f"errors={result.get('errors')}.\n"
    )
    for a in result.get("actions", []):
        if a.get("skipped_low_confidence"):
            continue
        if a.get("applied"):
            v = a.get("verify") or {}
            orphans = (v.get("orphan_chunks", 0) or 0) + (v.get("orphan_parents", 0) or 0)
            status = "OK" + (f"  ⚠ {orphans} orphan rows" if orphans else "")
        else:
            status = f"FAILED ({a.get('error')})"
        print(f"  - {a['delete_doc_id']} {a['delete_filename']!r}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
