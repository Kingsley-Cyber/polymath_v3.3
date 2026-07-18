#!/usr/bin/env python3
"""Real repair-cycle restart/idempotency assertion for Phase A g10."""

from __future__ import annotations

import argparse
import json
import urllib.request
from typing import Any

from bson import ObjectId
from config import get_settings
from neo4j import GraphDatabase
from pymongo import MongoClient
from qdrant_client import QdrantClient
from services.auth import auth_service


AUDIT_COLLECTION = "ingest_repair_runs"
IDENTITY_FIELDS = {
    "corpora": "corpus_id",
    "documents": "doc_id",
    "parent_chunks": "parent_id",
    "chunks": "chunk_id",
    "ghost_b_extractions": "chunk_id",
    "corpus_lexicon": "lexicon_id",
    "corpus_lexicon_sources": "source_id",
    "librarian_cards": "doc_id",
    "summary_tree": "node_id",
    "extraction_jobs": "job_id",
    "source_parse_jobs": "job_id",
    "summary_jobs": "job_id",
    "relation_support_records": "support_id",
    "ingest_batch_items": "item_id",
    "ingest_batches": "batch_id",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def mint_probe_token(db, corpus_id: str) -> str:
    corpus = db["corpora"].find_one(
        {"corpus_id": corpus_id},
        {"_id": 0, "user_id": 1},
    )
    require(bool(corpus and corpus.get("user_id")), "corpus owner is absent")
    user_id = str(corpus["user_id"])
    require(ObjectId.is_valid(user_id), "corpus owner id is invalid")
    user = db["users"].find_one(
        {"_id": ObjectId(user_id)},
        {"_id": 1, "username": 1},
    )
    require(bool(user and user.get("username")), "corpus owner user is absent")
    return auth_service.create_access_token(
        user_id=str(user["_id"]),
        username=str(user["username"]),
    )


def mongo_counts(db, corpus_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in sorted(db.list_collection_names()):
        count = int(db[name].count_documents({"corpus_id": corpus_id}))
        if count:
            counts[name] = count
    return counts


def duplicate_audit(db, corpus_id: str) -> dict[str, dict[str, int]]:
    report: dict[str, dict[str, int]] = {}
    for collection, field in IDENTITY_FIELDS.items():
        total = int(db[collection].count_documents({"corpus_id": corpus_id}))
        if total == 0:
            continue
        missing = int(
            db[collection].count_documents(
                {
                    "corpus_id": corpus_id,
                    "$or": [
                        {field: {"$exists": False}},
                        {field: None},
                        {field: ""},
                    ],
                }
            )
        )
        duplicates = list(
            db[collection].aggregate(
                [
                    {"$match": {"corpus_id": corpus_id}},
                    {"$group": {"_id": f"${field}", "count": {"$sum": 1}}},
                    {"$match": {"_id": {"$nin": [None, ""]}, "count": {"$gt": 1}}},
                    {"$count": "groups"},
                ]
            )
        )
        report[collection] = {
            "rows": total,
            "missing_identity": missing,
            "duplicate_identity_groups": int(duplicates[0]["groups"]) if duplicates else 0,
        }
    return report


def qdrant_counts(qdrant: QdrantClient, prefix: str, corpus_id: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for kind in ("naive", "hrag", "graph", "schemas"):
        name = f"{prefix}{corpus_id[:8]}_{kind}"
        result[kind] = int(qdrant.get_collection(name).points_count or 0)
    return result


def neo4j_counts(driver, corpus_id: str) -> dict[str, int]:
    with driver.session() as session:
        row = session.run(
            """
            MATCH (c:Chunk {corpus_id: $corpus_id})
            OPTIONAL MATCH (c)-[r]-()
            OPTIONAL MATCH (c)-[:MENTIONS]->(e:Entity)
            RETURN count(DISTINCT c) AS chunks,
                   count(DISTINCT r) AS incident_relationships,
                   count(DISTINCT e) AS mentioned_entities
            """,
            corpus_id=corpus_id,
        ).single()
        document_row = session.run(
            "MATCH (d:Document {corpus_id: $corpus_id}) RETURN count(d) AS documents",
            corpus_id=corpus_id,
        ).single()
    return {
        "chunks": int((row or {}).get("chunks") or 0),
        "incident_relationships": int((row or {}).get("incident_relationships") or 0),
        "mentioned_entities": int((row or {}).get("mentioned_entities") or 0),
        "documents": int((document_row or {}).get("documents") or 0),
    }


def snapshot(db, qdrant, driver, settings, corpus_id: str) -> dict[str, Any]:
    return {
        "mongo": mongo_counts(db, corpus_id),
        "duplicates": duplicate_audit(db, corpus_id),
        "qdrant": qdrant_counts(qdrant, settings.QDRANT_COLLECTION_PREFIX, corpus_id),
        "neo4j": neo4j_counts(driver, corpus_id),
    }


def trigger_repair(base: str, token: str, corpus_id: str) -> dict[str, Any]:
    body = {
        "apply": True,
        "background": False,
        "reconcile_failures": True,
        "plan_source_parse_jobs": True,
        "run_source_parse_jobs": True,
        "source_parse_job_plan_limit": 500,
        "source_parse_job_run_limit": 500,
        "plan_document_pipeline_jobs": True,
        "run_document_pipeline_jobs": True,
        "document_pipeline_job_plan_limit": 500,
        "document_pipeline_job_run_limit": 500,
        "plan_extraction_jobs": True,
        "run_extraction_jobs": True,
        "extraction_job_plan_limit": 500,
        "extraction_job_run_limit": 500,
        "plan_summary_jobs": True,
        "run_summary_jobs": True,
        "summary_job_plan_limit": 500,
        "summary_job_run_limit": 500,
        "run_document_summaries": True,
        "document_summary_limit": 10,
        "plan_graph_jobs": True,
        "run_graph_jobs": True,
        "graph_plan_limit": 500,
        "graph_run_limit": 100,
    }
    request = urllib.request.Request(
        f"{base.rstrip('/')}/api/corpora/{corpus_id}/ingestion/repair-cycle",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=1200) as response:
        require(response.status == 200, f"repair-cycle HTTP status {response.status}")
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--base", default="http://localhost:8000")
    args = parser.parse_args()
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    db = mongo[settings.MONGODB_DATABASE]
    qdrant = QdrantClient(url=settings.QDRANT_URL, timeout=settings.QDRANT_TIMEOUT_SECONDS)
    neo4j = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        token = mint_probe_token(db, args.corpus_id)
        before = snapshot(db, qdrant, neo4j, settings, args.corpus_id)
        print(json.dumps({"phase": "before", "snapshot": before}, indent=2, sort_keys=True), flush=True)
        response = trigger_repair(args.base, token, args.corpus_id)
        after = snapshot(db, qdrant, neo4j, settings, args.corpus_id)

        all_mongo_names = sorted(set(before["mongo"]) | set(after["mongo"]))
        mongo_deltas = {
            name: after["mongo"].get(name, 0) - before["mongo"].get(name, 0)
            for name in all_mongo_names
        }
        artifact_deltas = {
            name: delta for name, delta in mongo_deltas.items() if name != AUDIT_COLLECTION
        }
        changed_steps = [
            str(step.get("name") or "unknown")
            for step in (response.get("steps") or [])
            if step.get("changed") is True
        ]
        step_statuses = [
            {
                "name": str(step.get("name") or "unknown"),
                "status": str(step.get("status") or ""),
                "changed": bool(step.get("changed")),
            }
            for step in (response.get("steps") or [])
        ]
        duplicate_failures = {
            name: values
            for name, values in after["duplicates"].items()
            if values["missing_identity"] or values["duplicate_identity_groups"]
        }
        report = {
            "gate": "g10",
            "endpoint": f"/api/corpora/{args.corpus_id}/ingestion/repair-cycle",
            "probe_token_used": True,
            "repair_status": response.get("status"),
            "readiness_after": (response.get("readiness_after") or {}).get("status"),
            "step_statuses": step_statuses,
            "changed_steps": changed_steps,
            "before": before,
            "after": after,
            "mongo_row_deltas": mongo_deltas,
            "artifact_row_deltas": artifact_deltas,
            "audit_repair_run_delta": mongo_deltas.get(AUDIT_COLLECTION, 0),
            "duplicate_failures": duplicate_failures,
            "qdrant_unchanged": before["qdrant"] == after["qdrant"],
            "neo4j_unchanged": before["neo4j"] == after["neo4j"],
        }
        print(json.dumps(report, indent=2, sort_keys=True), flush=True)

        require(response.get("status") == "complete", "repair-cycle did not complete")
        require(
            (response.get("readiness_after") or {}).get("status") == "fully_enriched",
            "repair-cycle did not preserve fully_enriched readiness",
        )
        require(
            all(delta == 0 for delta in artifact_deltas.values()),
            f"corpus artifact row counts changed: {artifact_deltas}",
        )
        require(
            mongo_deltas.get(AUDIT_COLLECTION, 0) == 1,
            "repair audit history did not append exactly one run receipt",
        )
        require(not duplicate_failures, f"identity duplicate audit failed: {duplicate_failures}")
        require(before["duplicates"] == after["duplicates"], "identity census changed")
        require(before["qdrant"] == after["qdrant"], "Qdrant point counts changed")
        require(before["neo4j"] == after["neo4j"], "Neo4j corpus counts changed")
        return 0
    finally:
        neo4j.close()
        qdrant.close()
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
