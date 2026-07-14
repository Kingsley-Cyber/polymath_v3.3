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
from datetime import datetime
from typing import Any

from pymongo import UpdateMany, UpdateOne

logger = logging.getLogger(__name__)

SHARED_DOCSUM = "polymath_doc_summaries"
_INDEX_FIELDS = ["corpus_id", "doc_id", "chunk_type", "source_type"]


async def _ensure_collection(client, dim: int) -> None:
    from qdrant_client import models as qm
    from services.storage import qdrant_writer as qw

    await qw._create_collection_with_retry(
        client,
        collection_name=SHARED_DOCSUM,
        vectors_config={
            "dense": qm.VectorParams(size=dim, distance=qm.Distance.COSINE)
        },
        sparse_vectors_config={
            "sparse": qm.SparseVectorParams(modifier=qm.Modifier.IDF)
        },
        quantization_config=qw.binary_quantization_config(),
    )
    await qw.ensure_binary_quantization(client, SHARED_DOCSUM)
    for f in _INDEX_FIELDS:
        try:
            await client.create_payload_index(
                collection_name=SHARED_DOCSUM,
                field_name=f,
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        except Exception:  # noqa: BLE001 — exists; idempotent
            pass


async def embed_doc_profiles(
    db,
    client,
    *,
    corpus_id: str,
    doc_ids: list[str],
    dim: int,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Embed document routing cards and stamp their durable projection state.

    Deterministic point IDs make this safe for normal ingest, summary repair,
    and historical backfill. Embeddings are batched so repairing deferred
    summaries does not turn into one sidecar request per document.
    """
    from qdrant_client import models as qm

    from services.embedder import embed_batch
    from services.storage import qdrant_writer as qw

    ordered_ids = list(dict.fromkeys(str(value) for value in doc_ids if str(value)))
    if not ordered_ids:
        return {"embedded": 0, "requested": 0, "missing_profiles": []}
    rows = await db["documents"].find(
        {"corpus_id": corpus_id, "doc_id": {"$in": ordered_ids}},
        {"_id": 0, "doc_id": 1, "title": 1, "doc_profile": 1, "source_type": 1},
    ).to_list(length=len(ordered_ids))
    by_id = {str(row.get("doc_id") or ""): row for row in rows}
    docs: list[dict[str, Any]] = []
    missing_profiles: list[str] = []
    for doc_id in ordered_ids:
        doc = by_id.get(doc_id) or {}
        profile = doc.get("doc_profile") or {}
        summary = str(profile.get("summary") or "").strip()
        if not summary:
            missing_profiles.append(doc_id)
            continue
        docs.append({"doc_id": doc_id, "doc": doc, "profile": profile, "summary": summary})
    if not docs:
        return {
            "embedded": 0,
            "requested": len(ordered_ids),
            "missing_profiles": missing_profiles,
            "reason": "no doc_profile.summary",
        }

    await _ensure_collection(client, dim)
    try:
        vectors = await embed_batch(
            [row["summary"] for row in docs],
            expected_dim=dim,
            api_key=api_key,
        )
        points = [
            qm.PointStruct(
                id=qw._uuid_from_str(
                    f"{corpus_id}:{row['doc_id']}:doc_profile"
                ),
                vector={"dense": vector},
                payload={
                    "corpus_id": corpus_id,
                    "doc_id": row["doc_id"],
                    "chunk_type": "doc_summary",
                    "title": row["doc"].get("title") or "",
                    "source_type": row["doc"].get("source_type") or "",
                    "summary": row["summary"],
                    "concepts": row["profile"].get("concepts") or [],
                    "section_ids": row["profile"].get("section_ids") or [],
                },
            )
            for row, vector in zip(docs, vectors, strict=True)
        ]
        await client.upsert(
            collection_name=SHARED_DOCSUM,
            points=points,
            wait=True,
        )
    except Exception as exc:
        await db["documents"].update_many(
            {"corpus_id": corpus_id, "doc_id": {"$in": [row["doc_id"] for row in docs]}},
            {
                "$set": {
                    "write_state.document_profile_indexed": False,
                    "write_state.document_profile_index_error": (
                        f"{type(exc).__name__}: {exc}"[:500]
                    ),
                    "write_state.document_profile_index_checked_at": datetime.utcnow(),
                }
            },
        )
        raise

    now = datetime.utcnow()
    operations = [
        UpdateOne(
            {"corpus_id": corpus_id, "doc_id": row["doc_id"]},
            {
                "$set": {
                    "write_state.document_profile_indexed": True,
                    "write_state.document_profile_indexed_at": now,
                    "write_state.document_profile_index_checked_at": now,
                },
                "$unset": {"write_state.document_profile_index_error": ""},
            },
        )
        for row in docs
    ]
    if operations:
        await db["documents"].bulk_write(operations, ordered=False)
    return {
        "embedded": len(points),
        "requested": len(ordered_ids),
        "missing_profiles": missing_profiles,
        "collection": SHARED_DOCSUM,
    }


async def embed_doc_profile(
    db,
    client,
    *,
    corpus_id: str,
    doc_id: str,
    dim: int,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Backward-compatible one-document Tier-0 projection wrapper."""

    return await embed_doc_profiles(
        db,
        client,
        corpus_id=corpus_id,
        doc_ids=[doc_id],
        dim=dim,
        api_key=api_key,
    )


async def delete_doc_profile(client, *, corpus_id: str, doc_id: str) -> None:
    """Delete one deterministic shared Tier-0 routing point."""

    from qdrant_client import models as qm

    from services.storage import qdrant_writer as qw

    if not await client.collection_exists(SHARED_DOCSUM):
        return
    await client.delete(
        collection_name=SHARED_DOCSUM,
        points_selector=qm.PointIdsList(
            points=[qw._uuid_from_str(f"{corpus_id}:{doc_id}:doc_profile")]
        ),
        wait=True,
    )


async def delete_corpus_doc_profiles(client, *, corpus_id: str) -> int:
    """Delete every shared Tier-0 routing card owned by one corpus."""

    from qdrant_client import models as qm

    if not await client.collection_exists(SHARED_DOCSUM):
        return 0
    result = await client.delete(
        collection_name=SHARED_DOCSUM,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[
                    qm.FieldCondition(
                        key="corpus_id",
                        match=qm.MatchValue(value=corpus_id),
                    )
                ]
            )
        ),
        wait=True,
    )
    return 1 if getattr(result, "operation_id", None) is not None else 0


async def reconcile_doc_profile_projection_state(
    db,
    client,
    *,
    corpus_id: str,
) -> dict[str, Any]:
    """Project shared Tier-0 point existence into durable Mongo readiness."""

    from services.storage.record_status import with_active_records

    profile_rows = await db["documents"].find(
        with_active_records(
            {
                "corpus_id": corpus_id,
                "doc_profile.summary": {"$exists": True, "$nin": [None, ""]},
            }
        ),
        {"_id": 0, "doc_id": 1},
    ).to_list(length=None)
    profile_ids = {
        str(row.get("doc_id") or "") for row in profile_rows if row.get("doc_id")
    }
    indexed_ids: set[str] = set()
    if await client.collection_exists(SHARED_DOCSUM):
        offset = None
        while True:
            points, offset = await client.scroll(
                collection_name=SHARED_DOCSUM,
                scroll_filter={
                    "must": [
                        {"key": "corpus_id", "match": {"value": corpus_id}},
                    ]
                },
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            indexed_ids.update(
                str((point.payload or {}).get("doc_id") or "")
                for point in points
                if (point.payload or {}).get("doc_id")
            )
            if offset is None:
                break
    ready_ids = sorted(profile_ids & indexed_ids)
    missing_ids = sorted(profile_ids - indexed_ids)
    now = datetime.utcnow()
    operations: list[UpdateOne] = []
    if ready_ids:
        operations.append(
            UpdateMany(
                {"corpus_id": corpus_id, "doc_id": {"$in": ready_ids}},
                {
                    "$set": {
                        "write_state.document_profile_indexed": True,
                        "write_state.document_profile_index_checked_at": now,
                    },
                    "$unset": {"write_state.document_profile_index_error": ""},
                },
            )
        )
    if missing_ids:
        operations.append(
            UpdateMany(
                {"corpus_id": corpus_id, "doc_id": {"$in": missing_ids}},
                {
                    "$set": {
                        "write_state.document_profile_indexed": False,
                        "write_state.document_profile_index_checked_at": now,
                        "write_state.document_profile_index_error": (
                            "missing_tier0_document_profile"
                        ),
                    }
                },
            )
        )
    if operations:
        await db["documents"].bulk_write(operations, ordered=False)
    return {
        "corpus_id": corpus_id,
        "profiles": len(profile_ids),
        "indexed": len(ready_ids),
        "missing": len(missing_ids),
        "orphaned": len(indexed_ids - profile_ids),
        "missing_doc_ids": missing_ids,
    }
