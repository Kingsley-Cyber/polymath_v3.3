#!/usr/bin/env python3
"""Exact, hashed, fail-closed backup for the E2E isolation repair."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from bson import json_util
from motor.motor_asyncio import AsyncIOMotorClient
from neo4j import AsyncGraphDatabase
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchAny, MatchValue

from config import get_settings


E2E = "2c894530-8d57-4432-a6d4-bc14505a698b"
PROTECTED = "fd460347-61cc-4358-87fc-4b2a80533f0a"
BASELINE = Path(
    "/data/ingest-files/runpod-job-journals/e2e-protected-existing-baseline.json"
)
BACKUP_ROOT = Path(
    "/data/ingest-files/runpod-job-journals/"
    "e2e-isolation-backup-20260716T0046Z"
)
STAGING = BACKUP_ROOT.with_name(BACKUP_ROOT.name + ".staging")
SENSITIVE_NAMES = {
    "api_key",
    "api_keys",
    "secret",
    "secrets",
    "password",
    "access_token",
    "refresh_token",
    "authorization",
    "bearer_token",
    "private_key",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def scan_sensitive(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = str(key).strip().lower()
            if normalized in SENSITIVE_NAMES:
                raise RuntimeError(
                    "secret-bearing field refused at " + ".".join((*path, str(key)))
                )
            scan_sensitive(nested, (*path, str(key)))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            scan_sensitive(nested, (*path, str(index)))


def sensitive_paths(value: Any, path: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_path = (*path, str(key))
            if str(key).strip().lower() in SENSITIVE_NAMES:
                found.append(".".join(nested_path))
                continue
            found.extend(sensitive_paths(nested, nested_path))
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            found.extend(sensitive_paths(nested, (*path, str(index))))
    return found


class GzipJsonl:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._raw = path.open("wb")
        self._gzip = gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=self._raw,
            mtime=0,
        )
        self._text = io.TextIOWrapper(self._gzip, encoding="utf-8", newline="\n")
        self.rows = 0

    def write(self, value: Any) -> None:
        scan_sensitive(value)
        self._text.write(canonical_json(value))
        self._text.write("\n")
        self.rows += 1

    def close(self) -> None:
        self._text.flush()
        self._text.detach()
        self._gzip.close()
        self._raw.flush()
        os.fsync(self._raw.fileno())
        self._raw.close()


def file_receipt(path: Path, rows: int) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.name,
        "rows": rows,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): json_ready(nested) for key, nested in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(nested) for nested in value]
    if hasattr(value, "iso_format"):
        return value.iso_format()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def dump_mongo(
    db: Any,
    *,
    shared_doc_ids: list[str],
) -> dict[str, Any]:
    output = GzipJsonl(STAGING / "mongo.jsonl.gz")
    counts: dict[str, dict[str, int]] = {}
    hash_only_rows: list[dict[str, Any]] = []
    try:
        for collection_name in sorted(await db.list_collection_names()):
            if collection_name.startswith("system."):
                continue
            collection = db[collection_name]
            queries = {
                "e2e": {"corpus_id": E2E},
                "protected": {
                    "corpus_id": PROTECTED,
                    "$or": [
                        {"doc_id": {"$in": shared_doc_ids}},
                        {"document_id": {"$in": shared_doc_ids}},
                        {"source_document_ids": {"$in": shared_doc_ids}},
                        {"parent_doc_id": {"$in": shared_doc_ids}},
                    ],
                },
            }
            collection_counts: dict[str, int] = {}
            for scope, query in queries.items():
                expected = int(await collection.count_documents(query))
                observed = 0
                cursor = collection.find(query).sort("_id", 1)
                async for document in cursor:
                    serialized = json.loads(
                        json_util.dumps(document, json_options=json_util.CANONICAL_JSON_OPTIONS)
                    )
                    paths = sensitive_paths(serialized, ("document",))
                    if paths:
                        if collection_name != "corpora":
                            raise RuntimeError(
                                "secret-bearing field refused outside untouched "
                                f"corpus config: {collection_name}/{scope} {paths}"
                            )
                        content_sha256 = hashlib.sha256(
                            canonical_json(serialized).encode("utf-8")
                        ).hexdigest()
                        receipt = {
                            "scope": scope,
                            "collection": collection_name,
                            "content_sha256": content_sha256,
                            "sensitive_field_paths": paths,
                            "disposition": (
                                "hash_only_unmoved_untouched_config_by_key_law"
                            ),
                        }
                        output.write(receipt)
                        hash_only_rows.append(receipt)
                    else:
                        output.write(
                            {
                                "scope": scope,
                                "collection": collection_name,
                                "document": serialized,
                            }
                        )
                    observed += 1
                if observed != expected:
                    raise RuntimeError(
                        f"Mongo count drift {collection_name}/{scope}: "
                        f"expected={expected} observed={observed}"
                    )
                if observed:
                    collection_counts[scope] = observed
            if collection_counts:
                counts[collection_name] = collection_counts
    finally:
        output.close()
    return {
        "receipt": file_receipt(output.path, output.rows),
        "counts": counts,
        "hash_only_unmoved_untouched_config_rows": hash_only_rows,
    }


def qdrant_filter(*, corpus_id: str, doc_ids: list[str] | None = None) -> Filter:
    must = [FieldCondition(key="corpus_id", match=MatchValue(value=corpus_id))]
    if doc_ids is not None:
        must.append(FieldCondition(key="doc_id", match=MatchAny(any=doc_ids)))
    return Filter(must=must)


async def dump_qdrant_collection(
    client: AsyncQdrantClient,
    output: GzipJsonl,
    *,
    scope: str,
    collection_name: str,
    query_filter: Filter,
) -> int:
    if not await client.collection_exists(collection_name):
        return 0
    expected_result = await client.count(
        collection_name=collection_name,
        count_filter=query_filter,
        exact=True,
    )
    expected = int(expected_result.count)
    observed = 0
    offset = None
    while True:
        records, offset = await client.scroll(
            collection_name=collection_name,
            scroll_filter=query_filter,
            limit=128,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        for point in records:
            output.write(
                {
                    "scope": scope,
                    "collection": collection_name,
                    "id": str(point.id),
                    "payload": json_ready(dict(point.payload or {})),
                    "vector": json_ready(point.vector),
                }
            )
            observed += 1
        if offset is None:
            break
    if observed != expected:
        raise RuntimeError(
            f"Qdrant count drift {collection_name}/{scope}: "
            f"expected={expected} observed={observed}"
        )
    return observed


async def dump_qdrant(
    client: AsyncQdrantClient,
    *,
    shared_doc_ids: list[str],
) -> dict[str, Any]:
    output = GzipJsonl(STAGING / "qdrant.jsonl.gz")
    counts: dict[str, dict[str, int]] = {}
    try:
        for corpus_id, scope, doc_ids in (
            (E2E, "e2e", None),
            (PROTECTED, "protected", shared_doc_ids),
        ):
            prefix = corpus_id[:8]
            for kind in ("naive", "hrag", "graph", "schemas"):
                collection_name = f"corpus_{prefix}_{kind}"
                observed = await dump_qdrant_collection(
                    client,
                    output,
                    scope=scope,
                    collection_name=collection_name,
                    query_filter=qdrant_filter(corpus_id=corpus_id, doc_ids=doc_ids),
                )
                if observed:
                    counts.setdefault(collection_name, {})[scope] = observed
            observed = await dump_qdrant_collection(
                client,
                output,
                scope=scope,
                collection_name="polymath_doc_summaries",
                query_filter=qdrant_filter(corpus_id=corpus_id, doc_ids=doc_ids),
            )
            if observed:
                counts.setdefault("polymath_doc_summaries", {})[scope] = observed
    finally:
        output.close()
    return {"receipt": file_receipt(output.path, output.rows), "counts": counts}


async def dump_neo4j(
    driver: Any,
    *,
    shared_doc_ids: list[str],
) -> dict[str, Any]:
    nodes = GzipJsonl(STAGING / "neo4j_nodes.jsonl.gz")
    relationships = GzipJsonl(STAGING / "neo4j_relationships.jsonl.gz")
    node_query = """
    MATCH (n)
    WHERE (
        (n:Document OR n:Chunk OR n:Fact)
        AND (
            n.corpus_id = $e2e
            OR (n.corpus_id = $protected AND n.doc_id IN $doc_ids)
        )
    ) OR (
        n:Entity AND EXISTS {
            MATCH (c:Chunk)-[:MENTIONS]->(n)
            WHERE c.corpus_id = $e2e
               OR (c.corpus_id = $protected AND c.doc_id IN $doc_ids)
        }
    )
    RETURN elementId(n) AS element_id, labels(n) AS labels, properties(n) AS props
    ORDER BY labels, coalesce(n.corpus_id, ''), coalesce(n.doc_id, ''),
             coalesce(n.chunk_id, ''), coalesce(n.fact_id, ''),
             coalesce(n.entity_id, '')
    """
    relationship_query = """
    MATCH (a)-[r]->(b)
    WHERE r.corpus_id = $e2e
       OR $e2e IN coalesce(r.corpus_ids, [])
       OR (
            (a:Document OR a:Chunk OR a:Fact)
            AND (
                a.corpus_id = $e2e
                OR (a.corpus_id = $protected AND a.doc_id IN $doc_ids)
            )
       )
       OR (
            (b:Document OR b:Chunk OR b:Fact)
            AND (
                b.corpus_id = $e2e
                OR (b.corpus_id = $protected AND b.doc_id IN $doc_ids)
            )
       )
       OR any(doc_id IN coalesce(r.evidence_doc_ids, []) WHERE doc_id IN $doc_ids)
    RETURN elementId(r) AS element_id, type(r) AS type, properties(r) AS props,
           labels(a) AS start_labels, properties(a) AS start_props,
           labels(b) AS end_labels, properties(b) AS end_props
    ORDER BY type, coalesce(a.corpus_id, ''), coalesce(a.doc_id, ''),
             coalesce(a.chunk_id, ''), coalesce(a.entity_id, ''),
             coalesce(b.corpus_id, ''), coalesce(b.doc_id, ''),
             coalesce(b.chunk_id, ''), coalesce(b.entity_id, '')
    """
    try:
        async with driver.session() as session:
            result = await session.run(
                node_query,
                e2e=E2E,
                protected=PROTECTED,
                doc_ids=shared_doc_ids,
            )
            async for row in result:
                nodes.write(
                    {
                        "element_id": str(row["element_id"]),
                        "labels": sorted(str(value) for value in row["labels"]),
                        "props": json_ready(dict(row["props"] or {})),
                    }
                )
            result = await session.run(
                relationship_query,
                e2e=E2E,
                protected=PROTECTED,
                doc_ids=shared_doc_ids,
            )
            async for row in result:
                relationships.write(
                    {
                        "element_id": str(row["element_id"]),
                        "type": str(row["type"]),
                        "props": json_ready(dict(row["props"] or {})),
                        "start_labels": sorted(
                            str(value) for value in row["start_labels"]
                        ),
                        "start_props": json_ready(dict(row["start_props"] or {})),
                        "end_labels": sorted(str(value) for value in row["end_labels"]),
                        "end_props": json_ready(dict(row["end_props"] or {})),
                    }
                )
    finally:
        nodes.close()
        relationships.close()
    return {
        "nodes": file_receipt(nodes.path, nodes.rows),
        "relationships": file_receipt(relationships.path, relationships.rows),
    }


async def main() -> None:
    if BACKUP_ROOT.exists() or STAGING.exists():
        raise RuntimeError("refusing to overwrite existing backup or staging directory")
    STAGING.mkdir(parents=True, mode=0o700)
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL, timeout=120)
    neo4j = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        db = mongo[settings.MONGODB_DATABASE]
        e2e_docs = await db["documents"].find(
            {"corpus_id": E2E},
            {"_id": 0, "doc_id": 1},
        ).to_list(length=None)
        shared_doc_ids = sorted(
            str(row.get("doc_id") or "") for row in e2e_docs if row.get("doc_id")
        )
        if len(shared_doc_ids) != 15 or len(set(shared_doc_ids)) != 15:
            raise RuntimeError("expected exactly 15 unique E2E document IDs")
        protected_count = await db["documents"].count_documents(
            {"corpus_id": PROTECTED, "doc_id": {"$in": shared_doc_ids}}
        )
        if protected_count != 15:
            raise RuntimeError(
                f"expected 15 protected matching documents, observed {protected_count}"
            )

        baseline_copy = STAGING / BASELINE.name
        shutil.copyfile(BASELINE, baseline_copy)
        os.chmod(baseline_copy, 0o600)
        baseline_receipt = file_receipt(baseline_copy, 1)

        mongo_receipt = await dump_mongo(db, shared_doc_ids=shared_doc_ids)
        qdrant_receipt = await dump_qdrant(qdrant, shared_doc_ids=shared_doc_ids)
        neo4j_receipt = await dump_neo4j(neo4j, shared_doc_ids=shared_doc_ids)
        manifest = {
            "schema_version": "runpod_e2e_isolation_exact_backup.v1",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "e2e_corpus_id": E2E,
            "protected_corpus_id": PROTECTED,
            "shared_doc_ids": shared_doc_ids,
            "baseline": baseline_receipt,
            "mongo": mongo_receipt,
            "qdrant": qdrant_receipt,
            "neo4j": neo4j_receipt,
            "secret_field_scan": (
                "passed for all restorable identity rows; untouched corpus "
                "configuration is hash-only and was not moved"
            ),
        }
        manifest_path = STAGING / "manifest.json"
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
        manifest_path.write_bytes(manifest_bytes)
        os.chmod(manifest_path, 0o600)
        with manifest_path.open("rb") as handle:
            os.fsync(handle.fileno())
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        sha_path = STAGING / "MANIFEST.sha256"
        sha_path.write_text(f"{manifest_sha}  manifest.json\n", encoding="utf-8")
        os.chmod(sha_path, 0o600)
        with sha_path.open("rb") as handle:
            os.fsync(handle.fileno())
        directory_fd = os.open(str(STAGING), os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        os.replace(STAGING, BACKUP_ROOT)
        parent_fd = os.open(str(BACKUP_ROOT.parent), os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        print(
            json.dumps(
                {
                    "backup_root": str(BACKUP_ROOT),
                    "manifest_sha256": manifest_sha,
                    "mongo_rows": mongo_receipt["receipt"]["rows"],
                    "qdrant_rows": qdrant_receipt["receipt"]["rows"],
                    "neo4j_nodes": neo4j_receipt["nodes"]["rows"],
                    "neo4j_relationships": neo4j_receipt["relationships"]["rows"],
                    "hash_only_unmoved_config_rows": len(
                        mongo_receipt["hash_only_unmoved_untouched_config_rows"]
                    ),
                    "secret_field_scan": "passed_identity_rows",
                },
                indent=2,
                sort_keys=True,
            )
        )
    except Exception:
        if STAGING.exists():
            failed = STAGING.with_name(STAGING.name + ".failed")
            if not failed.exists():
                os.replace(STAGING, failed)
        raise
    finally:
        await neo4j.close()
        await qdrant.close()
        mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
