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

import asyncio
import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from services.storage.qdrant_writer import _col_for_corpus, payload_text_contract
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


async def _expected_qdrant_texts(
    db: AsyncIOMotorDatabase,
    *,
    doc_id: str,
    corpus_id: str,
) -> dict[str, str]:
    """Return canonical Mongo text keyed by Qdrant chunk_id/summary id."""
    expected: dict[str, str] = {}
    rows = await db["chunks"].find(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"_id": 0, "chunk_id": 1, "text": 1},
    ).to_list(length=None)
    for row in rows:
        chunk_id = str(row.get("chunk_id") or "")
        if chunk_id:
            expected[chunk_id] = str(row.get("text") or "")

    parents = await db["parent_chunks"].find(
        {"doc_id": doc_id, "corpus_id": corpus_id},
        {"_id": 0, "parent_id": 1, "summary": 1},
    ).to_list(length=None)
    if not parents:
        doc = await db["documents"].find_one(
            {"doc_id": doc_id, "corpus_id": corpus_id},
            {"_id": 0, "parent_chunks.parent_id": 1, "parent_chunks.summary": 1},
        )
        parents = (doc or {}).get("parent_chunks", []) or []
    for parent in parents:
        parent_id = str(parent.get("parent_id") or "")
        summary = str(parent.get("summary") or "")
        if parent_id and summary:
            expected[f"{parent_id}_summary"] = summary
    return expected


async def _scroll_doc_payloads(
    qdrant: AsyncQdrantClient,
    *,
    collection_name: str,
    doc_id: str,
    corpus_id: str,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    offset = None
    scroll_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="doc_id", match=qmodels.MatchValue(value=doc_id)
            ),
            qmodels.FieldCondition(
                key="corpus_id", match=qmodels.MatchValue(value=corpus_id)
            ),
        ]
    )
    while True:
        hits, offset = await qdrant.scroll(
            collection_name=collection_name,
            scroll_filter=scroll_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        payloads.extend((hit.payload or {}) for hit in hits)
        if offset is None:
            break
    return payloads


async def _verify_qdrant_text_contract(
    *,
    db: AsyncIOMotorDatabase,
    qdrant: AsyncQdrantClient,
    doc_id: str,
    corpus_id: str,
    collection_name: str,
) -> list[str]:
    """Check Qdrant payload text matches canonical Mongo text exactly."""
    errors: list[str] = []
    expected_by_id = await _expected_qdrant_texts(
        db, doc_id=doc_id, corpus_id=corpus_id
    )
    if not expected_by_id:
        return errors

    payloads = await _scroll_doc_payloads(
        qdrant,
        collection_name=collection_name,
        doc_id=doc_id,
        corpus_id=corpus_id,
    )
    missing = 0
    text_mismatch = 0
    contract_mismatch = 0
    examples: list[str] = []
    for payload in payloads:
        chunk_id = str(payload.get("chunk_id") or "")
        if not chunk_id:
            continue
        expected = expected_by_id.get(chunk_id)
        if expected is None:
            missing += 1
            continue
        actual = str(payload.get("chunk_text") or payload.get("text") or "")
        if actual != expected:
            text_mismatch += 1
            if len(examples) < 2:
                examples.append(
                    f"{chunk_id[:16]} qdrant_len={len(actual)} mongo_len={len(expected)}"
                )
        contract = payload_text_contract(expected)
        try:
            payload_len = int(payload.get("text_len"))
        except Exception:
            payload_len = -1
        if (
            payload_len != contract["text_len"]
            or payload.get("text_hash") != contract["text_hash"]
            or payload.get("is_truncated") is not False
        ):
            contract_mismatch += 1

    if missing:
        errors.append(f"{collection_name}: {missing} payload(s) missing Mongo text")
    if text_mismatch:
        suffix = f" examples={examples}" if examples else ""
        errors.append(
            f"{collection_name}: {text_mismatch} payload text mismatch(es){suffix}"
        )
    if contract_mismatch:
        errors.append(
            f"{collection_name}: {contract_mismatch} payload text contract mismatch(es)"
        )
    return errors


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

    # 3b. Payload text contract: Qdrant must carry full canonical text with
    # length/hash metadata. This catches accidental preview slicing before it
    # can affect vector-only/global retrieval lanes.
    for kind in target_qdrant_collections:
        col = _col_for_corpus(corpus_id, kind)
        # The contract scroll can time out under ingest load — that's a
        # checker infrastructure blip, not evidence the doc is bad. Retry
        # before failing, and never record an empty reason (many timeout
        # exceptions stringify to "", which produced undebuggable
        # "text_contract(col):" failures).
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                errors.extend(
                    await _verify_qdrant_text_contract(
                        db=db,
                        qdrant=qdrant,
                        doc_id=doc_id,
                        corpus_id=corpus_id,
                        collection_name=col,
                    )
                )
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                await asyncio.sleep(2.0 * (attempt + 1))
        if last_exc is not None:
            errors.append(
                f"text_contract({col}): check failed after retries — "
                f"{type(last_exc).__name__}: {last_exc}"
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
    # Pt 8c — worker.py filters NOISY_KINDS (toc/index/bibliography/front_matter
    # /back_matter/appendix) before calling write_document_graph, so Neo4j
    # legitimately has fewer Chunk nodes than Mongo. Compare against the
    # body-only expected count (same filter Qdrant uses above) or every doc
    # with a bibliography reads false-failed.
    if use_neo4j and neo4j_driver is None:
        errors.append("neo4j: required but driver is unavailable")
    elif use_neo4j and neo4j_driver is not None:
        try:
            expected_neo4j = await _expected_child_count(
                db,
                doc_id=doc_id,
                corpus_id=corpus_id,
                collection_kind="naive",
            )
            # Scope the Chunk side by corpus too: Document nodes MERGE on
            # doc_id, so re-ingesting the same file into a different corpus
            # leaves the shared Document linked to BOTH corpora's chunks.
            # Counting unscoped chunks false-fails any doc previously ingested
            # elsewhere (e.g. chunker settings changed -> stale extra chunk).
            cypher = (
                "MATCH (d:Document {doc_id: $doc_id, corpus_id: $corpus_id})"
                "-[:HAS_CHUNK]->(c:Chunk {corpus_id: $corpus_id}) "
                "RETURN count(c) AS cnt"
            )
            async with neo4j_driver.session() as session:
                result = await session.run(
                    cypher, doc_id=doc_id, corpus_id=corpus_id
                )
                row = await result.single()
                neo_cnt = int(row["cnt"]) if row else 0
            if neo_cnt != expected_neo4j:
                errors.append(
                    f"neo4j: HAS_CHUNK count={neo_cnt} but expected={expected_neo4j} "
                    f"(body chunks; mongo total={mongo_chunk_count})"
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
