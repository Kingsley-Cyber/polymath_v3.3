"""q8 (owner directive 2026-08-04) — backfill the one-point-per-child
candidate evidence collection from the legacy naive/hrag/graph family.

Shadow-only, additive, idempotent. Reads the three legacy route collections
for ONE corpus, unions their child membership, and writes each child ONCE
into `corpus_{cid8}_evidence` carrying:

  * the identical payload and dense+sparse vectors (source: naive first,
    then graph, then hrag — naive covers all children by writer contract);
  * record_kind=child, active=true;
  * one eligibility boolean per legacy collection that physically held the
    point: naive -> eligible_focused, hrag -> eligible_hierarchical,
    graph -> eligible_graph_seed.

Point IDs are REUSED verbatim (deterministic _child_point_id(chunk_id)), so
exact chunk_id/doc_id identity and parent hydration keys are preserved by
construction. No legacy collection is read from for production traffic and
nothing is deleted.

Usage (inside the backend container):
  python -m scripts.backfill_q8_evidence_collection --corpus <id> [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from config import get_settings
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http import models as rest

from services.storage import qdrant_writer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("backfill_q8_evidence")

_LEGACY_KINDS = ("naive", "hrag", "graph")
_SCROLL_BATCH = 256


async def _scroll_children(
    qdrant: AsyncQdrantClient, collection: str, *, chunk_type: str = "child"
) -> dict[str, Any]:
    """Scroll every point of one chunk_type (payload + vectors) out of a
    collection."""
    points: dict[str, Any] = {}
    if not await qdrant.collection_exists(collection):
        return points
    offset = None
    child_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="chunk_type", match=models.MatchValue(value=chunk_type)
            )
        ]
    )
    while True:
        records, offset = await qdrant.scroll(
            collection_name=collection,
            scroll_filter=child_filter,
            limit=_SCROLL_BATCH,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for rec in records:
            points[str(rec.id)] = {"payload": rec.payload or {}, "vector": rec.vector}
        if offset is None:
            break
    return points


def _normalize_vector(vector: Any) -> dict:
    """Shape a legacy point's vectors for the named dense+sparse layout."""
    if isinstance(vector, dict):
        out: dict = {}
        if vector.get("dense") is not None:
            out["dense"] = vector["dense"]
        sparse = vector.get("sparse")
        if sparse is not None and getattr(sparse, "indices", None):
            out["sparse"] = sparse
        return out
    # Legacy unnamed-dense layout: a raw list is the dense vector.
    return {"dense": list(vector)} if vector is not None else {}


async def _backfill_corpus(
    qdrant: AsyncQdrantClient, corpus_id: str, *, apply: bool
) -> dict:
    legacy: dict[str, dict[str, Any]] = {}
    for kind in _LEGACY_KINDS:
        name = qdrant_writer._col_for_corpus(corpus_id, kind)
        legacy[kind] = await _scroll_children(qdrant, name)
        log.info("scrolled %s: %d child points", name, len(legacy[kind]))

    # Union membership across the three route collections.
    union_ids = set()
    for kind in _LEGACY_KINDS:
        union_ids.update(legacy[kind].keys())

    # Payload/vector source priority: naive (holds all children by writer
    # contract) -> graph -> hrag. Duplicate chunk_ids keep the first source.
    points: list[rest.PointStruct] = []
    seen_chunk_ids: set[str] = set()
    flag_totals = {flag: 0 for flag in qdrant_writer._EVIDENCE_FLAG_FIELDS}
    source_counts = {kind: 0 for kind in _LEGACY_KINDS}
    for pid in sorted(union_ids):
        source_kind = next(
            (kind for kind in _LEGACY_KINDS if pid in legacy[kind]), None
        )
        record = legacy[source_kind][pid]
        payload = dict(record["payload"])
        chunk_id = str(payload.get("chunk_id") or "")
        if not chunk_id or chunk_id in seen_chunk_ids:
            continue  # one physical point per child — defensive dedupe
        seen_chunk_ids.add(chunk_id)
        flags = {
            qdrant_writer._KIND_TO_EVIDENCE_FLAG[kind]: (pid in legacy[kind])
            for kind in _LEGACY_KINDS
        }
        for flag, on in flags.items():
            if on:
                flag_totals[flag] += 1
        source_counts[source_kind] += 1
        payload.update({"record_kind": "child", "active": True, **flags})
        points.append(
            rest.PointStruct(
                id=pid,
                vector=_normalize_vector(record["vector"]),
                payload=payload,
            )
        )

    report = {
        "corpus_id": corpus_id,
        "legacy_child_counts": {
            kind: len(legacy[kind]) for kind in _LEGACY_KINDS
        },
        "union_child_count": len(union_ids),
        "candidate_point_count": len(points),
        "one_point_per_child": len(points) == len(seen_chunk_ids),
        "eligibility_flag_counts": flag_totals,
        "payload_source_counts": source_counts,
        "apply": apply,
    }

    # Hierarchical lane parity: funnel_a reads SUMMARY records from hrag, so
    # the candidate must also carry them (record_kind=parent_summary,
    # eligible_hierarchical only). Legacy point IDs are reused verbatim.
    hrag_name = qdrant_writer._col_for_corpus(corpus_id, "hrag")
    hrag_summaries = await _scroll_children(
        qdrant, hrag_name, chunk_type="summary"
    )
    summary_points: list[rest.PointStruct] = []
    for pid in sorted(hrag_summaries):
        record = hrag_summaries[pid]
        payload = dict(record["payload"])
        if payload.get("summary_model") == "":
            continue  # funnel_a explicitly excludes empty-model placeholders
        payload.update(
            {
                "record_kind": "parent_summary",
                "active": True,
                "eligible_focused": False,
                "eligible_hierarchical": True,
                "eligible_graph_seed": False,
            }
        )
        summary_points.append(
            rest.PointStruct(
                id=pid,
                vector=_normalize_vector(record["vector"]),
                payload=payload,
            )
        )
    report["hrag_summary_count"] = len(hrag_summaries)
    report["candidate_summary_count"] = len(summary_points)
    points.extend(summary_points)

    if not apply:
        log.info("DRY-RUN: %s", json.dumps(report, indent=2))
        return report

    evidence_name = await qdrant_writer.ensure_evidence_collection_for_corpus(
        qdrant, corpus_id
    )
    await qdrant_writer._upsert_points_batched(
        qdrant,
        collection_name=evidence_name,
        points=points,
        point_label="evidence-backfill",
    )
    info = await qdrant.get_collection(evidence_name)
    report["evidence_collection"] = evidence_name
    report["evidence_points_after"] = info.points_count
    log.info("APPLIED: %s", json.dumps(report, indent=2))
    return report


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="corpus UUID to backfill")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="write the evidence collection (default: dry-run report only)",
    )
    args = ap.parse_args()

    settings = get_settings()
    qdrant = AsyncQdrantClient(
        url=settings.QDRANT_URL,
        timeout=settings.QDRANT_TIMEOUT_SECONDS,
        prefer_grpc=settings.QDRANT_PREFER_GRPC,
        grpc_port=settings.QDRANT_GRPC_PORT,
    )
    try:
        report = await _backfill_corpus(qdrant, args.corpus, apply=args.apply)
        print(json.dumps(report, indent=2, default=str))
    finally:
        await qdrant.close()


if __name__ == "__main__":
    asyncio.run(main())
