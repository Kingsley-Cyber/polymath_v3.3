#!/usr/bin/env python3
"""Read-only before/after closure for the 15-document ecom modernization."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_settings
from neo4j import GraphDatabase
from pymongo import MongoClient
from qdrant_client import QdrantClient
from qdrant_client import models as qmodels


ECOM = "fd460347-61cc-4358-87fc-4b2a80533f0a"
E2E = "2c894530-8d57-4432-a6d4-bc14505a698b"
BACKUP_MANIFEST = Path(
    "/data/ingest-files/runpod-job-journals/"
    "e2e-isolation-backup-20260716T0046Z/manifest.json"
)
BACKUP_MANIFEST_SHA256 = (
    "e4fc35f387e75350b75762faa0266a429d135c82cdf6d3626834dc58ec9c737a"
)
MODERNIZATION_STATE = Path(
    "/data/ingest-files/runpod-job-journals/" "e2e-ecom-15doc-modernization-state.json"
)
BASELINE = Path(
    "/data/ingest-files/runpod-job-journals/" "e2e-ecom-modernization-e2e-baseline.json"
)
OUTPUT = Path(
    "/data/ingest-files/runpod-job-journals/" "e2e-ecom-modernization-closure.json"
)
ACTIVE_STATUSES = ("running", "in_progress", "processing", "leased")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hash(values: list[Any]) -> str:
    encoded = [
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
        for value in values
    ]
    return hashlib.sha256("\n".join(sorted(encoded)).encode()).hexdigest()


def atomic_write(path: Path, value: Any) -> None:
    require(not path.exists(), f"refusing to overwrite {path}")
    payload = json.dumps(value, indent=2, sort_keys=True, default=str).encode() + b"\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    directory = os.open(str(path.parent), os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def load_scope() -> tuple[dict[str, Any], list[str]]:
    payload = BACKUP_MANIFEST.read_bytes()
    require(
        hashlib.sha256(payload).hexdigest() == BACKUP_MANIFEST_SHA256,
        "sealed backup manifest drifted",
    )
    manifest = json.loads(payload)
    doc_ids = sorted(str(value) for value in manifest["shared_doc_ids"])
    require(len(doc_ids) == 15 and len(set(doc_ids)) == 15, "manifest scope drifted")
    return manifest, doc_ids


def mongo_rows(
    database: Any,
    collection: str,
    corpus_id: str,
    doc_ids: list[str],
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"corpus_id": corpus_id, "doc_id": {"$in": doc_ids}}
    if collection not in {"summary_tree", "relation_support_records"}:
        query["status"] = {"$ne": "deleted"}
    return list(database[collection].find(query, {"_id": 0}))


def mongo_snapshot(database: Any, corpus_id: str, doc_ids: list[str]) -> dict[str, Any]:
    identities = {
        "documents": "doc_id",
        "chunks": "chunk_id",
        "parent_chunks": "parent_id",
        "ghost_b_extractions": "chunk_id",
        "relation_support_records": "support_id",
        "summary_tree": "node_id",
    }
    output: dict[str, Any] = {}
    for collection, identity_field in identities.items():
        rows = mongo_rows(database, collection, corpus_id, doc_ids)
        identity_values = [str(row.get(identity_field) or "") for row in rows]
        output[collection] = {
            "rows": len(rows),
            "content_sha256": canonical_hash(rows),
            "identity_field": identity_field,
            "identity_count": len(set(identity_values) - {""}),
            "identity_sha256": canonical_hash(sorted(set(identity_values) - {""})),
        }
    verified = database["documents"].count_documents(
        {
            "corpus_id": corpus_id,
            "doc_id": {"$in": doc_ids},
            "status": {"$ne": "deleted"},
            "write_state.verified": True,
        }
    )
    output["verified_documents"] = verified
    for collection in (
        "corpora",
        "corpus_lexicon",
        "corpus_lexicon_sources",
        "corpus_readiness",
        "graph_domain_cache",
        "graph_metrics_cache",
    ):
        rows = list(database[collection].find({"corpus_id": corpus_id}, {"_id": 0}))
        output[f"corpus_scope::{collection}"] = {
            "rows": len(rows),
            "content_sha256": canonical_hash(rows),
        }
    return output


def qdrant_payload_rows(
    client: QdrantClient, collection: str, corpus_id: str, doc_ids: list[str]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = None
    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="corpus_id", match=qmodels.MatchValue(value=corpus_id)
            ),
            qmodels.FieldCondition(key="doc_id", match=qmodels.MatchAny(any=doc_ids)),
        ]
    )
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=query_filter,
            limit=256,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        rows.extend(dict(point.payload or {}) for point in points)
        if offset is None:
            return rows


def qdrant_snapshot(
    client: QdrantClient, corpus_id: str, doc_ids: list[str]
) -> dict[str, Any]:
    output: dict[str, Any] = {}
    prefix = f"corpus_{corpus_id[:8]}"
    for kind in ("naive", "hrag", "graph"):
        rows = qdrant_payload_rows(client, f"{prefix}_{kind}", corpus_id, doc_ids)
        identities = [
            {
                key: row.get(key)
                for key in ("doc_id", "chunk_id", "parent_id", "node_id", "node_type")
                if row.get(key) not in (None, "")
            }
            for row in rows
        ]
        output[kind] = {
            "points": len(rows),
            "payload_sha256": canonical_hash(rows),
            "identity_sha256": canonical_hash(identities),
        }
    profiles = qdrant_payload_rows(client, "polymath_doc_summaries", corpus_id, doc_ids)
    output["document_profiles"] = {
        "points": len(profiles),
        "payload_sha256": canonical_hash(profiles),
        "doc_id_sha256": canonical_hash(
            [str(row.get("doc_id") or "") for row in profiles]
        ),
    }
    return output


def neo4j_snapshot(driver: Any, corpus_id: str, doc_ids: list[str]) -> dict[str, Any]:
    nodes = driver.execute_query(
        "MATCH (n) WHERE n.corpus_id = $corpus_id AND n.doc_id IN $doc_ids "
        "RETURN labels(n) AS labels, properties(n) AS properties",
        corpus_id=corpus_id,
        doc_ids=doc_ids,
    ).records
    node_values = [
        {"labels": sorted(row["labels"]), "properties": dict(row["properties"])}
        for row in nodes
    ]
    edges = driver.execute_query(
        "MATCH (a)-[r]->(b) "
        "WHERE type(r) <> 'RELATES_TO' AND "
        "((a.corpus_id = $corpus_id AND a.doc_id IN $doc_ids) OR "
        " (b.corpus_id = $corpus_id AND b.doc_id IN $doc_ids)) "
        "RETURN labels(a) AS a_labels, properties(a) AS a, type(r) AS type, "
        "properties(r) AS r, labels(b) AS b_labels, properties(b) AS b",
        corpus_id=corpus_id,
        doc_ids=doc_ids,
    ).records

    def endpoint(labels: list[str], properties: dict[str, Any]) -> dict[str, Any]:
        props = dict(properties)
        if props.get("corpus_id") == corpus_id:
            selected = props
        else:
            selected = {
                key: props.get(key)
                for key in ("entity_id", "canonical_name", "name")
                if props.get(key) not in (None, "")
            }
        return {"labels": sorted(labels), "properties": selected}

    edge_values = [
        {
            "a": endpoint(row["a_labels"], row["a"]),
            "type": str(row["type"]),
            "r": dict(row["r"]),
            "b": endpoint(row["b_labels"], row["b"]),
        }
        for row in edges
    ]
    by_label: dict[str, list[str]] = {"Document": [], "Chunk": [], "Fact": []}
    for row in node_values:
        props = row["properties"]
        for label, field in (
            ("Document", "doc_id"),
            ("Chunk", "chunk_id"),
            ("Fact", "fact_id"),
        ):
            if label in row["labels"] and props.get(field):
                by_label[label].append(str(props[field]))
    return {
        "nodes": len(node_values),
        "node_content_sha256": canonical_hash(node_values),
        "relationships_excluding_global_relates_to": len(edge_values),
        "edge_content_sha256": canonical_hash(edge_values),
        "identity": {
            label: {
                "count": len(values),
                "unique": len(set(values)),
                "sha256": canonical_hash(sorted(set(values))),
            }
            for label, values in by_label.items()
        },
    }


def corpus_snapshot(
    database: Any,
    qdrant: QdrantClient,
    driver: Any,
    corpus_id: str,
    doc_ids: list[str],
) -> dict[str, Any]:
    return {
        "mongo": mongo_snapshot(database, corpus_id, doc_ids),
        "qdrant": qdrant_snapshot(qdrant, corpus_id, doc_ids),
        "neo4j": neo4j_snapshot(driver, corpus_id, doc_ids),
    }


def active_jobs(database: Any, doc_ids: list[str]) -> dict[str, int]:
    return {
        collection: database[collection].count_documents(
            {
                "corpus_id": ECOM,
                "doc_id": {"$in": doc_ids},
                "status": {"$in": list(ACTIVE_STATUSES)},
            }
        )
        for collection in (
            "source_parse_jobs",
            "document_pipeline_jobs",
            "extraction_jobs",
            "summary_jobs",
            "graph_promotion_jobs",
            "ingest_batch_items",
        )
    }


def require_shared_identity_closure(e2e: dict[str, Any], ecom: dict[str, Any]) -> None:
    for collection in ("documents", "chunks", "parent_chunks", "ghost_b_extractions"):
        for snapshot, label in ((e2e, "E2E"), (ecom, "ecom")):
            require(
                snapshot["mongo"][collection]["rows"]
                == snapshot["mongo"][collection]["identity_count"],
                f"{label} Mongo {collection} identities are not unique",
            )
        require(
            e2e["mongo"][collection]["rows"] == ecom["mongo"][collection]["rows"],
            f"Mongo {collection} row counts differ between corpora",
        )
        require(
            e2e["mongo"][collection]["identity_sha256"]
            == ecom["mongo"][collection]["identity_sha256"],
            f"Mongo {collection} identity sets differ between corpora",
        )
    for kind in ("naive", "hrag", "graph"):
        require(
            e2e["qdrant"][kind]["points"] == ecom["qdrant"][kind]["points"],
            f"Qdrant {kind} point counts differ between corpora",
        )
        require(
            e2e["qdrant"][kind]["identity_sha256"]
            == ecom["qdrant"][kind]["identity_sha256"],
            f"Qdrant {kind} identity sets differ between corpora",
        )
    for label in ("Document", "Chunk", "Fact"):
        for snapshot, corpus_label in ((e2e, "E2E"), (ecom, "ecom")):
            require(
                snapshot["neo4j"]["identity"][label]["count"]
                == snapshot["neo4j"]["identity"][label]["unique"],
                f"{corpus_label} Neo4j {label} identities are not unique",
            )
        require(
            e2e["neo4j"]["identity"][label]["count"]
            == ecom["neo4j"]["identity"][label]["count"],
            f"Neo4j {label} counts differ between corpora",
        )
        require(
            e2e["neo4j"]["identity"][label]["sha256"]
            == ecom["neo4j"]["identity"][label]["sha256"],
            f"Neo4j {label} identity sets differ between corpora",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("baseline", "verify"))
    args = parser.parse_args()
    _, doc_ids = load_scope()
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    qdrant = QdrantClient(url=settings.QDRANT_URL, timeout=120)
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        database = mongo[settings.MONGODB_DATABASE]
        state = json.loads(MODERNIZATION_STATE.read_text(encoding="utf-8"))
        require(state["doc_ids"] == doc_ids, "modernization state scope drifted")
        if args.action == "baseline":
            require(state["phase"] == "deleted", "delete phase is not complete")
            e2e = corpus_snapshot(database, qdrant, driver, E2E, doc_ids)
            require(e2e["mongo"]["verified_documents"] == 15, "E2E not verified")
            value = {
                "schema_version": "e2e_ecom_modernization_baseline.v1",
                "created_at_utc": utc_now(),
                "backup_manifest_sha256": BACKUP_MANIFEST_SHA256,
                "e2e_corpus_id": E2E,
                "scope_doc_ids": doc_ids,
                "e2e_snapshot": e2e,
            }
            atomic_write(BASELINE, value)
            print(
                json.dumps(
                    {
                        "output": str(BASELINE),
                        "snapshot_sha256": canonical_hash([e2e]),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        require(BASELINE.exists(), "E2E before snapshot is absent")
        require(state["phase"] == "reingest_launched", "reingest was not launched")
        batch = database["ingest_batches"].find_one(
            {"batch_id": state.get("reingest_batch_id")}, {"_id": 0, "options": 0}
        )
        require(bool(batch), "reingest batch is absent")
        require(batch.get("status") in {"done", "completed"}, "batch is not terminal")
        batch_counts = dict(batch.get("counts") or {})
        require(int(batch_counts.get("done") or 0) == 15, "batch done count drifted")
        require(int(batch_counts.get("failed") or 0) == 0, "batch has failed items")
        require(int(batch_counts.get("skipped") or 0) == 0, "batch has skipped items")
        before = json.loads(BASELINE.read_text(encoding="utf-8"))
        e2e_after = corpus_snapshot(database, qdrant, driver, E2E, doc_ids)
        require(
            before["e2e_snapshot"] == e2e_after,
            "E2E corpus changed during ecom modernization",
        )
        ecom = corpus_snapshot(database, qdrant, driver, ECOM, doc_ids)
        require(ecom["mongo"]["verified_documents"] == 15, "ecom docs not verified")
        jobs = active_jobs(database, doc_ids)
        require(sum(jobs.values()) == 0, "ecom target writers remain active")
        require_shared_identity_closure(e2e_after, ecom)
        cross = driver.execute_query(
            "MATCH (a)-[r]->(b) WHERE a.corpus_id IN [$a, $b] "
            "AND b.corpus_id IN [$a, $b] AND a.corpus_id <> b.corpus_id "
            "AND any(label IN labels(a) WHERE label IN ['Document','Chunk','Fact']) "
            "AND any(label IN labels(b) WHERE label IN ['Document','Chunk','Fact']) "
            "RETURN count(r) AS count",
            a=ECOM,
            b=E2E,
        ).records
        cross_count = int(cross[0]["count"])
        require(cross_count == 0, "cross-corpus derived relationships exist")
        result = {
            "schema_version": "e2e_ecom_modernization_closure.v1",
            "created_at_utc": utc_now(),
            "backup_manifest_sha256": BACKUP_MANIFEST_SHA256,
            "batch": batch,
            "active_jobs": jobs,
            "e2e_snapshot_unchanged": True,
            "shared_identity_sets_match": True,
            "cross_corpus_derived_relationships": cross_count,
            "ecom_snapshot": ecom,
            "e2e_snapshot_sha256": canonical_hash([e2e_after]),
        }
        atomic_write(OUTPUT, result)
        print(
            json.dumps(
                {
                    "output": str(OUTPUT),
                    "result_sha256": canonical_hash([result]),
                    "batch_id": str(batch.get("batch_id") or ""),
                    "e2e_snapshot_unchanged": True,
                    "shared_identity_sets_match": True,
                    "cross_corpus_derived_relationships": cross_count,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        driver.close()
        qdrant.close()
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
