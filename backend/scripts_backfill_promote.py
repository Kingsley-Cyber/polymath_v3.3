"""B2 backfill — promote existing ghost_b_extractions into Qdrant child payloads.

ADDITIVE ONLY: set_payload merges promoted keys onto existing points (never
deletes/replaces a payload; identity keys never touched — promote() cannot emit
them). Idempotent: rerun ⇒ same values. Creates the payload indexes in the
same run (Stage-Contract: index ships with the field).

Usage (inside backend container):
    python scripts_backfill_promote.py <corpus_id>
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

from config import get_settings  # noqa: E402
import motor.motor_asyncio  # noqa: E402
from qdrant_client import AsyncQdrantClient  # noqa: E402
from qdrant_client import models as qm  # noqa: E402

from services.ingestion.promote import promote, promoted_index_fields  # noqa: E402
from services.storage import qdrant_writer as qw  # noqa: E402
from services.graph import neo4j_writer as nw  # noqa: E402


def _entity_id_fn(name: str) -> str:
    # the REAL graph identity fn so vector entity_ids == Neo4j entity_id
    for cand in ("entity_id_for", "_entity_id_for", "_entity_id", "make_entity_id"):
        fn = getattr(nw, cand, None)
        if callable(fn):
            try:
                return fn(name)
            except TypeError:
                try:
                    return fn(name, "")
                except Exception:
                    pass
    from services.ingestion.promote import _default_entity_id

    return _default_entity_id(name)


async def main(corpus_id: str) -> None:
    s = get_settings()
    db = motor.motor_asyncio.AsyncIOMotorClient(s.MONGODB_URI)[s.MONGODB_DATABASE]
    client = AsyncQdrantClient(url=s.QDRANT_URL, timeout=30)

    cols = []
    for kind in ("naive", "hrag", "graph"):
        name = qw._col_for_corpus(corpus_id, kind)
        if await client.collection_exists(name):
            cols.append(name)
    print(f"collections: {cols}")

    # 1) payload indexes ship first (idempotent)
    for col in cols:
        for field, ftype in promoted_index_fields():
            try:
                await client.create_payload_index(
                    collection_name=col,
                    field_name=field,
                    field_schema=qm.PayloadSchemaType.KEYWORD
                    if ftype == "keyword"
                    else qm.PayloadSchemaType.BOOL,
                )
            except Exception:
                pass  # exists — idempotent

    # 2) promote every ok extraction row
    rows = await db["ghost_b_extractions"].find(
        {"corpus_id": corpus_id, "status": "ok"}
    ).to_list(length=None)
    print(f"extraction rows: {len(rows)}")
    done = skipped = 0
    for row in rows:
        delta = promote(row, entity_id_fn=_entity_id_fn)
        chunk_id = str(row.get("chunk_id") or "")
        if not chunk_id:
            skipped += 1
            continue
        pid = qw._uuid_from_str(chunk_id)
        for col in cols:
            try:
                await client.set_payload(
                    collection_name=col, payload=delta, points=[pid]
                )
            except Exception as exc:  # point may not exist in this collection
                print(f"  warn {col} {chunk_id[:18]}: {exc}")
        # mirror onto the Mongo child record (denormalized RetrievalPayload)
        await db["chunks"].update_one(
            {"corpus_id": corpus_id, "chunk_id": chunk_id}, {"$set": delta}
        )
        done += 1
    print(f"promoted: {done}, skipped: {skipped}")

    # 3) verify: filter by a promoted field
    if done and cols:
        sample = promote(rows[0], entity_id_fn=_entity_id_fn)
        probe = (sample.get("entity_ids") or sample.get("concepts") or [None])[0]
        if probe:
            key = "entity_ids" if sample.get("entity_ids") else "concepts"
            cnt = await client.count(
                collection_name=cols[0],
                count_filter=qm.Filter(
                    must=[qm.FieldCondition(key=key, match=qm.MatchValue(value=probe))]
                ),
                exact=True,
            )
            print(f"VERIFY filter {key}={probe!r} → {cnt.count} points")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
