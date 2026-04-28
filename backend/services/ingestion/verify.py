"""
Phase E — post-write verification.

Runs after the worker completes all legs (Mongo → Qdrant → optional Neo4j)
and confirms the writes are mutually consistent. Any mismatch is recorded in
`write_state.verify_errors[]` and `write_state.verified` is set to False.
All-pass sets `verified=True` with `verify_errors=[]`.

Non-fatal: verification failures do NOT raise. The doc still returns "done"
but the UI can surface a red "⚠ verify failed" badge so the user knows to
investigate instead of silently trusting bad data.
"""
from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from services.storage.qdrant_writer import _col_for_corpus
from services.ingestion.section_classifier import NOISY_KINDS

logger = logging.getLogger(__name__)
_HRAG_CHILD_TIERS = ("tier_a", "tier_b", "tier_b_plus")


async def _expected_child_count(
    db: AsyncIOMotorDatabase,
    *,
    doc_id: str,
    corpus_id: str,
    collection_kind: str,
) -> int:
    """Return the expected child-vector count for a Qdrant collection.

    HRAG intentionally stores only high-confidence child chunks (Tier A/B/B+)
    plus summaries. Naive and graph collections store every child chunk. The
    verifier must mirror that write contract or it reports false failures for
    Tier C documents.
    """
    query: dict[str, Any] = {"doc_id": doc_id, "corpus_id": corpus_id}
    query["$or"] = [
        {"chunk_kind": {"$exists": False}},
        {"chunk_kind": {"$nin": sorted(NOISY_KINDS)}},
    ]
    if collection_kind == "hrag":
        query["source_tier"] = {"$in": list(_HRAG_CHILD_TIERS)}
    return int(await db["chunks"].count_documents(query))


async def verify_ingest(
    *,
    db: AsyncIOMotorDatabase,
    qdrant: AsyncQdrantClient,
    neo4j_driver: Any | None,
    doc_id: str,
    corpus_id: str,
    target_qdrant_collections: list[str],
    use_neo4j: bool,
) -> tuple[bool, list[str]]:
    """Run consistency checks across Mongo / Qdrant / (optional) Neo4j.

    Returns (ok, errors). ok=False if any check failed.
    """
    errors: list[str] = []

    # 1. Mongo chunk count for this doc.
    mongo_chunk_count = await db["chunks"].count_documents(
        {"doc_id": doc_id, "corpus_id": corpus_id}
    )
    if mongo_chunk_count == 0:
        errors.append(
            f"mongo.chunks: 0 rows for doc={doc_id[:12]} corpus={corpus_id[:8]}"
        )

    # 2. Qdrant child-chunk count per target collection.
    #    Summary points live in the same collections (chunk_type=summary), so
    #    we filter on chunk_type=child to compare against Mongo.
    qdrant_counts: dict[str, int] = {}
    expected_counts: dict[str, int] = {}
    for kind in target_qdrant_collections:
        col = _col_for_corpus(corpus_id, kind)
        expected_counts[col] = await _expected_child_count(
            db,
            doc_id=doc_id,
            corpus_id=corpus_id,
            collection_kind=kind,
        )
        try:
            res = await qdrant.count(
                collection_name=col,
                count_filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="doc_id", match=qmodels.MatchValue(value=doc_id)
                        ),
                        qmodels.FieldCondition(
                            key="chunk_type",
                            match=qmodels.MatchValue(value="child"),
                        ),
                    ]
                ),
                exact=True,
            )
            qdrant_counts[col] = int(res.count)
        except Exception as exc:
            errors.append(f"qdrant.count({col}): {exc}")
            qdrant_counts[col] = -1

    # 3. Consistency: each target collection should match Mongo.
    for col, qcnt in qdrant_counts.items():
        if qcnt < 0:
            continue  # count failed — already reported
        expected = expected_counts.get(col, mongo_chunk_count)
        if qcnt != expected:
            errors.append(
                f"mismatch: expected={expected} child vectors but "
                f"{col} has {qcnt} child vectors"
            )

    # 4. Probe query on the first target collection — prove retrieval works,
    #    not just that inserts landed.
    if target_qdrant_collections and mongo_chunk_count > 0:
        probe_kind = target_qdrant_collections[0]
        probe_col = _col_for_corpus(corpus_id, probe_kind)
        try:
            sample_query: dict[str, Any] = {"doc_id": doc_id, "corpus_id": corpus_id}
            sample_query["$or"] = [
                {"chunk_kind": {"$exists": False}},
                {"chunk_kind": {"$nin": sorted(NOISY_KINDS)}},
            ]
            if probe_kind == "hrag":
                sample_query["source_tier"] = {"$in": list(_HRAG_CHILD_TIERS)}
            sample = await db["chunks"].find_one(
                sample_query,
                {"chunk_id": 1, "_id": 0},
            )
            sample_chunk_id = (sample or {}).get("chunk_id")
            if sample_chunk_id:
                hits, _ = await qdrant.scroll(
                    collection_name=probe_col,
                    scroll_filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="chunk_id",
                                match=qmodels.MatchValue(value=sample_chunk_id),
                            ),
                        ]
                    ),
                    limit=1,
                    with_payload=False,
                    with_vectors=False,
                )
                if not hits:
                    errors.append(
                        f"probe: chunk_id={sample_chunk_id[:16]} missing from {probe_col}"
                    )
        except Exception as exc:
            errors.append(f"probe.scroll({probe_col}): {exc}")

    # 5. Neo4j chunk count (only when use_neo4j and driver available).
    if use_neo4j and neo4j_driver is not None:
        try:
            cypher = (
                "MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})"
                "-[:HAS_CHUNK]->(c:Chunk) "
                "RETURN count(c) AS cnt"
            )
            async with neo4j_driver.session() as session:
                result = await session.run(
                    cypher, doc_id=doc_id, corpus_id=corpus_id
                )
                row = await result.single()
                neo_cnt = int(row["cnt"]) if row else 0
            if neo_cnt != mongo_chunk_count:
                errors.append(
                    f"neo4j: HAS_CHUNK count={neo_cnt} but mongo={mongo_chunk_count}"
                )
        except Exception as exc:
            errors.append(f"neo4j.count: {exc}")

    ok = not errors
    if ok:
        logger.info(
            "phase=verify ok=true doc=%s corpus=%s chunks=%d cols=%s",
            doc_id[:12],
            corpus_id[:8],
            mongo_chunk_count,
            list(qdrant_counts),
        )
    else:
        logger.warning(
            "phase=verify ok=false doc=%s corpus=%s errors=%s",
            doc_id[:12],
            corpus_id[:8],
            errors,
        )
    return ok, errors
