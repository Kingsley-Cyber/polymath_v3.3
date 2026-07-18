#!/usr/bin/env python3
"""Restore the protected 15-document graph from immutable original artifacts.

This operator never invokes the production graph writer.  It restores exact
backup rows/topology and the small legacy-only Chunk/Mention residue that can
be proven from the immutable post-collision backup.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from bson import json_util
from config import get_settings
from neo4j import GraphDatabase
from pymongo import MongoClient


ROOT = Path(
    "/data/ingest-files/runpod-job-journals/" "e2e-isolation-backup-20260716T0046Z"
)
BASELINE = Path(
    "/data/ingest-files/runpod-job-journals/e2e-protected-existing-baseline.json"
)
JOURNAL = Path(
    "/data/ingest-files/runpod-job-journals/" "e2e-original-graph-restore-state.json"
)
MANIFEST_SHA256 = "e4fc35f387e75350b75762faa0266a429d135c82cdf6d3626834dc58ec9c737a"
PROTECTED = "fd460347-61cc-4358-87fc-4b2a80533f0a"
E2E = "2c894530-8d57-4432-a6d4-bc14505a698b"
EXPECTED = {
    "documents": 15,
    "chunks": 14762,
    "facts": 2305,
    "has_chunk": 14762,
    "has_fact": 2305,
    "mentions": 4612,
    "supports_fact": 2305,
    "support_records": 12498,
    "legacy_only_chunks": 121,
    "overwritten_legacy_mentions": 71,
}
EXPECTED_FACT_HASH = "c5b2aa8d7730c99c1f1812f9ed7eef01e6543e0f60a4de5aa0509dd9703c85ca"
EXPECTED_FACT_EDGE_HASH = (
    "15c98f7bbf0c31bba926aba9da4ccd913391206fd8c572eca776581a2a621fd7"
)
REL_BATCH = 500
NODE_BATCH = 250
MONGO_BATCH = 1000


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def batched(values: list[Any], size: int) -> Iterable[list[Any]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(value, indent=2, sort_keys=True, default=str).encode() + b"\n"
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def canonical_bson(document: dict[str, Any]) -> dict[str, Any]:
    encoded = json_util.dumps(
        document,
        json_options=json_util.CANONICAL_JSON_OPTIONS,
    )
    return json.loads(encoded)


def bson_content_hash(documents: list[dict[str, Any]]) -> str:
    canonical = [canonical_bson(document) for document in documents]
    canonical.sort(key=lambda row: str(row.get("support_id") or ""))
    return stable_hash(canonical)


def identity_for_node(labels: list[str], props: dict[str, Any]) -> dict[str, Any]:
    label_set = set(labels)
    if "Entity" in label_set:
        return {"entity_id": str(props.get("entity_id") or "")}
    if "Fact" in label_set:
        return {
            "corpus_id": str(props.get("corpus_id") or ""),
            "fact_id": str(props.get("fact_id") or ""),
        }
    if "Chunk" in label_set:
        return {
            "corpus_id": str(props.get("corpus_id") or ""),
            "chunk_id": str(props.get("chunk_id") or ""),
        }
    if "Document" in label_set:
        return {
            "corpus_id": str(props.get("corpus_id") or ""),
            "doc_id": str(props.get("doc_id") or ""),
        }
    raise RuntimeError(f"unsupported relationship endpoint labels: {labels}")


def load_immutable_rows() -> dict[str, Any]:
    manifest_bytes = (ROOT / "manifest.json").read_bytes()
    require(
        hashlib.sha256(manifest_bytes).hexdigest() == MANIFEST_SHA256,
        "immutable manifest hash drifted",
    )
    manifest = json.loads(manifest_bytes)
    require(manifest["protected_corpus_id"] == PROTECTED, "protected ID drifted")
    require(manifest["e2e_corpus_id"] == E2E, "E2E ID drifted")
    doc_ids = sorted(str(value) for value in manifest["shared_doc_ids"])
    require(len(doc_ids) == EXPECTED["documents"], "document manifest drifted")
    doc_id_set = set(doc_ids)

    facts: dict[str, dict[str, Any]] = {}
    backup_chunks: dict[str, dict[str, Any]] = {}
    with gzip.open(ROOT / "neo4j_nodes.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            labels = [str(value) for value in row.get("labels") or []]
            props = row.get("props") or {}
            if (
                str(props.get("corpus_id") or "") != PROTECTED
                or str(props.get("doc_id") or "") not in doc_id_set
            ):
                continue
            if "Fact" in labels:
                fact_id = str(props.get("fact_id") or "")
                require(fact_id and fact_id not in facts, "duplicate backup Fact")
                facts[fact_id] = {"labels": sorted(labels), "props": props}
            if "Chunk" in labels:
                chunk_id = str(props.get("chunk_id") or "")
                require(
                    chunk_id and chunk_id not in backup_chunks, "duplicate backup Chunk"
                )
                backup_chunks[chunk_id] = {"labels": sorted(labels), "props": props}

    require(len(facts) == EXPECTED["facts"], "backup Fact count drifted")
    fact_rows = sorted(facts.values(), key=lambda row: str(row["props"]["fact_id"]))
    require(stable_hash(fact_rows) == EXPECTED_FACT_HASH, "backup Fact hash drifted")

    fact_edges: list[dict[str, Any]] = []
    protected_mentions: dict[tuple[str, str], dict[str, Any]] = {}
    overwritten_mentions: dict[tuple[str, str], dict[str, Any]] = {}
    fact_edge_counts: dict[str, Counter[str]] = {
        fact_id: Counter() for fact_id in facts
    }
    with gzip.open(
        ROOT / "neo4j_relationships.jsonl.gz", "rt", encoding="utf-8"
    ) as handle:
        for line in handle:
            row = json.loads(line)
            edge_type = str(row.get("type") or "")
            props = row.get("props") or {}
            start_props = row.get("start_props") or {}
            end_props = row.get("end_props") or {}
            if edge_type in {"HAS_FACT", "SUPPORTS_FACT"}:
                fact_id = str(end_props.get("fact_id") or "")
                if fact_id not in facts or end_props.get("corpus_id") != PROTECTED:
                    continue
                start_identity = identity_for_node(
                    [str(value) for value in row.get("start_labels") or []],
                    start_props,
                )
                if edge_type == "SUPPORTS_FACT":
                    start_identity["corpus_id"] = PROTECTED
                edge = {
                    "type": edge_type,
                    "start_identity": start_identity,
                    "end_identity": {"corpus_id": PROTECTED, "fact_id": fact_id},
                    "props": props,
                }
                fact_edges.append(edge)
                fact_edge_counts[fact_id][edge_type] += 1
                continue
            if edge_type != "MENTIONS":
                continue
            chunk_id = str(start_props.get("chunk_id") or "")
            entity_id = str(end_props.get("entity_id") or "")
            if (
                not chunk_id
                or not entity_id
                or str(props.get("doc_id") or "") not in doc_id_set
            ):
                continue
            key = (chunk_id, entity_id)
            if str(props.get("corpus_id") or "") == PROTECTED:
                require(
                    key not in protected_mentions, "duplicate protected MENTIONS edge"
                )
                protected_mentions[key] = {
                    "chunk_id": chunk_id,
                    "entity_id": entity_id,
                    "props": props,
                    "provenance": "immutable_protected_edge",
                }
                continue
            if str(props.get("corpus_id") or "") != E2E:
                continue
            extracted_types = [
                str(value) for value in props.get("extracted_types") or []
            ]
            legacy_types = [
                value for value in extracted_types if value != value.upper()
            ]
            if not legacy_types:
                continue
            require(len(set(legacy_types)) == 1, "overwritten legacy type is ambiguous")
            restored_props = dict(props)
            restored_props["corpus_id"] = PROTECTED
            restored_props["extracted_type"] = legacy_types[0]
            restored_props["extracted_types"] = sorted(set(legacy_types))
            require(
                key not in overwritten_mentions, "duplicate overwritten MENTIONS edge"
            )
            overwritten_mentions[key] = {
                "chunk_id": chunk_id,
                "entity_id": entity_id,
                "props": restored_props,
                "provenance": "merged_edge_preserved_legacy_type",
            }

    require(
        all(
            counts == {"HAS_FACT": 1, "SUPPORTS_FACT": 1}
            for counts in fact_edge_counts.values()
        ),
        "backup Fact edge closure drifted",
    )
    canonical_fact_edges = sorted(
        fact_edges,
        key=lambda row: (
            row["type"],
            json.dumps(row["start_identity"], sort_keys=True),
            str(row["end_identity"]["fact_id"]),
        ),
    )
    require(
        stable_hash(canonical_fact_edges) == EXPECTED_FACT_EDGE_HASH,
        "backup Fact edge hash drifted",
    )
    require(len(protected_mentions) == 4541, "protected MENTIONS residue drifted")
    require(
        len(overwritten_mentions) == EXPECTED["overwritten_legacy_mentions"],
        "overwritten MENTIONS count drifted",
    )
    require(
        not (set(protected_mentions) & set(overwritten_mentions)),
        "historical MENTIONS sets overlap",
    )
    mentions = list(protected_mentions.values()) + list(overwritten_mentions.values())
    require(len(mentions) == EXPECTED["mentions"], "original MENTIONS topology drifted")

    support_records: list[dict[str, Any]] = []
    with gzip.open(ROOT / "mongo.jsonl.gz", "rt", encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            document = row.get("document") or {}
            if (
                row.get("collection") == "relation_support_records"
                and row.get("scope") == "protected"
                and document.get("corpus_id") == PROTECTED
                and document.get("doc_id") in doc_id_set
            ):
                support_records.append(
                    json_util.loads(json.dumps(document, separators=(",", ":")))
                )
    require(
        len(support_records) == EXPECTED["support_records"],
        "backup relation-support count drifted",
    )
    require(
        len({str(row.get("support_id") or "") for row in support_records})
        == EXPECTED["support_records"],
        "backup support IDs are not unique",
    )

    return {
        "manifest": manifest,
        "doc_ids": doc_ids,
        "facts": facts,
        "fact_edges": canonical_fact_edges,
        "backup_chunks": backup_chunks,
        "mentions": mentions,
        "support_records": support_records,
        "support_records_sha256": bson_content_hash(support_records),
    }


def graph_counts(driver: Any, corpus_id: str) -> dict[str, int]:
    rows = driver.execute_query(
        "MATCH (n) WHERE n.corpus_id = $corpus_id RETURN count(n) AS nodes",
        corpus_id=corpus_id,
    ).records
    relationships = driver.execute_query(
        "MATCH (a)-[r]->(b) "
        "WHERE r.corpus_id = $corpus_id OR a.corpus_id = $corpus_id "
        "OR b.corpus_id = $corpus_id "
        "RETURN count(r) AS relationships",
        corpus_id=corpus_id,
    ).records
    return {
        "nodes": int(rows[0]["nodes"]),
        "relationships_touching_corpus": int(relationships[0]["relationships"]),
    }


def target_snapshot(driver: Any, doc_ids: list[str]) -> dict[str, Any]:
    node_rows = driver.execute_query(
        "MATCH (n) WHERE n.corpus_id = $corpus_id AND n.doc_id IN $doc_ids "
        "UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label",
        corpus_id=PROTECTED,
        doc_ids=doc_ids,
    ).records
    edge_rows = driver.execute_query(
        "MATCH (a)-[r]->(b) "
        "WHERE (a.corpus_id = $corpus_id AND a.doc_id IN $doc_ids) "
        "OR (b.corpus_id = $corpus_id AND b.doc_id IN $doc_ids) "
        "RETURN type(r) AS type, count(*) AS count ORDER BY type",
        corpus_id=PROTECTED,
        doc_ids=doc_ids,
    ).records
    chunk_rows = driver.execute_query(
        "MATCH (c:Chunk {corpus_id: $corpus_id}) WHERE c.doc_id IN $doc_ids "
        "RETURN c.chunk_id AS chunk_id, c.doc_id AS doc_id",
        corpus_id=PROTECTED,
        doc_ids=doc_ids,
    ).records
    return {
        "nodes": {str(row["label"]): int(row["count"]) for row in node_rows},
        "relationships": {str(row["type"]): int(row["count"]) for row in edge_rows},
        "chunks": {str(row["chunk_id"]): str(row["doc_id"]) for row in chunk_rows},
    }


def count_entities(driver: Any, entity_ids: set[str]) -> int:
    found = 0
    values = sorted(entity_ids)
    for batch in batched(values, 1000):
        rows = driver.execute_query(
            "UNWIND $entity_ids AS entity_id "
            "MATCH (e:Entity {entity_id: entity_id}) RETURN count(e) AS count",
            entity_ids=batch,
        ).records
        found += int(rows[0]["count"])
    return found


def build_plan(database: Any, driver: Any, immutable: dict[str, Any]) -> dict[str, Any]:
    doc_ids = immutable["doc_ids"]
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    baseline_graph = baseline["neo4j_counts_by_corpus"][PROTECTED]
    snapshot = target_snapshot(driver, doc_ids)
    current_graph = graph_counts(driver, PROTECTED)
    current_support_target = database["relation_support_records"].count_documents(
        {"corpus_id": PROTECTED, "doc_id": {"$in": doc_ids}}
    )
    current_support_total = database["relation_support_records"].count_documents(
        {"corpus_id": PROTECTED}
    )

    require(
        snapshot["nodes"] == {"Chunk": 14641, "Document": 15, "Fact": 10665},
        "current RED node state drifted",
    )
    require(
        snapshot["relationships"]
        == {
            "HAS_CHUNK": 14641,
            "HAS_FACT": 10665,
            "MENTIONS": 26124,
            "SUPPORTS_FACT": 10665,
        },
        "current RED relationship state drifted",
    )
    require(
        current_graph == {"nodes": 81381, "relationships_touching_corpus": 165024},
        "current protected graph RED census drifted",
    )
    require(current_support_target == 12616, "current support target count drifted")
    require(current_support_total == 45778, "current support total count drifted")

    current_chunks = set(snapshot["chunks"])
    backup_chunks = set(immutable["backup_chunks"])
    legacy_only_chunks = sorted(backup_chunks - current_chunks)
    require(
        len(legacy_only_chunks) == EXPECTED["legacy_only_chunks"],
        "legacy-only Chunk count drifted",
    )
    desired_chunk_ids = current_chunks | set(legacy_only_chunks)
    require(len(desired_chunk_ids) == EXPECTED["chunks"], "desired Chunk count drifted")
    desired_chunk_docs = dict(snapshot["chunks"])
    for chunk_id in legacy_only_chunks:
        desired_chunk_docs[chunk_id] = str(
            immutable["backup_chunks"][chunk_id]["props"].get("doc_id") or ""
        )
    require(
        set(desired_chunk_docs.values()).issubset(set(doc_ids))
        and "" not in desired_chunk_docs.values(),
        "desired Chunk document ownership drifted",
    )

    support_chunks = {
        str(edge["start_identity"].get("chunk_id") or "")
        for edge in immutable["fact_edges"]
        if edge["type"] == "SUPPORTS_FACT"
    }
    mention_chunks = {str(row["chunk_id"]) for row in immutable["mentions"]}
    require(support_chunks.issubset(desired_chunk_ids), "Fact support Chunk is absent")
    require(mention_chunks.issubset(desired_chunk_ids), "MENTIONS Chunk is absent")
    entity_ids = {
        str(edge["start_identity"].get("entity_id") or "")
        for edge in immutable["fact_edges"]
        if edge["type"] == "HAS_FACT"
    } | {str(row["entity_id"]) for row in immutable["mentions"]}
    require("" not in entity_ids, "required Entity identity is empty")
    require(
        count_entities(driver, entity_ids) == len(entity_ids),
        "required global Entity is absent",
    )

    desired_nodes = EXPECTED["documents"] + EXPECTED["chunks"] + EXPECTED["facts"]
    current_target_nodes = sum(snapshot["nodes"].values())
    desired_relationships = (
        EXPECTED["has_chunk"]
        + EXPECTED["has_fact"]
        + EXPECTED["mentions"]
        + EXPECTED["supports_fact"]
    )
    current_target_relationships = sum(snapshot["relationships"].values())
    expected_graph = {
        "nodes": current_graph["nodes"] - current_target_nodes + desired_nodes,
        "relationships_touching_corpus": current_graph["relationships_touching_corpus"]
        - current_target_relationships
        + desired_relationships,
    }
    require(
        expected_graph == baseline_graph, "recovery graph arithmetic does not close"
    )
    expected_support_total = (
        current_support_total - current_support_target + EXPECTED["support_records"]
    )
    baseline_support_total = int(
        baseline["mongo_counts_by_collection_and_corpus"]["relation_support_records"][
            PROTECTED
        ]
    )
    require(
        expected_support_total == baseline_support_total,
        "relation-support arithmetic does not close",
    )

    plan = {
        "schema_version": "e2e_original_graph_restore_plan.v1",
        "manifest_sha256": MANIFEST_SHA256,
        "protected_corpus_id": PROTECTED,
        "document_count": len(doc_ids),
        "current_target": {
            "nodes": snapshot["nodes"],
            "relationships": snapshot["relationships"],
            "support_records": current_support_target,
        },
        "restore_target": {
            "nodes": {
                "Document": EXPECTED["documents"],
                "Chunk": EXPECTED["chunks"],
                "Fact": EXPECTED["facts"],
            },
            "relationships": {
                "HAS_CHUNK": EXPECTED["has_chunk"],
                "HAS_FACT": EXPECTED["has_fact"],
                "MENTIONS": EXPECTED["mentions"],
                "SUPPORTS_FACT": EXPECTED["supports_fact"],
            },
            "support_records": EXPECTED["support_records"],
        },
        "legacy_only_chunk_count": len(legacy_only_chunks),
        "overwritten_legacy_mention_count": sum(
            1
            for row in immutable["mentions"]
            if row["provenance"] == "merged_edge_preserved_legacy_type"
        ),
        "required_entity_count": len(entity_ids),
        "fact_content_sha256": EXPECTED_FACT_HASH,
        "fact_edge_topology_sha256": EXPECTED_FACT_EDGE_HASH,
        "support_records_sha256": immutable["support_records_sha256"],
        "expected_protected_graph": expected_graph,
        "expected_protected_support_records": expected_support_total,
    }
    return {
        "plan": plan,
        "legacy_only_chunks": legacy_only_chunks,
        "desired_chunk_docs": desired_chunk_docs,
    }


def delete_target_relationships(driver: Any, doc_ids: list[str]) -> int:
    total = 0
    while True:
        rows = driver.execute_query(
            "MATCH (a)-[r]->(b) "
            "WHERE type(r) IN ['HAS_CHUNK','HAS_FACT','MENTIONS','SUPPORTS_FACT'] "
            "AND ((a.corpus_id = $corpus_id AND a.doc_id IN $doc_ids) "
            "OR (b.corpus_id = $corpus_id AND b.doc_id IN $doc_ids)) "
            "WITH r LIMIT $batch_size DELETE r RETURN count(r) AS count",
            corpus_id=PROTECTED,
            doc_ids=doc_ids,
            batch_size=REL_BATCH,
        ).records
        count = int(rows[0]["count"] if rows else 0)
        total += count
        if count == 0:
            return total


def delete_target_facts(driver: Any, doc_ids: list[str]) -> int:
    total = 0
    while True:
        rows = driver.execute_query(
            "MATCH (f:Fact {corpus_id: $corpus_id}) WHERE f.doc_id IN $doc_ids "
            "WITH f LIMIT $batch_size DETACH DELETE f RETURN count(f) AS count",
            corpus_id=PROTECTED,
            doc_ids=doc_ids,
            batch_size=NODE_BATCH,
        ).records
        count = int(rows[0]["count"] if rows else 0)
        total += count
        if count == 0:
            return total


def restore_nodes(
    driver: Any,
    immutable: dict[str, Any],
    legacy_only_chunks: list[str],
) -> dict[str, int]:
    chunk_rows = [
        immutable["backup_chunks"][chunk_id]["props"] for chunk_id in legacy_only_chunks
    ]
    for batch in batched(chunk_rows, NODE_BATCH):
        driver.execute_query(
            "UNWIND $rows AS row "
            "MERGE (c:Chunk {corpus_id: row.corpus_id, chunk_id: row.chunk_id}) "
            "SET c = row",
            rows=batch,
        )
    fact_rows = [row["props"] for row in immutable["facts"].values()]
    for batch in batched(fact_rows, NODE_BATCH):
        driver.execute_query(
            "UNWIND $rows AS row "
            "MERGE (f:Fact {corpus_id: row.corpus_id, fact_id: row.fact_id}) "
            "SET f = row",
            rows=batch,
        )
    return {"chunks": len(chunk_rows), "facts": len(fact_rows)}


def restore_relationships(
    driver: Any,
    immutable: dict[str, Any],
    desired_chunk_docs: dict[str, str],
) -> dict[str, int]:
    has_chunk_rows = [
        {
            "corpus_id": PROTECTED,
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "props": {"corpus_id": PROTECTED},
        }
        for chunk_id, doc_id in sorted(desired_chunk_docs.items())
    ]
    for batch in batched(has_chunk_rows, REL_BATCH):
        driver.execute_query(
            "UNWIND $rows AS row "
            "MATCH (d:Document {corpus_id: row.corpus_id, doc_id: row.doc_id}) "
            "MATCH (c:Chunk {corpus_id: row.corpus_id, chunk_id: row.chunk_id}) "
            "MERGE (d)-[r:HAS_CHUNK]->(c) SET r = row.props",
            rows=batch,
        )

    has_fact_rows = [
        edge for edge in immutable["fact_edges"] if edge["type"] == "HAS_FACT"
    ]
    for batch in batched(has_fact_rows, REL_BATCH):
        driver.execute_query(
            "UNWIND $rows AS row "
            "MATCH (e:Entity {entity_id: row.start_identity.entity_id}) "
            "MATCH (f:Fact {corpus_id: row.end_identity.corpus_id, "
            "fact_id: row.end_identity.fact_id}) "
            "MERGE (e)-[r:HAS_FACT]->(f) SET r = row.props",
            rows=batch,
        )

    supports_rows = [
        edge for edge in immutable["fact_edges"] if edge["type"] == "SUPPORTS_FACT"
    ]
    for batch in batched(supports_rows, REL_BATCH):
        driver.execute_query(
            "UNWIND $rows AS row "
            "MATCH (c:Chunk {corpus_id: row.start_identity.corpus_id, "
            "chunk_id: row.start_identity.chunk_id}) "
            "MATCH (f:Fact {corpus_id: row.end_identity.corpus_id, "
            "fact_id: row.end_identity.fact_id}) "
            "MERGE (c)-[r:SUPPORTS_FACT]->(f) SET r = row.props",
            rows=batch,
        )

    mention_rows = [
        {
            "corpus_id": PROTECTED,
            "chunk_id": row["chunk_id"],
            "entity_id": row["entity_id"],
            "props": row["props"],
        }
        for row in immutable["mentions"]
    ]
    for batch in batched(mention_rows, REL_BATCH):
        driver.execute_query(
            "UNWIND $rows AS row "
            "MATCH (c:Chunk {corpus_id: row.corpus_id, chunk_id: row.chunk_id}) "
            "MATCH (e:Entity {entity_id: row.entity_id}) "
            "MERGE (c)-[r:MENTIONS]->(e) SET r = row.props",
            rows=batch,
        )
    return {
        "HAS_CHUNK": len(has_chunk_rows),
        "HAS_FACT": len(has_fact_rows),
        "MENTIONS": len(mention_rows),
        "SUPPORTS_FACT": len(supports_rows),
    }


def restore_support_records(database: Any, immutable: dict[str, Any]) -> dict[str, int]:
    doc_ids = immutable["doc_ids"]
    collection = database["relation_support_records"]
    deleted = collection.delete_many(
        {"corpus_id": PROTECTED, "doc_id": {"$in": doc_ids}}
    ).deleted_count
    rows = immutable["support_records"]
    for batch in batched(rows, MONGO_BATCH):
        collection.insert_many(batch, ordered=True)
    return {"deleted": int(deleted), "inserted": len(rows)}


def verify_live(
    database: Any,
    driver: Any,
    immutable: dict[str, Any],
) -> dict[str, Any]:
    snapshot = target_snapshot(driver, immutable["doc_ids"])
    require(
        snapshot["nodes"]
        == {
            "Chunk": EXPECTED["chunks"],
            "Document": EXPECTED["documents"],
            "Fact": EXPECTED["facts"],
        },
        f"restored target node counts drifted: {snapshot['nodes']}",
    )
    require(
        snapshot["relationships"]
        == {
            "HAS_CHUNK": EXPECTED["has_chunk"],
            "HAS_FACT": EXPECTED["has_fact"],
            "MENTIONS": EXPECTED["mentions"],
            "SUPPORTS_FACT": EXPECTED["supports_fact"],
        },
        f"restored target relationship counts drifted: {snapshot['relationships']}",
    )
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    graph = graph_counts(driver, PROTECTED)
    require(
        graph == baseline["neo4j_counts_by_corpus"][PROTECTED],
        f"protected graph baseline did not close: {graph}",
    )
    support_target = list(
        database["relation_support_records"].find(
            {"corpus_id": PROTECTED, "doc_id": {"$in": immutable["doc_ids"]}}
        )
    )
    require(
        len(support_target) == EXPECTED["support_records"],
        "support target did not close",
    )
    require(
        bson_content_hash(support_target) == immutable["support_records_sha256"],
        "support-record content hash did not close",
    )
    support_total = database["relation_support_records"].count_documents(
        {"corpus_id": PROTECTED}
    )
    expected_support_total = int(
        baseline["mongo_counts_by_collection_and_corpus"]["relation_support_records"][
            PROTECTED
        ]
    )
    require(support_total == expected_support_total, "support total did not close")
    return {
        "target_nodes": snapshot["nodes"],
        "target_relationships": snapshot["relationships"],
        "protected_graph": graph,
        "support_record_target_count": len(support_target),
        "support_record_total_count": support_total,
        "support_records_sha256": immutable["support_records_sha256"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-manifest-sha")
    args = parser.parse_args()
    if args.apply:
        require(
            args.confirm_manifest_sha == MANIFEST_SHA256,
            "apply requires the exact immutable manifest SHA",
        )

    immutable = load_immutable_rows()
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        database = mongo[settings.MONGODB_DATABASE]
        built = build_plan(database, driver, immutable)
        plan = built["plan"]
        print(json.dumps(plan, indent=2, sort_keys=True), flush=True)
        if not args.apply:
            return 0

        journal = {
            "schema_version": "e2e_original_graph_restore_state.v1",
            "manifest_sha256": MANIFEST_SHA256,
            "plan_sha256": stable_hash(plan),
            "status": "running",
            "steps": [],
        }
        atomic_write(JOURNAL, journal)

        support_receipt = restore_support_records(database, immutable)
        journal["steps"].append({"step": "support_records_restore", **support_receipt})
        atomic_write(JOURNAL, journal)

        deleted_relationships = delete_target_relationships(
            driver, immutable["doc_ids"]
        )
        journal["steps"].append(
            {"step": "target_relationship_delete", "rows": deleted_relationships}
        )
        atomic_write(JOURNAL, journal)

        deleted_facts = delete_target_facts(driver, immutable["doc_ids"])
        journal["steps"].append({"step": "target_fact_delete", "rows": deleted_facts})
        atomic_write(JOURNAL, journal)

        node_receipt = restore_nodes(driver, immutable, built["legacy_only_chunks"])
        journal["steps"].append({"step": "original_node_restore", **node_receipt})
        atomic_write(JOURNAL, journal)

        relationship_receipt = restore_relationships(
            driver,
            immutable,
            built["desired_chunk_docs"],
        )
        journal["steps"].append(
            {"step": "original_relationship_restore", **relationship_receipt}
        )
        atomic_write(JOURNAL, journal)

        verification = verify_live(database, driver, immutable)
        journal["steps"].append({"step": "verification", **verification})
        journal["status"] = "done"
        atomic_write(JOURNAL, journal)
        print(
            json.dumps(
                {
                    "schema_version": "e2e_original_graph_restore_receipt.v1",
                    "status": "done",
                    "journal": str(JOURNAL),
                    "verification": verification,
                },
                indent=2,
                sort_keys=True,
            ),
            flush=True,
        )
        return 0
    finally:
        driver.close()
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
