#!/usr/bin/env python3
"""Manifest-bound delete/reingest operator for the owner-approved 15-doc migration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bson import ObjectId
from config import get_settings
from models.schemas import IngestionConfig
from neo4j import GraphDatabase
from pymongo import MongoClient
from qdrant_client import QdrantClient
from qdrant_client import models as qmodels
from services.auth import auth_service


BACKUP_ROOT = Path(
    "/data/ingest-files/runpod-job-journals/e2e-isolation-backup-20260716T0046Z"
)
SOURCE_ROOT = Path("/ingest-source/runpod_e2e_15doc_20260715")
SELECTION = Path("/app/evals/runpod_e2e_15doc_selection_v1.json")
STATE = Path(
    "/data/ingest-files/runpod-job-journals/e2e-ecom-15doc-modernization-state.json"
)
MANIFEST_SHA256 = "e4fc35f387e75350b75762faa0266a429d135c82cdf6d3626834dc58ec9c737a"
SELECTION_SHA256 = "da7b94c152dd5e72d52db1fd80a68f0cc2797d85ed1fd4899f9a8c19874eaf00"
PROTECTED = "fd460347-61cc-4358-87fc-4b2a80533f0a"
E2E = "2c894530-8d57-4432-a6d4-bc14505a698b"
EXPECTED_DOCUMENTS = 15
SUMMARY_AUTHORITY_USD = "30.00"
ACTIVE_STATUSES = ["running", "in_progress", "processing", "leased"]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(payload).hexdigest()


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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_scope() -> dict[str, Any]:
    manifest_bytes = (BACKUP_ROOT / "manifest.json").read_bytes()
    require(
        hashlib.sha256(manifest_bytes).hexdigest() == MANIFEST_SHA256,
        "immutable backup manifest hash drifted",
    )
    manifest = json.loads(manifest_bytes)
    require(manifest["protected_corpus_id"] == PROTECTED, "protected ID drifted")
    require(manifest["e2e_corpus_id"] == E2E, "E2E ID drifted")
    doc_ids = sorted(str(value) for value in manifest["shared_doc_ids"])
    require(
        len(doc_ids) == EXPECTED_DOCUMENTS and len(set(doc_ids)) == EXPECTED_DOCUMENTS,
        "document manifest did not close at 15 unique IDs",
    )

    selection_bytes = SELECTION.read_bytes()
    require(
        hashlib.sha256(selection_bytes).hexdigest() == SELECTION_SHA256,
        "selection manifest hash drifted",
    )
    selection = json.loads(selection_bytes)
    expected_files = {
        str(row["filename"]): str(row["sha256"]) for row in selection["selected"]
    }
    require(len(expected_files) == EXPECTED_DOCUMENTS, "selection count drifted")
    observed_files = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in SOURCE_ROOT.iterdir()
        if not path.name.startswith("._") and path.is_file()
    }
    require(observed_files == expected_files, "source file identity drifted")
    return {
        "doc_ids": doc_ids,
        "expected_files": expected_files,
        "source_identity_sha256": stable_hash(expected_files),
    }


def mint_token(database: Any) -> str:
    users = list(database["users"].find({}, {"_id": 1, "username": 1}).limit(2))
    require(len(users) == 1, "exactly one API owner must be discoverable")
    owner = users[0]
    owner_id = str(owner.get("_id") or "")
    require(ObjectId.is_valid(owner_id), "API owner identity is invalid")
    require(bool(owner.get("username")), "API owner username is absent")
    return auth_service.create_access_token(
        user_id=owner_id, username=str(owner["username"])
    )


def api(
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    *,
    timeout_seconds: int = 1800,
) -> dict[str, Any]:
    payload = (
        json.dumps(body, separators=(",", ":")).encode("utf-8")
        if body is not None
        else None
    )
    request = urllib.request.Request(
        f"http://localhost:8000{path}",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            require(200 <= response.status < 300, f"API status {response.status}")
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"API status {exc.code} for {path}: {detail}") from exc


def active_job_counts(database: Any, doc_ids: list[str]) -> dict[str, int]:
    counts = {}
    for collection in (
        "source_parse_jobs",
        "document_pipeline_jobs",
        "extraction_jobs",
        "summary_jobs",
        "graph_promotion_jobs",
        "ingest_batch_items",
    ):
        counts[collection] = database[collection].count_documents(
            {
                "corpus_id": PROTECTED,
                "doc_id": {"$in": doc_ids},
                "status": {"$in": ACTIVE_STATUSES},
            }
        )
    return counts


def safe_config_projection(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "extraction_engine": config.get("extraction_engine"),
        "runpod_wire_contract": config.get("runpod_wire_contract"),
        "runpod_endpoint_id_override": config.get("runpod_endpoint_id_override"),
        "runpod_account_name_override": config.get("runpod_account_name_override"),
        "runpod_local_extraction_routes": config.get("runpod_local_extraction_routes")
        or [],
        "embedding_model": config.get("embedding_model"),
        "embedding_dimension": config.get("embedding_dimension"),
        "embedding_model_id": config.get("embedding_model_id"),
        "embed_mode": config.get("embed_mode"),
        "use_neo4j": config.get("use_neo4j"),
        "chunk_summarization": config.get("chunk_summarization"),
        "summary_model_count": len(config.get("summary_models") or []),
    }


def active_mongo_counts(database: Any, doc_ids: list[str]) -> dict[str, int]:
    query = {
        "corpus_id": PROTECTED,
        "doc_id": {"$in": doc_ids},
        "status": {"$ne": "deleted"},
    }
    counts = {}
    for collection in (
        "documents",
        "chunks",
        "parent_chunks",
        "ghost_b_extractions",
        "relation_support_records",
    ):
        counts[collection] = database[collection].count_documents(query)
    counts["summary_tree"] = database["summary_tree"].count_documents(
        {"corpus_id": PROTECTED, "doc_id": {"$in": doc_ids}}
    )
    counts["deleted_documents"] = database["documents"].count_documents(
        {
            "corpus_id": PROTECTED,
            "doc_id": {"$in": doc_ids},
            "status": "deleted",
        }
    )
    return counts


def qdrant_counts(client: QdrantClient, doc_ids: list[str]) -> dict[str, int]:
    query_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="corpus_id", match=qmodels.MatchValue(value=PROTECTED)
            ),
            qmodels.FieldCondition(key="doc_id", match=qmodels.MatchAny(any=doc_ids)),
        ]
    )
    output = {}
    for kind in ("naive", "hrag", "graph"):
        collection = f"corpus_{PROTECTED[:8]}_{kind}"
        output[kind] = int(
            client.count(
                collection_name=collection,
                count_filter=query_filter,
                exact=True,
            ).count
        )
    output["document_profiles"] = int(
        client.count(
            collection_name="polymath_doc_summaries",
            count_filter=query_filter,
            exact=True,
        ).count
    )
    return output


def neo4j_counts(driver: Any, doc_ids: list[str]) -> dict[str, int]:
    node_rows = driver.execute_query(
        "MATCH (n) WHERE n.corpus_id = $corpus_id AND n.doc_id IN $doc_ids "
        "RETURN count(n) AS count",
        corpus_id=PROTECTED,
        doc_ids=doc_ids,
    ).records
    edge_rows = driver.execute_query(
        "MATCH (a)-[r]->(b) "
        "WHERE (a.corpus_id = $corpus_id AND a.doc_id IN $doc_ids) "
        "OR (b.corpus_id = $corpus_id AND b.doc_id IN $doc_ids) "
        "RETURN count(r) AS count",
        corpus_id=PROTECTED,
        doc_ids=doc_ids,
    ).records
    return {
        "nodes": int(node_rows[0]["count"]),
        "relationships": int(edge_rows[0]["count"]),
    }


def store_snapshot(
    database: Any, qdrant: QdrantClient, driver: Any, doc_ids: list[str]
) -> dict[str, Any]:
    return {
        "mongo_active": active_mongo_counts(database, doc_ids),
        "qdrant": qdrant_counts(qdrant, doc_ids),
        "neo4j": neo4j_counts(driver, doc_ids),
        "active_jobs": active_job_counts(database, doc_ids),
    }


def require_deletion_closure(
    snapshot: dict[str, Any], *, expected_tombstones: int
) -> None:
    require(
        all(
            snapshot["mongo_active"][name] == 0
            for name in (
                "documents",
                "chunks",
                "parent_chunks",
                "ghost_b_extractions",
                "relation_support_records",
                "summary_tree",
            )
        ),
        "Mongo active deletion closure failed",
    )
    require(
        snapshot["mongo_active"]["deleted_documents"] == expected_tombstones,
        "Mongo document tombstone count drifted",
    )
    require(
        all(value == 0 for value in snapshot["qdrant"].values()),
        "Qdrant deletion closure failed",
    )
    require(
        snapshot["neo4j"] == {"nodes": 0, "relationships": 0},
        "Neo4j deletion closure failed",
    )


def document_filename_map(database: Any, doc_ids: list[str]) -> dict[str, str]:
    rows = database["documents"].find(
        {
            "corpus_id": PROTECTED,
            "doc_id": {"$in": doc_ids},
            "status": {"$ne": "deleted"},
        },
        {"_id": 0, "doc_id": 1, "filename": 1, "original_filename": 1},
    )
    return {
        str(row["doc_id"]): str(
            row.get("original_filename") or row.get("filename") or ""
        )
        for row in rows
    }


def upgrade_runpod_contract(database: Any) -> dict[str, Any]:
    protected = database["corpora"].find_one(
        {"corpus_id": PROTECTED}, {"_id": 0, "default_ingestion_config": 1}
    )
    e2e = database["corpora"].find_one(
        {"corpus_id": E2E}, {"_id": 0, "default_ingestion_config": 1}
    )
    require(protected is not None and e2e is not None, "corpus config is absent")
    old_config = dict(protected.get("default_ingestion_config") or {})
    routes = list(
        (e2e.get("default_ingestion_config") or {}).get(
            "runpod_local_extraction_routes"
        )
        or []
    )
    require(len(routes) == 2, "certified E2E RunPod route count drifted")
    candidate = dict(old_config)
    candidate["runpod_wire_contract"] = "local_extraction_v1"
    candidate["runpod_local_extraction_routes"] = routes
    candidate["runpod_endpoint_id_override"] = None
    candidate["runpod_account_name_override"] = None
    validated = IngestionConfig.model_validate(candidate)
    require(
        validated.runpod_wire_contract == "local_extraction_v1",
        "candidate RunPod contract validation failed",
    )
    result = database["corpora"].update_one(
        {"corpus_id": PROTECTED},
        {
            "$set": {
                "default_ingestion_config.runpod_wire_contract": "local_extraction_v1",
                "default_ingestion_config.runpod_local_extraction_routes": routes,
                "default_ingestion_config.runpod_endpoint_id_override": None,
                "default_ingestion_config.runpod_account_name_override": None,
            }
        },
    )
    require(result.matched_count == 1, "protected corpus config update missed")
    current = database["corpora"].find_one(
        {"corpus_id": PROTECTED}, {"_id": 0, "default_ingestion_config": 1}
    )
    current_config = dict((current or {}).get("default_ingestion_config") or {})
    IngestionConfig.model_validate(current_config)
    return {
        "before": safe_config_projection(old_config),
        "after": safe_config_projection(current_config),
        "certified_route_sha256": stable_hash(routes),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=("preflight", "delete", "resume-delete", "launch", "status"),
    )
    args = parser.parse_args()
    scope = load_scope()
    doc_ids = scope["doc_ids"]
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    database = mongo[settings.MONGODB_DATABASE]
    qdrant = QdrantClient(url=settings.QDRANT_URL, timeout=120)
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        snapshot = store_snapshot(database, qdrant, driver, doc_ids)
        if args.action == "preflight":
            filenames = document_filename_map(database, doc_ids)
            require(
                len(filenames) == EXPECTED_DOCUMENTS, "protected docs are incomplete"
            )
            require(
                set(filenames.values()) == set(scope["expected_files"]),
                "protected filename scope drifted",
            )
            require(
                sum(snapshot["active_jobs"].values()) == 0,
                "protected target has active writers",
            )
            require(not STATE.exists(), "modernization state already exists")
            result = {
                "schema_version": "e2e_ecom_15doc_modernization_preflight.v1",
                "manifest_sha256": MANIFEST_SHA256,
                "source_identity_sha256": scope["source_identity_sha256"],
                "document_count": len(filenames),
                "snapshot": snapshot,
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        require(STATE.exists() or args.action == "delete", "modernization state absent")
        if args.action == "delete":
            require(not STATE.exists(), "refusing duplicate delete launch")
            require(
                sum(snapshot["active_jobs"].values()) == 0,
                "protected target has active writers",
            )
            filenames = document_filename_map(database, doc_ids)
            require(
                len(filenames) == EXPECTED_DOCUMENTS, "protected docs are incomplete"
            )
            state = {
                "schema_version": "e2e_ecom_15doc_modernization_state.v1",
                "phase": "deleting",
                "started_at_utc": utc_now(),
                "updated_at_utc": utc_now(),
                "manifest_sha256": MANIFEST_SHA256,
                "source_identity_sha256": scope["source_identity_sha256"],
                "corpus_id": PROTECTED,
                "doc_ids": doc_ids,
                "filenames": filenames,
                "pre_delete_snapshot": snapshot,
                "deleted_doc_ids": [],
                "delete_completed_at_utc": None,
                "config_upgrade": None,
                "reingest_batch_id": None,
                "reingest_launched_at_utc": None,
            }
            atomic_write(STATE, state)
            token = mint_token(database)
            for doc_id in doc_ids:
                response = api(
                    token,
                    "DELETE",
                    f"/api/corpora/{PROTECTED}/documents/{doc_id}",
                )
                require(response.get("status") == "success", "delete response failed")
                require(response.get("doc_id") == doc_id, "delete response ID drifted")
                state["deleted_doc_ids"].append(doc_id)
                state["updated_at_utc"] = utc_now()
                atomic_write(STATE, state)
                print(f"DELETE_DONE {doc_id}", flush=True)
            post = store_snapshot(database, qdrant, driver, doc_ids)
            require_deletion_closure(post, expected_tombstones=EXPECTED_DOCUMENTS)
            state["phase"] = "deleted"
            state["post_delete_snapshot"] = post
            state["delete_completed_at_utc"] = utc_now()
            state["updated_at_utc"] = utc_now()
            atomic_write(STATE, state)
            print(json.dumps(state, indent=2, sort_keys=True))
            return 0

        state = json.loads(STATE.read_text(encoding="utf-8"))
        require(state["manifest_sha256"] == MANIFEST_SHA256, "state manifest drifted")
        require(state["doc_ids"] == doc_ids, "state document scope drifted")
        if args.action == "resume-delete":
            require(state["phase"] == "deleting", "delete phase is not resumable")
            require(
                sum(snapshot["active_jobs"].values()) == 0,
                "protected target has active writers",
            )
            document_rows = list(
                database["documents"].find(
                    {"corpus_id": PROTECTED, "doc_id": {"$in": doc_ids}},
                    {"_id": 0, "doc_id": 1, "status": 1},
                )
            )
            require(
                len(document_rows) == EXPECTED_DOCUMENTS,
                "manifest document rows are incomplete",
            )
            tombstoned = sorted(
                str(row["doc_id"])
                for row in document_rows
                if row.get("status") == "deleted"
            )
            for doc_id in tombstoned:
                require_deletion_closure(
                    store_snapshot(database, qdrant, driver, [doc_id]),
                    expected_tombstones=1,
                )
            state["deleted_doc_ids"] = tombstoned
            state["resume_reconciled_at_utc"] = utc_now()
            state["updated_at_utc"] = utc_now()
            atomic_write(STATE, state)
            print(f"RESUME_RECONCILED {len(tombstoned)}", flush=True)

            token = mint_token(database)
            for doc_id in doc_ids:
                if doc_id in tombstoned:
                    continue
                response = api(
                    token,
                    "DELETE",
                    f"/api/corpora/{PROTECTED}/documents/{doc_id}",
                    timeout_seconds=7200,
                )
                require(response.get("status") == "success", "delete response failed")
                require(response.get("doc_id") == doc_id, "delete response ID drifted")
                require_deletion_closure(
                    store_snapshot(database, qdrant, driver, [doc_id]),
                    expected_tombstones=1,
                )
                tombstoned.append(doc_id)
                tombstoned.sort()
                state["deleted_doc_ids"] = tombstoned
                state["updated_at_utc"] = utc_now()
                atomic_write(STATE, state)
                print(f"DELETE_DONE {doc_id}", flush=True)

            post = store_snapshot(database, qdrant, driver, doc_ids)
            require_deletion_closure(post, expected_tombstones=EXPECTED_DOCUMENTS)
            state["phase"] = "deleted"
            state["post_delete_snapshot"] = post
            state["delete_completed_at_utc"] = utc_now()
            state["updated_at_utc"] = utc_now()
            atomic_write(STATE, state)
            print(json.dumps(state, indent=2, sort_keys=True))
            return 0

        if args.action == "launch":
            require(state["phase"] == "deleted", "delete phase is not complete")
            current = store_snapshot(database, qdrant, driver, doc_ids)
            require(
                current["neo4j"] == {"nodes": 0, "relationships": 0},
                "Neo4j repopulated before launch",
            )
            require(
                all(value == 0 for value in current["qdrant"].values()),
                "Qdrant repopulated before launch",
            )
            config_upgrade = upgrade_runpod_contract(database)
            token = mint_token(database)
            batch = api(
                token,
                "POST",
                f"/api/corpora/{PROTECTED}/ingest-batches/local",
                {
                    "root_path": str(SOURCE_ROOT),
                    "profile": "runpod_burst",
                    "recursive": False,
                    "extensions": [".md"],
                    "max_files": EXPECTED_DOCUMENTS,
                    "store_files": True,
                    "max_total_bytes": 100000000,
                    "use_neo4j": True,
                    "chunk_summarization": True,
                    "model": "",
                    "concurrency": 1,
                    "summary_cost_authority_usd": SUMMARY_AUTHORITY_USD,
                    "start": True,
                },
            )
            batch_id = str(batch.get("batch_id") or "")
            require(batch_id, "reingest response omitted batch ID")
            require(
                int(batch.get("total") or 0) == EXPECTED_DOCUMENTS,
                "batch total drifted",
            )
            state["phase"] = "reingest_launched"
            state["config_upgrade"] = config_upgrade
            state["reingest_batch_id"] = batch_id
            state["reingest_launched_at_utc"] = utc_now()
            state["runner_started"] = bool(batch.get("runner_started"))
            state["summary_cost_authority_usd"] = SUMMARY_AUTHORITY_USD
            state["updated_at_utc"] = utc_now()
            atomic_write(STATE, state)
            print(json.dumps(state, indent=2, sort_keys=True))
            return 0

        batch_id = str(state.get("reingest_batch_id") or "")
        batch = (
            database["ingest_batches"].find_one(
                {"batch_id": batch_id}, {"_id": 0, "options": 0}
            )
            if batch_id
            else None
        )
        result = {
            "schema_version": "e2e_ecom_15doc_modernization_status.v1",
            "phase": state["phase"],
            "batch": batch,
            "snapshot": store_snapshot(database, qdrant, driver, doc_ids),
        }
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0
    finally:
        driver.close()
        qdrant.close()
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
