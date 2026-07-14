"""B4 groundwork — multitenancy migration (owner design §4). FLAG-GATED OFF.

Copies a corpus's child points from its per-corpus collection into the SHARED
`polymath_children` collection (corpus_id already lives indexed in every
payload — the multitenancy key), and embeds each document's L4 profile
(documents.doc_profile.summary, built by B3) into the SHARED
`polymath_doc_summaries` collection — the ONLY embedded summaries under the
owner design (Tier-0 routing).

ADDITIVE + reversible: source collections untouched; retrieval keeps using
per-corpus collections until QDRANT_SHARED_COLLECTIONS flips (owner call).
Idempotent: deterministic point ids ⇒ re-run overwrites the same points.

Usage: python scripts_migrate_multitenant.py <corpus_id>
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, "/app")

from config import get_settings  # noqa: E402
import motor.motor_asyncio  # noqa: E402
from qdrant_client import AsyncQdrantClient  # noqa: E402
from qdrant_client import models as qm  # noqa: E402

from services.storage import qdrant_writer as qw  # noqa: E402

SHARED_CHILDREN = "polymath_children"
SHARED_DOCSUM = "polymath_doc_summaries"
_INDEX_FIELDS = [
    "corpus_id", "doc_id", "chunk_id", "parent_id", "chunk_type", "chunk_kind",
    "language", "domain", "concepts", "entity_ids", "relation_families",
    "fact_types",
]


async def _ensure(client: AsyncQdrantClient, name: str, dim: int) -> None:
    await qw._create_collection_with_retry(
        client,
        collection_name=name,
        vectors_config={
            "dense": qm.VectorParams(size=dim, distance=qm.Distance.COSINE)
        },
        sparse_vectors_config={
            "sparse": qm.SparseVectorParams(modifier=qm.Modifier.IDF)
        },
        quantization_config=qw.binary_quantization_config(),
    )
    await qw.ensure_binary_quantization(client, name)
    for f in _INDEX_FIELDS:
        try:
            await client.create_payload_index(
                collection_name=name, field_name=f,
                field_schema=qm.PayloadSchemaType.KEYWORD)
        except Exception:
            pass


async def main(corpus_id: str) -> None:
    s = get_settings()
    db = motor.motor_asyncio.AsyncIOMotorClient(s.MONGODB_URI)[s.MONGODB_DATABASE]
    client = AsyncQdrantClient(url=s.QDRANT_URL, timeout=60)

    src = qw._col_for_corpus(corpus_id, "naive")
    info = await client.get_collection(src)
    vecs = info.config.params.vectors
    dim = vecs["dense"].size if isinstance(vecs, dict) else vecs.size
    named = isinstance(vecs, dict)
    print(f"source={src} dim={dim} named_vectors={named}")
    await _ensure(client, SHARED_CHILDREN, dim)
    await _ensure(client, SHARED_DOCSUM, dim)

    # ── children: copy points (vectors + payload) into the shared collection ──
    moved = 0
    offset = None
    while True:
        points, offset = await client.scroll(
            collection_name=src, limit=128, offset=offset,
            with_payload=True, with_vectors=True)
        if not points:
            break
        batch = []
        for p in points:
            vec = p.vector if isinstance(p.vector, dict) else {"dense": p.vector}
            vec = {k: v for k, v in vec.items() if k in ("dense", "sparse") and v is not None}
            batch.append(qm.PointStruct(id=p.id, vector=vec, payload=p.payload or {}))
        await client.upsert(collection_name=SHARED_CHILDREN, points=batch)
        moved += len(batch)
        if offset is None:
            break
    print(f"children migrated: {moved}")

    # ── doc profiles: embed L4 summaries → shared Tier-0 collection ──────────
    from services.embedder import embed_batch

    docs = await db["documents"].find(
        {"corpus_id": corpus_id, "doc_profile.summary": {"$exists": True, "$ne": ""}},
        {"doc_id": 1, "title": 1, "doc_profile": 1, "source_type": 1},
    ).to_list(length=None)
    if docs:
        texts = [d["doc_profile"]["summary"] for d in docs]
        vectors = await embed_batch(texts, dim)
        pts = []
        for d, v in zip(docs, vectors):
            pts.append(qm.PointStruct(
                id=qw._uuid_from_str(f"{corpus_id}:{d['doc_id']}:doc_profile"),
                vector={"dense": v},
                payload={
                    "corpus_id": corpus_id,
                    "doc_id": d["doc_id"],
                    "chunk_type": "doc_summary",
                    "title": d.get("title") or "",
                    "source_type": d.get("source_type") or "",
                    "summary": d["doc_profile"]["summary"],
                    "concepts": d["doc_profile"].get("concepts") or [],
                    "section_ids": d["doc_profile"].get("section_ids") or [],
                },
            ))
        await client.upsert(collection_name=SHARED_DOCSUM, points=pts)
    print(f"doc profiles embedded: {len(docs)}")

    # ── verify: shared count matches source; corpus filter works ─────────────
    src_n = (await client.count(collection_name=src, exact=True)).count
    flt = qm.Filter(must=[qm.FieldCondition(key="corpus_id", match=qm.MatchValue(value=corpus_id))])
    shared_n = (await client.count(collection_name=SHARED_CHILDREN, count_filter=flt, exact=True)).count
    print(f"VERIFY children: source={src_n} shared[corpus_id]={shared_n} match={src_n == shared_n}")
    ds_n = (await client.count(collection_name=SHARED_DOCSUM, count_filter=flt, exact=True)).count
    print(f"VERIFY doc_summaries[corpus_id]={ds_n}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
