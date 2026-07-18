#!/usr/bin/env python3
"""Read-only E2E census for the owner-facing schema/metadata utilization report."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import get_settings
from models.registry_loader import load_all, registry_hashes
from neo4j import GraphDatabase
from pymongo import MongoClient
from qdrant_client import QdrantClient
from qdrant_client import models as qmodels


CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"
CORPUS_NAME = "runpod_e2e_15doc_20260715"

MONGO_FIELDS: dict[str, tuple[str, ...]] = {
    "documents": (
        "title",
        "author",
        "document_date",
        "bibliographic_provenance",
        "source_identity",
        "doc_profile",
    ),
    "chunks": (
        "heading_path",
        "chunk_kind",
        "entity_ids",
        "relation_predicates",
        "concepts",
        "mechanisms",
        "corpus_id",
    ),
    "parent_chunks": (
        "heading_path",
        "chunk_kind",
        "summary",
        "retrieval_text",
        "temporal_class",
        "time_expressions",
        "domain",
        "concepts",
        "mechanisms",
        "corpus_id",
    ),
    "ghost_b_extractions": (
        "source_version_id",
        "stage_identity",
        "local_extraction",
        "local_extraction.entities",
        "local_extraction.predicates",
        "local_extraction.relations",
        "local_extraction.sentence_ids",
        "local_extraction.unresolved_spans",
        "local_extraction.schema_version",
        "temporal_captures",
        "claim_compilation",
        "claim_compilation.claims",
        "claim_compilation.links",
        "claim_compilation.schema_version",
        "entities",
        "relations",
        "facts",
        "corpus_id",
    ),
    "summary_tree": (
        "summary",
        "retrieval_text",
        "heading_path",
        "temporal_class",
        "time_expressions",
        "corpus_id",
    ),
    "graph_domain_cache": (
        "clusters",
        "doc_assignments",
        "outliers",
        "corpus_change_signature",
        "corpus_id",
    ),
}

QDRANT_FIELDS = (
    "corpus_id",
    "doc_id",
    "chunk_id",
    "parent_id",
    "source_identity",
    "source_version_id",
    "heading_path",
    "chunk_kind",
    "temporal_class",
    "time_expressions",
    "entity_ids",
    "relation_predicates",
    "summary",
    "retrieval_text",
    "title",
    "author",
    "document_date",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def nested(document: dict[str, Any], path: str) -> tuple[bool, Any]:
    value: Any = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False, None
        value = value[part]
    return True, value


def nonempty(value: Any) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def field_counters(rows: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    counters = {
        field: {"present_rows": 0, "nonempty_rows": 0, "array_items": 0}
        for field in fields
    }
    total = 0
    for row in rows:
        total += 1
        for field in fields:
            present, value = nested(row, field)
            if present:
                counters[field]["present_rows"] += 1
            if present and nonempty(value):
                counters[field]["nonempty_rows"] += 1
            if isinstance(value, list):
                counters[field]["array_items"] += len(value)
    return {"rows": total, "fields": counters}


def mongo_census(database: Any) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for collection, fields in MONGO_FIELDS.items():
        query: dict[str, Any] = {"corpus_id": CORPUS_ID}
        if collection != "summary_tree":
            query["status"] = {"$ne": "deleted"}
        # Mongo rejects projections that include both a parent object and one
        # of its nested paths (for example ``local_extraction`` plus
        # ``local_extraction.entities``). Fetch each top-level object once;
        # field_counters() still evaluates the original nested field list.
        projection = {field.split(".", 1)[0]: 1 for field in fields}
        projection["_id"] = 0
        output[collection] = field_counters(
            database[collection].find(query, projection), fields
        )
    return output


def typed_schema_census(database: Any) -> dict[str, Any]:
    ghosts = database["ghost_b_extractions"]
    local_rows = ghosts.count_documents(
        {
            "corpus_id": CORPUS_ID,
            "local_extraction.schema_version": "local_extraction.v1",
        }
    )
    claim_rows = ghosts.count_documents(
        {
            "corpus_id": CORPUS_ID,
            "claim_compilation.schema_version": "claim_compilation.v1",
        }
    )
    digest_jobs = database["semantic_digest_jobs"]
    job_statuses = {
        str(row.get("_id") or "unknown"): int(row.get("count") or 0)
        for row in digest_jobs.aggregate(
            [
                {"$match": {"corpus_id": CORPUS_ID}},
                {"$group": {"_id": "$status", "count": {"$sum": 1}}},
            ]
        )
    }
    compilations = database["semantic_digest_claim_compilations"].count_documents(
        {"corpus_id": CORPUS_ID}
    )
    cache_keys = {
        str(row.get("cache_key") or "")
        for row in digest_jobs.find(
            {"corpus_id": CORPUS_ID, "cache_key": {"$exists": True, "$ne": ""}},
            {"_id": 0, "cache_key": 1},
        )
    }
    digest_cache_rows = (
        database["semantic_digest_cache"].count_documents(
            {"_id": {"$in": list(cache_keys)}}
        )
        if cache_keys
        else 0
    )
    registries = load_all()
    vocab = registries["vocab"]
    return {
        "local_extraction_v1": {"rows": local_rows},
        "claim_compilation_v1": {"rows": claim_rows},
        "semantic_digest_v1": {
            "jobs_by_status": job_statuses,
            "claim_input_compilations": compilations,
            "cache_rows_resolved_through_corpus_jobs": digest_cache_rows,
        },
        "semantic_artifacts": {
            "repository_rows": database["semantic_artifacts"].count_documents({}),
            "e2e_rows_by_top_level_corpus_id": database[
                "semantic_artifacts"
            ].count_documents({"corpus_id": CORPUS_ID}),
        },
        "registry_contracts": {
            "hashes": registry_hashes(),
            "domain_definitions": len(registries["domain"].get("domains") or []),
            "superframe_definitions": len(
                registries["superframe"].get("superframes") or []
            ),
            "motif_definitions": len(registries["motif"].get("motifs") or []),
            "predicate_types": len(vocab.get("predicate_types") or []),
            "superframe_rules": len(registries["superframe_rule"].get("rules") or []),
        },
    }


def qdrant_collection_census(
    client: QdrantClient,
    collection: str,
    *,
    corpus_filter: bool,
) -> dict[str, Any]:
    query_filter = None
    if corpus_filter:
        query_filter = qmodels.Filter(
            must=[
                qmodels.FieldCondition(
                    key="corpus_id", match=qmodels.MatchValue(value=CORPUS_ID)
                )
            ]
        )
    counters = {
        field: {"present_points": 0, "nonempty_points": 0, "array_items": 0}
        for field in QDRANT_FIELDS
    }
    total = 0
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=query_filter,
            limit=256,
            offset=offset,
            with_payload=list(QDRANT_FIELDS),
            with_vectors=False,
        )
        for point in points:
            total += 1
            payload = dict(point.payload or {})
            for field in QDRANT_FIELDS:
                if field in payload:
                    counters[field]["present_points"] += 1
                value = payload.get(field)
                if field in payload and nonempty(value):
                    counters[field]["nonempty_points"] += 1
                if isinstance(value, list):
                    counters[field]["array_items"] += len(value)
        if offset is None:
            break
    return {"points": total, "fields": counters}


def qdrant_census(client: QdrantClient) -> dict[str, Any]:
    prefix = f"corpus_{CORPUS_ID[:8]}"
    return {
        f"{prefix}_{kind}": qdrant_collection_census(
            client, f"{prefix}_{kind}", corpus_filter=False
        )
        for kind in ("naive", "hrag", "graph")
    } | {
        "polymath_doc_summaries": qdrant_collection_census(
            client, "polymath_doc_summaries", corpus_filter=True
        )
    }


def neo4j_census(settings: Any) -> dict[str, Any]:
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        label_rows, _, _ = driver.execute_query(
            "MATCH (n) WHERE n.corpus_id = $corpus_id "
            "UNWIND labels(n) AS label RETURN label, count(*) AS count ORDER BY label",
            corpus_id=CORPUS_ID,
        )
        relation_rows, _, _ = driver.execute_query(
            "MATCH (a)-[r]->(b) "
            "WHERE a.corpus_id = $corpus_id OR b.corpus_id = $corpus_id "
            "RETURN type(r) AS type, count(*) AS count ORDER BY type",
            corpus_id=CORPUS_ID,
        )
        entity_rows, _, _ = driver.execute_query(
            "MATCH (:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(e:Entity) "
            "RETURN count(DISTINCT e) AS entities",
            corpus_id=CORPUS_ID,
        )
        return {
            "corpus_node_labels": {
                str(row["label"]): int(row["count"]) for row in label_rows
            },
            "touching_relationship_types": {
                str(row["type"]): int(row["count"]) for row in relation_rows
            },
            "corpus_linked_global_entities": int(entity_rows[0]["entities"]),
        }
    finally:
        driver.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), "refusing to overwrite a census artifact")
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    qdrant = QdrantClient(url=settings.QDRANT_URL, timeout=120)
    try:
        database = mongo[settings.MONGODB_DATABASE]
        corpus = database["corpora"].find_one(
            {"corpus_id": CORPUS_ID}, {"_id": 0, "name": 1}
        )
        require(bool(corpus) and corpus.get("name") == CORPUS_NAME, "corpus drifted")
        active_documents = database["documents"].count_documents(
            {"corpus_id": CORPUS_ID, "status": {"$ne": "deleted"}}
        )
        require(active_documents == 15, "E2E corpus is not 15-document complete")
        result = {
            "schema_version": "e2e_schema_metadata_census.v1",
            "created_at_utc": utc_now(),
            "corpus_id": CORPUS_ID,
            "corpus_name": CORPUS_NAME,
            "active_documents": active_documents,
            "mongo": mongo_census(database),
            "typed_schemas": typed_schema_census(database),
            "qdrant": qdrant_census(qdrant),
            "neo4j": neo4j_census(settings),
        }
        payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "sha256": hashlib.sha256(payload.encode()).hexdigest(),
                    "active_documents": active_documents,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    finally:
        qdrant.close()
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
