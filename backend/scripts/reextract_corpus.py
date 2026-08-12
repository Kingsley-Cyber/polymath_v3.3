"""Re-extract a corpus under the current schema + compiler (owner-ordered).

Why a re-extraction is needed rather than an incremental pass: the corpus
entity_schema changed (global 15-class -> domain vocabulary), and the
extraction contract hash does not cover entity_schema, so existing terminal
jobs would otherwise be treated as current and never re-run.

What it does, per corpus, in order:
  1. delete ghost_b_extractions rows (the stale proposal ledger)
  2. reset every extraction job to queued, clearing lease fields
  3. clear graph-promotion jobs so the graph is rebuilt from compiled facts

Safety and idempotency:
  - scoped to ONE corpus per invocation, named explicitly on the CLI
  - requires --confirm to do anything; without it, reports only
  - re-running is safe: the same corpus simply requeues again
  - nothing outside the named corpus is touched

Usage:
  docker exec polymath_v33-backend-1 python /app/scripts/reextract_corpus.py \
      --corpus video-generation-school --confirm
"""
import argparse
import asyncio
import sys

sys.path.insert(0, "/app")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="corpus NAME (not id)")
    ap.add_argument("--confirm", action="store_true", help="actually perform the reset")
    args = ap.parse_args()

    from services.conversation import conversation_service

    await conversation_service.connect()
    db = conversation_service._db

    corpus = await db["corpora"].find_one(
        {"name": args.corpus}, {"corpus_id": 1, "ontology_domain": 1, "default_ingestion_config": 1}
    )
    if not corpus:
        print(f"corpus {args.corpus!r} not found")
        return
    cid = corpus["corpus_id"]
    cfg = corpus.get("default_ingestion_config") or {}

    rows = await db["ghost_b_extractions"].count_documents({"corpus_id": cid})
    jobs = await db["extraction_jobs"].count_documents({"corpus_id": cid})
    body = await db["chunks"].count_documents({"corpus_id": cid, "chunk_kind": "body"})
    print(f"corpus            : {args.corpus} ({cid[:12]})")
    print(f"ontology_domain   : {corpus.get('ontology_domain')}")
    print(f"entity_schema     : {len(cfg.get('entity_schema') or [])} labels")
    print(f"engine            : {cfg.get('extraction_engine')}")
    print(f"body chunks       : {body}")
    print(f"extraction rows   : {rows}   (will be DELETED)")
    print(f"extraction jobs   : {jobs}   (will be REQUEUED)")

    if not args.confirm:
        print("\ndry run — pass --confirm to execute")
        return

    deleted = await db["ghost_b_extractions"].delete_many({"corpus_id": cid})
    requeued = await db["extraction_jobs"].update_many(
        {"corpus_id": cid},
        {
            "$set": {"status": "queued", "attempts": 0},
            "$unset": {
                "lease_expires_at": "",
                "lease_until": "",
                "claimed_by": "",
                "failure": "",
            },
        },
    )
    promos = await db["graph_promotion_jobs"].update_many(
        {"corpus_id": cid},
        {"$set": {"status": "queued"}, "$unset": {"lease_expires_at": "", "claimed_by": ""}},
    )
    await db["ingest_lane_leases"].delete_many({"corpus_id": cid})
    print(
        f"\ndeleted {deleted.deleted_count} extraction rows | "
        f"requeued {requeued.modified_count} jobs | "
        f"reset {promos.modified_count} promotion jobs | lanes cleared"
    )
    print("workers pick this up on the next claim.")


if __name__ == "__main__":
    asyncio.run(main())
