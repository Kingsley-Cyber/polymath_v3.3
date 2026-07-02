"""Backfill child-chunk `domain` from Ghost-A parent domains (M1, 2026-07-02).

Ghost A assigns a taxonomy `domain` to every parent_chunk, but children and
their Qdrant points never carried it — so the retrieval layer could not
filter or tie-break by discipline (the trait=type_traits polysemy root).
This copies parent.domain -> child (Mongo `chunks`) and -> Qdrant child
payload, for VALID taxonomy labels only (Cluster*/Outliers/other/null are
left unset — they are not filterable signals).

Deterministic, idempotent, non-destructive (only sets `domain`). Resumable:
re-running skips children already carrying the correct domain.

Usage (inside the backend container):
  python -m scripts.backfill_child_domain --corpus <id> [--dry-run] [--verify]
  python -m scripts.backfill_child_domain --all      [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from config import get_settings
from motor.motor_asyncio import AsyncIOMotorClient
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

from services.storage import qdrant_writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill_child_domain")

_TAXONOMY_REJECT = {"other", "outliers", "unknown", "null", "none", ""}


def _is_taxonomy_domain(value) -> bool:
    text = str(value or "").strip().lower()
    if text in _TAXONOMY_REJECT:
        return False
    return not text.startswith("cluster")


async def _backfill_corpus(
    db, qdrant: AsyncQdrantClient, corpus_id: str, *, dry_run: bool
) -> dict:
    # 1) Build parent_id -> domain map (valid taxonomy only).
    domain_by_parent: dict[str, str] = {}
    cursor = db["parent_chunks"].find(
        {"corpus_id": corpus_id, "domain": {"$exists": True, "$ne": None}},
        {"_id": 0, "parent_id": 1, "domain": 1},
    )
    async for row in cursor:
        dom = row.get("domain")
        if _is_taxonomy_domain(dom):
            domain_by_parent[str(row["parent_id"])] = str(dom).strip()

    # 2) Group children by domain via their parent, then update Mongo + Qdrant.
    #    (Group so Qdrant set_payload runs once per domain, not once per point.)
    updated_mongo = 0
    updated_qdrant = 0
    by_domain: dict[str, list[str]] = {}
    cursor = db["chunks"].find(
        {"corpus_id": corpus_id},
        {"_id": 0, "chunk_id": 1, "parent_id": 1, "domain": 1},
    )
    async for row in cursor:
        parent_id = str(row.get("parent_id") or "")
        dom = domain_by_parent.get(parent_id)
        if not dom:
            continue
        if row.get("domain") == dom:
            continue  # idempotent skip
        by_domain.setdefault(dom, []).append(str(row["chunk_id"]))

    if dry_run:
        total = sum(len(v) for v in by_domain.values())
        log.info(
            "[dry-run] corpus=%s parents_with_domain=%d children_to_set=%d across %d domains",
            corpus_id[:8], len(domain_by_parent), total, len(by_domain),
        )
        return {"corpus": corpus_id, "would_set": total, "domains": len(by_domain)}

    col = qdrant_writer._col_for_corpus(corpus_id, "hrag")
    naive_col = qdrant_writer._col_for_corpus(corpus_id, "naive")
    for dom, chunk_ids in by_domain.items():
        # Mongo: one bulk update per domain group.
        res = await db["chunks"].update_many(
            {"corpus_id": corpus_id, "chunk_id": {"$in": chunk_ids}},
            {"$set": {"domain": dom}},
        )
        updated_mongo += res.modified_count
        # Qdrant: set_payload filtered to this domain's chunk_ids. Try both the
        # hrag and naive collections (per-corpus layout varies); ignore misses.
        for collection in (col, naive_col):
            try:
                await qdrant.set_payload(
                    collection_name=collection,
                    payload={"domain": dom},
                    points=qm.Filter(
                        must=[
                            qm.FieldCondition(
                                key="chunk_id", match=qm.MatchAny(any=chunk_ids)
                            )
                        ]
                    ),
                    wait=False,
                )
                updated_qdrant += len(chunk_ids)
            except Exception as exc:  # noqa: BLE001
                log.debug("qdrant set_payload skipped %s: %s", collection, exc)

    # Ensure the domain payload index exists (idempotent, wait=True so the
    # field is immediately filterable — Qdrant rejects MatchValue filters on
    # un-indexed keyword fields).
    for collection in (col, naive_col):
        try:
            await qdrant.create_payload_index(
                collection_name=collection,
                field_name="domain",
                field_schema=qm.PayloadSchemaType.KEYWORD,
                wait=True,
            )
        except Exception:  # noqa: BLE001
            pass

    log.info(
        "corpus=%s mongo_set=%d qdrant_set~=%d domains=%d",
        corpus_id[:8], updated_mongo, updated_qdrant, len(by_domain),
    )
    return {
        "corpus": corpus_id,
        "mongo_set": updated_mongo,
        "qdrant_set": updated_qdrant,
        "domains": len(by_domain),
    }


async def _verify(db, corpus_id: str) -> None:
    total = await db["chunks"].count_documents({"corpus_id": corpus_id})
    with_dom = await db["chunks"].count_documents(
        {"corpus_id": corpus_id, "domain": {"$exists": True, "$ne": None}}
    )
    pct = round(100 * with_dom / total, 1) if total else 0.0
    log.info("VERIFY corpus=%s children_with_domain=%d/%d (%s%%)", corpus_id[:8], with_dom, total, pct)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client.get_default_database()
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL, timeout=settings.QDRANT_TIMEOUT_SECONDS)

    if args.all:
        corpus_ids = await db["corpora"].distinct("corpus_id")
    elif args.corpus:
        corpus_ids = [args.corpus]
    else:
        ap.error("pass --corpus <id> or --all")

    for cid in corpus_ids:
        await _backfill_corpus(db, qdrant, cid, dry_run=args.dry_run)
        if args.verify and not args.dry_run:
            await _verify(db, cid)


if __name__ == "__main__":
    asyncio.run(main())
