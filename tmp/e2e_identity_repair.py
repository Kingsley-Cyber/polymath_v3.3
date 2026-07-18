#!/usr/bin/env python3
"""Plan/apply the exact 15-document corpus-identity isolation repair."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from pymongo import ReplaceOne
from qdrant_client import AsyncQdrantClient, models as qmodels

from config import get_settings
from services.graph.neo4j_writer import corpus_content_key, write_document_graph
from services.ingestion.graph_backfill import _rehydrate_ghost_b_staging
from services.ingestion.section_classifier import (
    NOISY_KINDS,
    should_summarize_parent,
)
from services.ingestion.summary_tree import derive_node_concepts


E2E = "2c894530-8d57-4432-a6d4-bc14505a698b"
PROTECTED = "fd460347-61cc-4358-87fc-4b2a80533f0a"
BACKUP_ROOT = Path(
    "/data/ingest-files/runpod-job-journals/" "e2e-isolation-backup-20260716T0046Z"
)
MANIFEST_SHA256 = "e4fc35f387e75350b75762faa0266a429d135c82cdf6d3626834dc58ec9c737a"
BASELINE = Path(
    "/data/ingest-files/runpod-job-journals/e2e-protected-existing-baseline.json"
)
JOURNAL = Path("/data/ingest-files/runpod-job-journals/e2e-identity-repair-state.json")
EXPECTED_PROTECTED_TREE_ROWS = 310
RELATION_BATCH = 100
MARKER = f"e2e-isolation-{MANIFEST_SHA256[:16]}"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True, default=str).encode() + b"\n"
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory_fd = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def load_manifest() -> dict[str, Any]:
    path = BACKUP_ROOT / "manifest.json"
    payload = path.read_bytes()
    require(
        hashlib.sha256(payload).hexdigest() == MANIFEST_SHA256,
        "immutable backup manifest hash drifted",
    )
    manifest = json.loads(payload)
    require(manifest.get("e2e_corpus_id") == E2E, "backup E2E corpus drifted")
    require(
        manifest.get("protected_corpus_id") == PROTECTED,
        "backup protected corpus drifted",
    )
    doc_ids = list(manifest.get("shared_doc_ids") or [])
    require(
        len(doc_ids) == 15 and len(set(doc_ids)) == 15, "backup scope is not 15 IDs"
    )
    return manifest


async def qdrant_tree_payloads(
    client: AsyncQdrantClient,
    doc_ids: list[str],
) -> dict[str, dict[str, Any]]:
    collection = f"corpus_{PROTECTED[:8]}_schemas"
    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="corpus_id",
                match=qmodels.MatchValue(value=PROTECTED),
            ),
            qmodels.FieldCondition(
                key="kind",
                match=qmodels.MatchValue(value="summary_tree"),
            ),
            qmodels.FieldCondition(
                key="doc_id",
                match=qmodels.MatchAny(any=doc_ids),
            ),
        ]
    )
    output: dict[str, dict[str, Any]] = {}
    offset = None
    while True:
        rows, offset = await client.scroll(
            collection_name=collection,
            scroll_filter=query_filter,
            limit=128,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        for row in rows:
            payload = dict(row.payload or {})
            node_id = str(payload.get("node_id") or "")
            require(
                node_id and node_id not in output, "Qdrant tree node identity drifted"
            )
            output[node_id] = payload
        if offset is None:
            break
    return output


async def build_protected_tree_records(
    db: Any,
    qdrant: AsyncQdrantClient,
    doc_ids: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    payloads = await qdrant_tree_payloads(qdrant, doc_ids)
    records: list[dict[str, Any]] = []
    per_doc: dict[str, Any] = {}
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for doc_id in doc_ids:
        document = await db["documents"].find_one(
            {"corpus_id": PROTECTED, "doc_id": doc_id},
            {
                "_id": 0,
                "title": 1,
                "filename": 1,
                "source_type": 1,
                "doc_profile": 1,
            },
        )
        require(document is not None, f"protected document missing: {doc_id}")
        parent_rows = (
            await db["parent_chunks"]
            .find(
                {"corpus_id": PROTECTED, "doc_id": doc_id},
                {
                    "_id": 0,
                    "parent_id": 1,
                    "summary": 1,
                    "heading_path": 1,
                    "domain": 1,
                    "chunk_kind": 1,
                    "key_terms": 1,
                    "mechanisms": 1,
                    "concept_tags": 1,
                },
            )
            .sort("parent_id", 1)
            .to_list(length=None)
        )
        summary_rows = [
            row
            for row in parent_rows
            if should_summarize_parent(str(row.get("chunk_kind") or "body"))
        ]
        require(
            summary_rows
            and all(str(row.get("summary") or "").strip() for row in summary_rows),
            f"protected parent summaries incomplete: {doc_id}",
        )
        parent_by_id = {str(row["parent_id"]): row for row in summary_rows}
        doc_payloads = {
            node_id: payload
            for node_id, payload in payloads.items()
            if str(payload.get("doc_id") or "") == doc_id
        }
        rollup_payloads = {
            node_id: payload
            for node_id, payload in doc_payloads.items()
            if str(payload.get("node_type") or "") == "rollup"
        }
        section_payloads = {
            node_id: payload
            for node_id, payload in doc_payloads.items()
            if str(payload.get("node_type") or "") == "section"
        }
        require(
            len(rollup_payloads) + len(section_payloads) == len(doc_payloads),
            f"protected Qdrant tree type drifted: {doc_id}",
        )
        rollup_records: dict[str, dict[str, Any]] = {}
        for node_id, source in sorted(rollup_payloads.items()):
            parent_ids = [str(value) for value in source.get("parent_ids") or []]
            require(
                parent_ids and all(value in parent_by_id for value in parent_ids),
                f"protected rollup parent references drifted: {node_id}",
            )
            summary = str(source.get("summary") or "").strip()
            require(summary, f"protected Qdrant summary empty: {node_id}")
            rollup_records[node_id] = {
                "node_id": node_id,
                "node_type": "rollup",
                "doc_id": doc_id,
                "corpus_id": PROTECTED,
                "parent_ids": parent_ids,
                "child_node_ids": [],
                "section_range": str(source.get("section_range") or ""),
                "summary": summary,
                "concepts": derive_node_concepts(
                    [parent_by_id[parent_id] for parent_id in parent_ids]
                ),
                "domains": {},
                "schema_version": str(
                    source.get("schema_version") or "polymath.summary_tree.v1"
                ),
                "updated_at": now,
            }
        section_records: dict[str, dict[str, Any]] = {}
        for node_id, source in sorted(section_payloads.items()):
            child_ids = [str(value) for value in source.get("child_node_ids") or []]
            require(
                child_ids and all(value in rollup_records for value in child_ids),
                f"protected section child references drifted: {node_id}",
            )
            summary = str(source.get("summary") or "").strip()
            require(summary, f"protected Qdrant summary empty: {node_id}")
            section_records[node_id] = {
                "node_id": node_id,
                "node_type": "section",
                "doc_id": doc_id,
                "corpus_id": PROTECTED,
                "parent_ids": [],
                "child_node_ids": child_ids,
                "section_range": str(source.get("section_range") or ""),
                "summary": summary,
                "concepts": derive_node_concepts(
                    [
                        {"concept_tags": rollup_records[child_id]["concepts"]}
                        for child_id in child_ids
                    ]
                ),
                "domains": {},
                "schema_version": str(
                    source.get("schema_version") or "polymath.summary_tree.v1"
                ),
                "updated_at": now,
            }
        referenced_rollups = [
            child_id
            for section in section_records.values()
            for child_id in section["child_node_ids"]
        ]
        require(
            len(referenced_rollups) == len(set(referenced_rollups))
            and set(referenced_rollups) == set(rollup_records),
            f"protected Qdrant hierarchy is incomplete: {doc_id}",
        )
        profile = document.get("doc_profile") or {}
        document_node_id = str(profile.get("summary_id") or "")
        section_ids = [str(value) for value in profile.get("section_ids") or []]
        require(
            document_node_id
            and set(section_ids) == set(section_records)
            and len(section_ids) == len(section_records),
            f"protected document profile topology drifted: {doc_id}",
        )
        document_summary = str(profile.get("summary") or "").strip()
        require(document_summary, f"protected document profile empty: {doc_id}")
        document_record = {
            "node_id": document_node_id,
            "node_type": "document",
            "doc_id": doc_id,
            "corpus_id": PROTECTED,
            "parent_ids": [],
            "child_node_ids": section_ids,
            "section_range": str(
                document.get("title") or document.get("filename") or doc_id[:12]
            ),
            "summary": document_summary,
            "concepts": list(profile.get("concepts") or []),
            "domains": dict(profile.get("domains") or {}),
            "schema_version": str(
                profile.get("schema_version") or "polymath.summary_tree.v1"
            ),
            "updated_at": now,
        }
        records.extend(rollup_records.values())
        records.extend(section_records.values())
        records.append(document_record)
        per_doc[doc_id] = {
            "parents": len(parent_rows),
            "summary_parents": len(summary_rows),
            "tree_nodes": len(doc_payloads) + 1,
            "qdrant_tree_nodes": len(doc_payloads),
            "tree_source": "protected_qdrant_topology_plus_surviving_parent_concepts",
        }
    require(
        len(records) == EXPECTED_PROTECTED_TREE_ROWS,
        f"protected tree arithmetic drifted: {len(records)}",
    )
    require(
        len(payloads) == EXPECTED_PROTECTED_TREE_ROWS - len(doc_ids),
        f"protected Qdrant tree arithmetic drifted: {len(payloads)}",
    )
    return records, per_doc


async def graph_input_summary(db: Any, corpus_id: str, doc_id: str) -> dict[str, Any]:
    chunks = (
        await db["chunks"]
        .find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"_id": 0, "chunk_id": 1, "parent_id": 1, "chunk_kind": 1},
        )
        .to_list(length=None)
    )
    graph_chunks = [
        row for row in chunks if str(row.get("chunk_kind") or "body") not in NOISY_KINDS
    ]
    code_chunks = [row for row in graph_chunks if row.get("chunk_kind") == "code"]
    ghost_rows = (
        await db["ghost_b_extractions"]
        .find(
            {"corpus_id": corpus_id, "doc_id": doc_id},
            {"_id": 0, "chunk_id": 1, "status": 1, "facts": 1},
        )
        .to_list(length=None)
    )
    ok = [row for row in ghost_rows if row.get("status") == "ok"]
    errors = [row for row in ghost_rows if row.get("status") == "error"]
    graph_ids = {str(row.get("chunk_id") or "") for row in graph_chunks}
    ghost_ids = {str(row.get("chunk_id") or "") for row in ok}
    require(ghost_ids.issubset(graph_ids), f"stale Ghost-B rows: {corpus_id}/{doc_id}")
    require(not code_chunks, f"code synthesis required: {corpus_id}/{doc_id}")
    return {
        "all_chunks": len(chunks),
        "graph_chunks": len(graph_chunks),
        "ghost_ok": len(ok),
        "ghost_error": len(errors),
        "facts": sum(len(row.get("facts") or []) for row in ok),
        "unextracted_graph_chunks": len(graph_ids - ghost_ids),
        "chunk_ids": sorted(graph_ids),
    }


async def graph_plan(
    db: Any,
    driver: Any,
    doc_ids: list[str],
) -> tuple[dict[str, Any], dict[str, dict[str, list[str]]]]:
    per_corpus: dict[str, Any] = {}
    chunk_ids: dict[str, dict[str, list[str]]] = {PROTECTED: {}, E2E: {}}
    for corpus_id in (PROTECTED, E2E):
        rows: dict[str, Any] = {}
        totals = {
            "all_chunks": 0,
            "graph_chunks": 0,
            "ghost_ok": 0,
            "ghost_error": 0,
            "facts": 0,
            "unextracted_graph_chunks": 0,
        }
        for doc_id in doc_ids:
            summary = await graph_input_summary(db, corpus_id, doc_id)
            chunk_ids[corpus_id][doc_id] = list(summary.pop("chunk_ids"))
            rows[doc_id] = summary
            for key in totals:
                totals[key] += int(summary[key])
        per_corpus[corpus_id] = {"totals": totals, "documents": rows}

    target_chunk_ids = sorted(
        {
            chunk_id
            for corpus_rows in chunk_ids.values()
            for doc_rows in corpus_rows.values()
            for chunk_id in doc_rows
        }
    )
    third_owner_count = await db["chunks"].count_documents(
        {
            "chunk_id": {"$in": target_chunk_ids},
            "corpus_id": {"$nin": [PROTECTED, E2E]},
        }
    )
    require(third_owner_count == 0, "target chunk IDs have a third corpus owner")

    async with driver.session() as session:
        current_nodes = {}
        for corpus_id in (PROTECTED, E2E):
            result = await session.run(
                "MATCH (n) WHERE n.corpus_id = $corpus_id AND n.doc_id IN $doc_ids "
                "UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label",
                corpus_id=corpus_id,
                doc_ids=doc_ids,
            )
            current_nodes[corpus_id] = {
                str(row["label"]): int(row["count"]) async for row in result
            }
        relation_result = await session.run(
            "MATCH ()-[r:RELATES_TO]->() "
            "WHERE any(doc_id IN coalesce(r.evidence_doc_ids, []) WHERE doc_id IN $doc_ids) "
            "   OR r.latest_doc_id IN $doc_ids "
            "   OR any(key IN coalesce(r.evidence_doc_keys, []) WHERE key IN $doc_keys) "
            "RETURN count(r) AS relationships, "
            "sum(CASE WHEN size(coalesce(r.evidence_doc_keys, [])) > 0 THEN 1 ELSE 0 END) AS qualified",
            doc_ids=doc_ids,
            doc_keys=[
                corpus_content_key(corpus_id, doc_id)
                for corpus_id in (PROTECTED, E2E)
                for doc_id in doc_ids
            ],
        )
        relation_row = await relation_result.single()
    return (
        {
            "per_corpus": per_corpus,
            "target_chunk_id_count": len(target_chunk_ids),
            "third_owner_count": third_owner_count,
            "current_target_nodes": current_nodes,
            "target_relation_edges": int(
                relation_row["relationships"] if relation_row else 0
            ),
            "target_relation_edges_with_any_qualified_docs": int(
                relation_row["qualified"] if relation_row else 0
            ),
        },
        chunk_ids,
    )


async def batched_relation_cleanup(
    driver: Any,
    doc_ids: list[str],
    chunk_ids: dict[str, dict[str, list[str]]],
) -> dict[str, int]:
    updated_by_doc: dict[str, int] = {}
    async with driver.session() as session:
        for doc_id in doc_ids:
            raw_chunk_ids = sorted(
                set(chunk_ids[PROTECTED][doc_id]) | set(chunk_ids[E2E][doc_id])
            )
            doc_keys = [
                corpus_content_key(corpus_id, doc_id) for corpus_id in (PROTECTED, E2E)
            ]
            chunk_keys = [
                corpus_content_key(corpus_id, chunk_id)
                for corpus_id in (PROTECTED, E2E)
                for chunk_id in chunk_ids[corpus_id][doc_id]
            ]
            total = 0
            while True:
                result = await session.run(
                    """
                    MATCH ()-[r:RELATES_TO]->()
                    WHERE $doc_id IN coalesce(r.evidence_doc_ids, [])
                       OR r.latest_doc_id = $doc_id
                       OR any(key IN coalesce(r.evidence_doc_keys, []) WHERE key IN $doc_keys)
                       OR r.latest_doc_key IN $doc_keys
                    WITH r LIMIT $batch_size
                    WITH r,
                         coalesce(r.support_confidence_chunk_ids, []) AS support_ids,
                         coalesce(r.support_confidence_values, []) AS support_values,
                         coalesce(r.support_confidence_chunk_keys, []) AS support_keys,
                         coalesce(r.support_confidence_values_v2, []) AS support_values_v2
                    SET r.e2e_identity_repair_marker = $marker,
                        r.e2e_identity_repair_prepared = NULL,
                        r.e2e_identity_repair_finalized = NULL,
                        r.evidence_chunk_ids = [value IN coalesce(r.evidence_chunk_ids, []) WHERE NOT value IN $chunk_ids],
                        r.evidence_doc_ids = [value IN coalesce(r.evidence_doc_ids, []) WHERE value <> $doc_id],
                        r.evidence_chunk_keys = [value IN coalesce(r.evidence_chunk_keys, []) WHERE NOT value IN $chunk_keys],
                        r.evidence_doc_keys = [value IN coalesce(r.evidence_doc_keys, []) WHERE NOT value IN $doc_keys],
                        r.latest_doc_id = CASE WHEN r.latest_doc_id = $doc_id THEN NULL ELSE r.latest_doc_id END,
                        r.latest_doc_key = CASE WHEN r.latest_doc_key IN $doc_keys THEN NULL ELSE r.latest_doc_key END,
                        r.support_confidence_chunk_ids = [value IN support_ids WHERE NOT value IN $chunk_ids],
                        r.support_confidence_values = CASE
                            WHEN size(support_ids) = 0 THEN []
                            ELSE [idx IN range(0, size(support_ids) - 1)
                                  WHERE idx < size(support_values) AND NOT support_ids[idx] IN $chunk_ids
                                  | support_values[idx]]
                        END,
                        r.support_confidence_chunk_keys = [value IN support_keys WHERE NOT value IN $chunk_keys],
                        r.support_confidence_values_v2 = CASE
                            WHEN size(support_keys) = 0 THEN []
                            ELSE [idx IN range(0, size(support_keys) - 1)
                                  WHERE idx < size(support_values_v2) AND NOT support_keys[idx] IN $chunk_keys
                                  | support_values_v2[idx]]
                        END
                    SET r.support_count = CASE
                            WHEN size(coalesce(r.evidence_chunk_keys, [])) > size(coalesce(r.evidence_chunk_ids, []))
                            THEN size(coalesce(r.evidence_chunk_keys, []))
                            ELSE size(coalesce(r.evidence_chunk_ids, []))
                        END
                    RETURN count(r) AS updated
                    """,
                    doc_id=doc_id,
                    doc_keys=doc_keys,
                    chunk_ids=raw_chunk_ids,
                    chunk_keys=chunk_keys,
                    marker=MARKER,
                    batch_size=RELATION_BATCH,
                )
                row = await result.single()
                updated = int(row["updated"] if row else 0)
                total += updated
                if updated == 0:
                    break
            updated_by_doc[doc_id] = total

        while True:
            result = await session.run(
                """
                MATCH ()-[r:RELATES_TO]->()
                WHERE r.e2e_identity_repair_marker = $marker
                  AND coalesce(r.e2e_identity_repair_prepared, '') <> $marker
                WITH r LIMIT $batch_size
                CALL {
                    WITH r
                    OPTIONAL MATCH (d:Document {corpus_id: $protected})
                    WHERE d.doc_id IN coalesce(r.evidence_doc_ids, [])
                      AND NOT (d.doc_id IN $doc_ids)
                    RETURN count(d) > 0 AS protected_doc_support
                }
                CALL {
                    WITH r
                    OPTIONAL MATCH (c:Chunk {corpus_id: $protected})
                    WHERE c.chunk_id IN coalesce(r.evidence_chunk_ids, [])
                      AND NOT (c.doc_id IN $doc_ids)
                    RETURN count(c) > 0 AS protected_chunk_support
                }
                WITH r, protected_doc_support OR protected_chunk_support AS protected_legacy_support
                SET r.corpus_ids = [cid IN coalesce(r.corpus_ids, [])
                                    WHERE cid <> $protected AND cid <> $e2e]
                                   + CASE WHEN protected_legacy_support THEN [$protected] ELSE [] END,
                    r.e2e_identity_repair_prepared = $marker
                RETURN count(r) AS updated
                """,
                marker=MARKER,
                protected=PROTECTED,
                e2e=E2E,
                doc_ids=doc_ids,
                batch_size=RELATION_BATCH,
            )
            row = await result.single()
            if not row or int(row["updated"] or 0) == 0:
                break
    return updated_by_doc


async def write_graph_document(
    db: Any,
    driver: Any,
    corpus_id: str,
    doc_id: str,
    graph_chunk_ids: list[str],
) -> dict[str, Any]:
    document = await db["documents"].find_one(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )
    require(document is not None, f"graph document missing: {corpus_id}/{doc_id}")
    ghost_rows = (
        await db["ghost_b_extractions"]
        .find(
            {"corpus_id": corpus_id, "doc_id": doc_id, "status": "ok"},
            {"_id": 0},
        )
        .sort("chunk_id", 1)
        .to_list(length=None)
    )
    results = _rehydrate_ghost_b_staging(ghost_rows)
    parent_rows = (
        await db["chunks"]
        .find(
            {
                "corpus_id": corpus_id,
                "doc_id": doc_id,
                "chunk_id": {"$in": graph_chunk_ids},
            },
            {"_id": 0, "chunk_id": 1, "parent_id": 1},
        )
        .to_list(length=None)
    )
    parent_map = {
        str(row["chunk_id"]): str(row.get("parent_id") or "") for row in parent_rows
    }
    metrics = document.get("ghost_b_metrics") or {}
    schema_lens_id = (document.get("ingestion_config") or {}).get(
        "schema_lens_id"
    ) or metrics.get("schema_lens")
    parent_count = await db["parent_chunks"].count_documents(
        {"corpus_id": corpus_id, "doc_id": doc_id}
    )
    await write_document_graph(
        driver=driver,
        doc_id=doc_id,
        corpus_id=corpus_id,
        extraction_results=results,
        user_id=str(document.get("user_id") or "") or None,
        file_id=str(document.get("file_id") or "") or None,
        all_chunk_ids=graph_chunk_ids,
        filename=document.get("filename"),
        parent_count=int(parent_count),
        source_path=document.get("source_path"),
        source_tier=document.get("source_tier"),
        schema_lens_id=(str(schema_lens_id) if schema_lens_id else None),
        ghost_b_success_rate=(
            float(metrics["success_rate"])
            if metrics.get("success_rate") is not None
            else None
        ),
        ghost_b_extracted=(
            int(metrics["extracted_chunks"])
            if metrics.get("extracted_chunks") is not None
            else None
        ),
        ghost_b_total=(
            int(metrics["requested_chunks"])
            if metrics.get("requested_chunks") is not None
            else None
        ),
        db=db,
        chunk_parent_ids=parent_map,
    )
    return {
        "corpus_id": corpus_id,
        "doc_id": doc_id,
        "graph_chunks": len(graph_chunk_ids),
        "ghost_rows": len(results),
        "parent_count": int(parent_count),
    }


async def final_relation_reconcile(driver: Any, doc_ids: list[str]) -> int:
    doc_keys = [
        corpus_content_key(corpus_id, doc_id)
        for corpus_id in (PROTECTED, E2E)
        for doc_id in doc_ids
    ]
    total = 0
    async with driver.session() as session:
        while True:
            result = await session.run(
                """
                MATCH ()-[r:RELATES_TO]->()
                WHERE (r.e2e_identity_repair_marker = $marker
                       OR any(key IN coalesce(r.evidence_doc_keys, []) WHERE key IN $doc_keys))
                  AND coalesce(r.e2e_identity_repair_finalized, '') <> $marker
                WITH r LIMIT $batch_size
                CALL {
                    WITH r
                    OPTIONAL MATCH (d:Document {corpus_id: $protected})
                    WHERE d.doc_id IN coalesce(r.evidence_doc_ids, [])
                      AND NOT (d.doc_id IN $doc_ids)
                    RETURN count(d) > 0 AS protected_doc_support
                }
                CALL {
                    WITH r
                    OPTIONAL MATCH (c:Chunk {corpus_id: $protected})
                    WHERE c.chunk_id IN coalesce(r.evidence_chunk_ids, [])
                      AND NOT (c.doc_id IN $doc_ids)
                    RETURN count(c) > 0 AS protected_chunk_support
                }
                CALL {
                    WITH r
                    OPTIONAL MATCH (d:Document {corpus_id: $e2e})
                    WHERE d.doc_id IN coalesce(r.evidence_doc_ids, [])
                      AND NOT (d.doc_id IN $doc_ids)
                    RETURN count(d) > 0 AS e2e_doc_support
                }
                CALL {
                    WITH r
                    OPTIONAL MATCH (c:Chunk {corpus_id: $e2e})
                    WHERE c.chunk_id IN coalesce(r.evidence_chunk_ids, [])
                      AND NOT (c.doc_id IN $doc_ids)
                    RETURN count(c) > 0 AS e2e_chunk_support
                }
                WITH r,
                     protected_doc_support OR protected_chunk_support
                       OR any(key IN coalesce(r.evidence_doc_keys, []) WHERE key STARTS WITH $protected_prefix)
                       OR any(key IN coalesce(r.evidence_chunk_keys, []) WHERE key STARTS WITH $protected_prefix)
                       AS protected_support,
                     e2e_doc_support OR e2e_chunk_support
                       OR any(key IN coalesce(r.evidence_doc_keys, []) WHERE key STARTS WITH $e2e_prefix)
                       OR any(key IN coalesce(r.evidence_chunk_keys, []) WHERE key STARTS WITH $e2e_prefix)
                       AS e2e_support
                SET r.corpus_ids = [cid IN coalesce(r.corpus_ids, [])
                                    WHERE cid <> $protected AND cid <> $e2e]
                                   + CASE WHEN protected_support THEN [$protected] ELSE [] END
                                   + CASE WHEN e2e_support THEN [$e2e] ELSE [] END,
                    r.support_count = CASE
                        WHEN size(coalesce(r.evidence_chunk_keys, [])) > size(coalesce(r.evidence_chunk_ids, []))
                        THEN size(coalesce(r.evidence_chunk_keys, []))
                        ELSE size(coalesce(r.evidence_chunk_ids, []))
                    END,
                    r.avg_confidence = CASE
                        WHEN size(coalesce(r.support_confidence_values_v2, [])) > 0
                        THEN reduce(total = 0.0, value IN r.support_confidence_values_v2 | total + toFloat(value))
                             / size(r.support_confidence_values_v2)
                        WHEN size(coalesce(r.support_confidence_values, [])) > 0
                        THEN reduce(total = 0.0, value IN r.support_confidence_values | total + toFloat(value))
                             / size(r.support_confidence_values)
                        ELSE toFloat(coalesce(r.confidence, 0.0))
                    END,
                    r.e2e_identity_repair_finalized = $marker
                RETURN count(r) AS updated
                """,
                marker=MARKER,
                doc_keys=doc_keys,
                doc_ids=doc_ids,
                protected=PROTECTED,
                e2e=E2E,
                protected_prefix=f"{PROTECTED}|",
                e2e_prefix=f"{E2E}|",
                batch_size=RELATION_BATCH,
            )
            row = await result.single()
            updated = int(row["updated"] if row else 0)
            total += updated
            if updated == 0:
                break
        await session.run(
            "MATCH ()-[r:RELATES_TO]->() "
            "WHERE r.e2e_identity_repair_marker = $marker "
            "   OR r.e2e_identity_repair_finalized = $marker "
            "REMOVE r.e2e_identity_repair_marker, "
            "       r.e2e_identity_repair_prepared, "
            "       r.e2e_identity_repair_finalized",
            marker=MARKER,
        )
        await session.run(
            "MATCH ()-[r:RELATES_TO]->() "
            "WHERE size(coalesce(r.corpus_ids, [])) = 0 "
            "  AND size(coalesce(r.evidence_chunk_ids, [])) = 0 "
            "  AND size(coalesce(r.evidence_chunk_keys, [])) = 0 "
            "DELETE r"
        )
    return total


async def persist_tree(db: Any, records: list[dict[str, Any]]) -> int:
    operations = [
        ReplaceOne(
            {"corpus_id": PROTECTED, "node_id": row["node_id"]},
            row,
            upsert=True,
        )
        for row in records
    ]
    for start in range(0, len(operations), 100):
        await db["summary_tree"].bulk_write(
            operations[start : start + 100], ordered=True
        )
    return len(operations)


async def live_graph_counts(driver: Any, corpus_id: str) -> dict[str, int]:
    async with driver.session() as session:
        node = await (
            await session.run(
                "MATCH (n) WHERE n.corpus_id = $corpus_id RETURN count(n) AS count",
                corpus_id=corpus_id,
            )
        ).single()
        relationship = await (
            await session.run(
                "MATCH (a)-[r]->(b) "
                "WHERE r.corpus_id = $corpus_id "
                "OR a.corpus_id = $corpus_id OR b.corpus_id = $corpus_id "
                "RETURN count(r) AS count",
                corpus_id=corpus_id,
            )
        ).single()
    return {
        "nodes": int(node["count"] if node else 0),
        "relationships_touching_corpus": int(
            relationship["count"] if relationship else 0
        ),
    }


async def verify_repair(
    db: Any,
    driver: Any,
    doc_ids: list[str],
    expected_tree_hash: str,
    expected_e2e_tree_hash: str,
) -> dict[str, Any]:
    baseline = json.loads(BASELINE.read_text())
    baseline_graph = baseline["neo4j_counts_by_corpus"][PROTECTED]
    graph = await live_graph_counts(driver, PROTECTED)
    require(graph == baseline_graph, f"protected graph baseline did not close: {graph}")
    protected_tree = (
        await db["summary_tree"]
        .find(
            {"corpus_id": PROTECTED, "doc_id": {"$in": doc_ids}},
            {"_id": 0, "updated_at": 0},
        )
        .sort([("doc_id", 1), ("node_id", 1)])
        .to_list(length=None)
    )
    require(
        len(protected_tree) == EXPECTED_PROTECTED_TREE_ROWS,
        "protected tree row count did not close",
    )
    require(
        stable_hash(protected_tree) == expected_tree_hash,
        "protected tree content hash did not close",
    )
    e2e_tree = (
        await db["summary_tree"]
        .find(
            {"corpus_id": E2E, "doc_id": {"$in": doc_ids}},
            {"_id": 0, "updated_at": 0},
        )
        .sort([("doc_id", 1), ("node_id", 1)])
        .to_list(length=None)
    )
    require(len(e2e_tree) == 669, "E2E tree row count drifted during repair")
    require(
        stable_hash(e2e_tree) == expected_e2e_tree_hash,
        "E2E tree content changed during protected restore",
    )
    async with driver.session() as session:
        cross = await (
            await session.run(
                "MATCH (a)-[r]->(b) "
                "WHERE (a:Document OR a:Chunk OR a:Fact OR b:Document OR b:Chunk OR b:Fact) "
                "  AND ((a.corpus_id = $protected AND b.corpus_id = $e2e) "
                "    OR (a.corpus_id = $e2e AND b.corpus_id = $protected)) "
                "RETURN count(r) AS count",
                protected=PROTECTED,
                e2e=E2E,
            )
        ).single()
        temp = await (
            await session.run(
                "MATCH ()-[r:RELATES_TO]->() "
                "WHERE r.e2e_identity_repair_marker IS NOT NULL "
                "   OR r.e2e_identity_repair_prepared IS NOT NULL "
                "   OR r.e2e_identity_repair_finalized IS NOT NULL "
                "RETURN count(r) AS count"
            )
        ).single()
    require(
        int(cross["count"] if cross else -1) == 0, "cross-corpus derived edge remains"
    )
    require(
        int(temp["count"] if temp else -1) == 0, "temporary relation marker remains"
    )
    return {
        "protected_graph": graph,
        "protected_tree_rows": len(protected_tree),
        "protected_tree_content_sha256": stable_hash(protected_tree),
        "e2e_tree_rows": len(e2e_tree),
        "e2e_tree_content_sha256": stable_hash(e2e_tree),
        "cross_corpus_derived_edges": 0,
        "temporary_relation_markers": 0,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-manifest-sha")
    args = parser.parse_args()
    if args.apply:
        require(
            args.confirm_manifest_sha == MANIFEST_SHA256,
            "apply requires the exact immutable manifest SHA",
        )
    manifest = load_manifest()
    doc_ids = sorted(str(value) for value in manifest["shared_doc_ids"])
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL, timeout=120)
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        db = mongo[settings.MONGODB_DATABASE]
        tree_records, tree_plan = await build_protected_tree_records(
            db, qdrant, doc_ids
        )
        tree_hash = stable_hash(
            sorted(
                [
                    {key: value for key, value in row.items() if key != "updated_at"}
                    for row in tree_records
                ],
                key=lambda row: (str(row["doc_id"]), str(row["node_id"])),
            )
        )
        existing_tree = {
            corpus_id: await db["summary_tree"].count_documents(
                {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}}
            )
            for corpus_id in (PROTECTED, E2E)
        }
        e2e_tree_rows = (
            await db["summary_tree"]
            .find(
                {"corpus_id": E2E, "doc_id": {"$in": doc_ids}},
                {"_id": 0, "updated_at": 0},
            )
            .sort([("doc_id", 1), ("node_id", 1)])
            .to_list(length=None)
        )
        e2e_tree_hash = stable_hash(e2e_tree_rows)
        require(
            existing_tree[PROTECTED] in {0, EXPECTED_PROTECTED_TREE_ROWS},
            f"protected tree is partial: {existing_tree[PROTECTED]}",
        )
        graph_summary, chunk_ids = await graph_plan(db, driver, doc_ids)
        plan = {
            "schema_version": "e2e_identity_repair_plan.v1",
            "mode": "apply" if args.apply else "plan",
            "manifest_sha256": MANIFEST_SHA256,
            "marker": MARKER,
            "doc_count": len(doc_ids),
            "doc_ids": doc_ids,
            "existing_tree_rows": existing_tree,
            "protected_tree_rebuild_rows": len(tree_records),
            "protected_tree_content_sha256": tree_hash,
            "e2e_tree_content_sha256": e2e_tree_hash,
            "protected_tree_documents": tree_plan,
            "graph": graph_summary,
        }
        if not args.apply:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return

        journal: dict[str, Any] = {
            "schema_version": "e2e_identity_repair_state.v1",
            "manifest_sha256": MANIFEST_SHA256,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "plan_sha256": stable_hash(plan),
            "steps": [],
        }
        atomic_write(JOURNAL, journal)
        cleaned = await batched_relation_cleanup(driver, doc_ids, chunk_ids)
        journal["steps"].append(
            {"step": "relation_provenance_cleanup", "updated_by_doc": cleaned}
        )
        atomic_write(JOURNAL, journal)

        graph_writes: list[dict[str, Any]] = []
        for corpus_id in (PROTECTED, E2E):
            for doc_id in doc_ids:
                receipt = await write_graph_document(
                    db,
                    driver,
                    corpus_id,
                    doc_id,
                    chunk_ids[corpus_id][doc_id],
                )
                graph_writes.append(receipt)
                journal["steps"].append({"step": "graph_write", **receipt})
                atomic_write(JOURNAL, journal)

        reconciled = await final_relation_reconcile(driver, doc_ids)
        journal["steps"].append(
            {"step": "relation_provenance_reconcile", "updated": reconciled}
        )
        atomic_write(JOURNAL, journal)
        tree_written = await persist_tree(db, tree_records)
        journal["steps"].append(
            {"step": "protected_summary_tree_restore", "rows": tree_written}
        )
        atomic_write(JOURNAL, journal)
        verification = await verify_repair(
            db,
            driver,
            doc_ids,
            tree_hash,
            e2e_tree_hash,
        )
        journal["steps"].append({"step": "verification", **verification})
        journal["completed_at_utc"] = datetime.now(timezone.utc).isoformat()
        journal["status"] = "done"
        atomic_write(JOURNAL, journal)
        print(
            json.dumps(
                {
                    "schema_version": "e2e_identity_repair_receipt.v1",
                    "status": "done",
                    "journal": str(JOURNAL),
                    "manifest_sha256": MANIFEST_SHA256,
                    "relation_cleanup_updates": sum(cleaned.values()),
                    "graph_documents_written": len(graph_writes),
                    "relation_reconcile_updates": reconciled,
                    "summary_tree_rows_written": tree_written,
                    "verification": verification,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await driver.close()
        await qdrant.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
