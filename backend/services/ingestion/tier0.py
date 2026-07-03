"""W1 Tier-0 — auto-embed the document ROUTING CARD at ingest.

§10.2/§12.0: the doc_profile is a routing card, NEVER evidence. This module
embeds `documents.doc_profile.summary` into the UNIVERSAL
`polymath_doc_summaries` collection (corpus_id in payload, §11.0 ratified:
routing must see across corpora) so Tier-0 routing has a vector to search.

Consumption stays GATED behind TIER0_ROUTING (default false, zero query-path
call sites — probe via scripts_probe_tier0.py). This writer is additive and
best-effort: a failure logs a warning and never fails the ingest.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SHARED_DOCSUM = "polymath_doc_summaries"
_INDEX_FIELDS = ["corpus_id", "doc_id", "chunk_type", "source_type"]


async def _ensure_collection(client, dim: int) -> None:
    from qdrant_client import models as qm

    if not await client.collection_exists(SHARED_DOCSUM):
        await client.create_collection(
            collection_name=SHARED_DOCSUM,
            vectors_config={
                "dense": qm.VectorParams(size=dim, distance=qm.Distance.COSINE)
            },
            sparse_vectors_config={
                "sparse": qm.SparseVectorParams(modifier=qm.Modifier.IDF)
            },
        )
    for f in _INDEX_FIELDS:
        try:
            await client.create_payload_index(
                collection_name=SHARED_DOCSUM,
                field_name=f,
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        except Exception:  # noqa: BLE001 — exists; idempotent
            pass


async def embed_doc_profile(
    db,
    client,
    *,
    corpus_id: str,
    doc_id: str,
    dim: int,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Embed ONE doc's routing card into the shared Tier-0 collection.

    Deterministic point id ({corpus}:{doc}:doc_profile) → re-ingest/heal
    overwrites in place. Same payload shape as scripts_migrate_multitenant
    (the two writers MUST stay aligned — one Tier-0 schema).
    """
    from qdrant_client import models as qm

    from services.embedder import embed_batch
    from services.storage import qdrant_writer as qw

    doc = await db["documents"].find_one(
        {"corpus_id": corpus_id, "doc_id": doc_id},
        {"doc_id": 1, "title": 1, "doc_profile": 1, "source_type": 1},
    )
    profile = (doc or {}).get("doc_profile") or {}
    summary = str(profile.get("summary") or "").strip()
    if not summary:
        return {"embedded": 0, "reason": "no doc_profile.summary"}

    await _ensure_collection(client, dim)
    vectors = await embed_batch([summary], expected_dim=dim, api_key=api_key)
    point = qm.PointStruct(
        id=qw._uuid_from_str(f"{corpus_id}:{doc_id}:doc_profile"),
        vector={"dense": vectors[0]},
        payload={
            "corpus_id": corpus_id,
            "doc_id": doc_id,
            "chunk_type": "doc_summary",
            "title": (doc or {}).get("title") or "",
            "source_type": (doc or {}).get("source_type") or "",
            "summary": summary,
            "concepts": profile.get("concepts") or [],
            "section_ids": profile.get("section_ids") or [],
        },
    )
    await client.upsert(collection_name=SHARED_DOCSUM, points=[point])
    return {"embedded": 1, "collection": SHARED_DOCSUM}
