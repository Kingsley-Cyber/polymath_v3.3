#!/usr/bin/env python3
"""Read-only census of protected target Chunk identities across sealed stores."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from collections import Counter
from pathlib import Path

from neo4j import GraphDatabase
from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qmodels

from config import get_settings
from services.ingestion.section_classifier import NOISY_KINDS


ROOT = Path(
    "/data/ingest-files/runpod-job-journals/e2e-isolation-backup-20260716T0046Z"
)
PROTECTED = "fd460347-61cc-4358-87fc-4b2a80533f0a"
MANIFEST_SHA256 = "e4fc35f387e75350b75762faa0266a429d135c82cdf6d3626834dc58ec9c737a"


def stable_hash(values: set[str]) -> str:
    payload = json.dumps(sorted(values), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def chunk_field_shape(document: dict) -> dict[str, dict]:
    output: dict[str, dict] = {}

    def walk(value: object, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, f"{path}.{key}" if path else str(key))
            return
        if "chunk" not in path.lower():
            return
        if isinstance(value, list):
            output[path] = {
                "type": "list",
                "count": len(value),
                "sha256": stable_hash({str(item) for item in value}),
            }
            return
        output[path] = {"type": type(value).__name__, "value": value}

    walk(document, "")
    return output


async def qdrant_chunk_ids(
    client: AsyncQdrantClient, doc_ids: list[str]
) -> set[str]:
    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="corpus_id", match=qmodels.MatchValue(value=PROTECTED)
            ),
            qmodels.FieldCondition(
                key="doc_id", match=qmodels.MatchAny(any=doc_ids)
            ),
        ]
    )
    output: set[str] = set()
    offset = None
    while True:
        points, offset = await client.scroll(
            collection_name=f"corpus_{PROTECTED[:8]}_graph",
            scroll_filter=query_filter,
            limit=256,
            offset=offset,
            with_payload=["chunk_id", "doc_id", "corpus_id"],
            with_vectors=False,
        )
        for point in points:
            payload = point.payload or {}
            chunk_id = str(payload.get("chunk_id") or "")
            if not chunk_id:
                raise RuntimeError("Qdrant graph point has empty chunk_id")
            output.add(chunk_id)
        if offset is None:
            return output


async def main() -> None:
    manifest_bytes = (ROOT / "manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA256:
        raise RuntimeError("immutable manifest hash drifted")
    manifest = json.loads(manifest_bytes)
    doc_ids = sorted(str(value) for value in manifest["shared_doc_ids"])
    doc_id_set = set(doc_ids)

    backup: set[str] = set()
    backup_any_target: set[str] = set()
    backup_e2e_target: set[str] = set()
    backup_node_props: dict[str, dict] = {}
    backup_protected_label_counts: Counter[str] = Counter()
    with gzip.open(ROOT / "neo4j_nodes.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            labels = set(str(value) for value in row.get("labels") or [])
            props = row.get("props") or {}
            if str(props.get("corpus_id") or "") == PROTECTED:
                backup_protected_label_counts.update(labels)
            if (
                "Chunk" in labels
                and str(props.get("doc_id") or "") in doc_id_set
            ):
                chunk_id = str(props.get("chunk_id") or "")
                corpus_id = str(props.get("corpus_id") or "")
                if not chunk_id or chunk_id in backup_any_target:
                    raise RuntimeError("backup target Chunk identity is empty or duplicate")
                backup_any_target.add(chunk_id)
                backup_node_props[chunk_id] = props
                if corpus_id == PROTECTED:
                    backup.add(chunk_id)
                elif corpus_id == manifest["e2e_corpus_id"]:
                    backup_e2e_target.add(chunk_id)
                else:
                    raise RuntimeError("backup target Chunk has unexpected owner")
                if corpus_id == PROTECTED and chunk_id in backup - {chunk_id}:
                    raise RuntimeError("backup Chunk identity is empty or duplicate")

    mongo_chunks: set[str] = set()
    mongo_noisy: set[str] = set()
    mongo_by_id: dict[str, dict] = {}
    mongo_kind_counts: Counter[str] = Counter()
    backup_neo4j_kind_counts: Counter[str] = Counter()
    backup_any_noisy_kind_counts: Counter[str] = Counter()
    backup_e2e_noisy_kind_counts: Counter[str] = Counter()
    with gzip.open(ROOT / "mongo.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            document = row.get("document") or {}
            if (
                row.get("collection") == "chunks"
                and row.get("scope") == "protected"
                and str(document.get("corpus_id") or "") == PROTECTED
                and str(document.get("doc_id") or "") in doc_id_set
            ):
                chunk_id = str(document.get("chunk_id") or "")
                if not chunk_id or chunk_id in mongo_chunks:
                    raise RuntimeError("backup Mongo Chunk identity is empty or duplicate")
                mongo_chunks.add(chunk_id)
                mongo_by_id[chunk_id] = document
                chunk_kind = str(document.get("chunk_kind") or "body")
                mongo_kind_counts[chunk_kind] += 1
                if chunk_kind in NOISY_KINDS:
                    mongo_noisy.add(chunk_id)

    mongo_kind_by_id: dict[str, str] = {}
    noisy_ghost_status: Counter[str] = Counter()
    noisy_ghost_nonempty_entities: set[str] = set()
    noisy_ghost_nonempty_relations: set[str] = set()
    noisy_ghost_nonempty_facts: set[str] = set()
    graph_promotion_job_chunk_fields: list[dict] = []
    with gzip.open(ROOT / "mongo.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            document = row.get("document") or {}
            chunk_id = str(document.get("chunk_id") or "")
            if (
                row.get("collection") == "chunks"
                and row.get("scope") == "protected"
                and chunk_id in mongo_chunks
            ):
                mongo_kind_by_id[chunk_id] = str(document.get("chunk_kind") or "body")
            if (
                row.get("collection") == "ghost_b_extractions"
                and row.get("scope") == "protected"
                and chunk_id in mongo_noisy
            ):
                noisy_ghost_status[str(document.get("status") or "<empty>")] += 1
                if document.get("entities"):
                    noisy_ghost_nonempty_entities.add(chunk_id)
                if document.get("relations"):
                    noisy_ghost_nonempty_relations.add(chunk_id)
                if document.get("facts"):
                    noisy_ghost_nonempty_facts.add(chunk_id)
            if (
                row.get("collection") == "graph_promotion_jobs"
                and row.get("scope") == "protected"
                and str(document.get("doc_id") or "") in doc_id_set
            ):
                graph_promotion_job_chunk_fields.append(
                    {
                        "doc_id": str(document.get("doc_id") or ""),
                        "status": str(document.get("status") or ""),
                        "chunk_fields": chunk_field_shape(document),
                    }
                )
    backup_neo4j_kind_counts.update(mongo_kind_by_id[value] for value in backup)
    backup_any_noisy_kind_counts.update(
        mongo_kind_by_id[value] for value in backup_any_target & mongo_noisy
    )
    backup_e2e_noisy_kind_counts.update(
        mongo_kind_by_id[value] for value in backup_e2e_target & mongo_noisy
    )
    common_noisy_ids = mongo_noisy & backup_any_target
    node_property_keys: Counter[str] = Counter()
    node_property_match_counts: Counter[str] = Counter()
    for chunk_id in common_noisy_ids:
        node = backup_node_props[chunk_id]
        mongo = mongo_by_id[chunk_id]
        node_property_keys.update(node.keys())
        for key, value in node.items():
            if key in mongo and mongo[key] == value:
                node_property_match_counts[key] += 1

    settings = get_settings()
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL, timeout=120)
    try:
        rows = driver.execute_query(
            "MATCH (c:Chunk {corpus_id: $corpus_id}) "
            "WHERE c.doc_id IN $doc_ids RETURN c.chunk_id AS chunk_id",
            corpus_id=PROTECTED,
            doc_ids=doc_ids,
        ).records
        current = {str(row["chunk_id"]) for row in rows}
        label_rows = driver.execute_query(
            "MATCH (n) WHERE n.corpus_id = $corpus_id "
            "UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label",
            corpus_id=PROTECTED,
        ).records
        current_protected_label_counts = {
            str(row["label"]): int(row["count"]) for row in label_rows
        }
        qdrant_ids = await qdrant_chunk_ids(qdrant, doc_ids)
    finally:
        driver.close()
        await qdrant.close()

    sets = {
        "backup_mongo": mongo_chunks,
        "backup_mongo_noisy": mongo_noisy,
        "backup_neo4j_any_target": backup_any_target,
        "backup_neo4j_e2e_target": backup_e2e_target,
        "backup_neo4j": backup,
        "current_neo4j": current,
        "qdrant": qdrant_ids,
    }
    result = {
        "schema_version": "e2e_chunk_identity_census.v1",
        "sets": {
            name: {"count": len(values), "sha256": stable_hash(values)}
            for name, values in sets.items()
        },
        "protected_label_counts": {
            "immutable_backup_after_collision": dict(
                sorted(backup_protected_label_counts.items())
            ),
            "current_after_failed_replay": current_protected_label_counts,
        },
        "kind_counts": {
            "backup_mongo": dict(sorted(mongo_kind_counts.items())),
            "backup_neo4j": dict(sorted(backup_neo4j_kind_counts.items())),
            "backup_neo4j_any_target_noisy": dict(
                sorted(backup_any_noisy_kind_counts.items())
            ),
            "backup_neo4j_e2e_target_noisy": dict(
                sorted(backup_e2e_noisy_kind_counts.items())
            ),
        },
        "backup_any_noisy_node_property_audit": {
            "population": len(common_noisy_ids),
            "key_presence": dict(sorted(node_property_keys.items())),
            "exact_mongo_match": dict(sorted(node_property_match_counts.items())),
        },
        "noisy_ghost_audit": {
            "status": dict(sorted(noisy_ghost_status.items())),
            "nonempty_entities": len(noisy_ghost_nonempty_entities),
            "nonempty_relations": len(noisy_ghost_nonempty_relations),
            "nonempty_facts": len(noisy_ghost_nonempty_facts),
            "nonempty_any": len(
                noisy_ghost_nonempty_entities
                | noisy_ghost_nonempty_relations
                | noisy_ghost_nonempty_facts
            ),
            "nonempty_any_in_backup_neo4j_any_target": len(
                (
                    noisy_ghost_nonempty_entities
                    | noisy_ghost_nonempty_relations
                    | noisy_ghost_nonempty_facts
                )
                & backup_any_target
            ),
        },
        "graph_promotion_job_chunk_fields": graph_promotion_job_chunk_fields,
        "comparisons": {
            "backup_intersect_current": len(backup & current),
            "backup_only_current": len(backup - current),
            "current_only_backup": len(current - backup),
            "backup_intersect_qdrant": len(backup & qdrant_ids),
            "backup_only_qdrant": len(backup - qdrant_ids),
            "qdrant_only_backup": len(qdrant_ids - backup),
            "current_intersect_qdrant": len(current & qdrant_ids),
            "current_only_qdrant": len(current - qdrant_ids),
            "qdrant_only_current": len(qdrant_ids - current),
            "backup_union_current": len(backup | current),
            "backup_union_qdrant": len(backup | qdrant_ids),
            "mongo_only_qdrant": len(mongo_chunks - qdrant_ids),
            "qdrant_only_mongo": len(qdrant_ids - mongo_chunks),
            "mongo_noisy_only_qdrant": len(mongo_noisy - qdrant_ids),
            "mongo_only_qdrant_in_backup_neo4j": len(
                (mongo_chunks - qdrant_ids) & backup
            ),
            "mongo_only_qdrant_absent_backup_neo4j": len(
                (mongo_chunks - qdrant_ids) - backup
            ),
            "mongo_noisy_in_backup_neo4j_any_target": len(
                mongo_noisy & backup_any_target
            ),
            "mongo_noisy_in_backup_neo4j_e2e_target": len(
                mongo_noisy & backup_e2e_target
            ),
            "desired_historical_union": len(qdrant_ids | (mongo_noisy & backup_any_target)),
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
