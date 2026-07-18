#!/usr/bin/env python3
"""Hash corpus-owned Mongo, Qdrant, and Neo4j state without mutating it."""

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


CORPUS_ID = "2c894530-8d57-4432-a6d4-bc14505a698b"


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def atomic_write(path: Path, value: Any) -> None:
    if path.exists():
        raise RuntimeError(f"refusing to overwrite fingerprint: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(value, indent=2, sort_keys=True, default=str).encode() + b"\n"
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def mongo_fingerprint(database: Any) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for name in sorted(database.list_collection_names()):
        count = database[name].count_documents({"corpus_id": CORPUS_ID})
        if not count:
            continue
        digest = hashlib.sha256()
        observed = 0
        for row in database[name].find({"corpus_id": CORPUS_ID}).sort("_id", 1):
            digest.update(canonical(row))
            digest.update(b"\n")
            observed += 1
        if observed != count:
            raise RuntimeError(f"Mongo count drift during fingerprint: {name}")
        output[name] = {"count": count, "sha256": digest.hexdigest()}
    return output


def qdrant_fingerprint(client: QdrantClient) -> dict[str, Any]:
    corpus_filter = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="corpus_id", match=qmodels.MatchValue(value=CORPUS_ID)
            )
        ]
    )
    output: dict[str, Any] = {}
    names = sorted(row.name for row in client.get_collections().collections)
    for name in names:
        expected = int(
            client.count(
                collection_name=name,
                count_filter=corpus_filter,
                exact=True,
            ).count
        )
        if not expected:
            continue
        digest = hashlib.sha256()
        observed = 0
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=name,
                scroll_filter=corpus_filter,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )
            for point in points:
                digest.update(
                    canonical({"id": str(point.id), "payload": point.payload or {}})
                )
                digest.update(b"\n")
                observed += 1
            if offset is None:
                break
        if observed != expected:
            raise RuntimeError(f"Qdrant count drift during fingerprint: {name}")
        output[name] = {"count": expected, "sha256": digest.hexdigest()}
    return output


def neo4j_fingerprint(driver: Any) -> dict[str, Any]:
    node_rows = driver.execute_query(
        "MATCH (n) WHERE n.corpus_id = $corpus_id "
        "RETURN elementId(n) AS id, labels(n) AS labels, properties(n) AS props "
        "ORDER BY id",
        corpus_id=CORPUS_ID,
    ).records
    relationship_rows = driver.execute_query(
        "MATCH (a)-[r]->(b) "
        "WHERE r.corpus_id = $corpus_id OR a.corpus_id = $corpus_id "
        "OR b.corpus_id = $corpus_id "
        "RETURN elementId(r) AS id, type(r) AS type, elementId(a) AS start, "
        "elementId(b) AS end, properties(r) AS props ORDER BY id",
        corpus_id=CORPUS_ID,
    ).records
    output: dict[str, Any] = {}
    for label, rows in (("nodes", node_rows), ("relationships", relationship_rows)):
        digest = hashlib.sha256()
        for row in rows:
            digest.update(canonical(dict(row)))
            digest.update(b"\n")
        output[label] = {"count": len(rows), "sha256": digest.hexdigest()}
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = get_settings()
    mongo = MongoClient(settings.MONGODB_URI)
    qdrant = QdrantClient(url=settings.QDRANT_URL, timeout=120)
    driver = GraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        stores = {
            "mongo": mongo_fingerprint(mongo[settings.MONGODB_DATABASE]),
            "qdrant": qdrant_fingerprint(qdrant),
            "neo4j": neo4j_fingerprint(driver),
        }
        result = {
            "schema_version": "e2e_corpus_readonly_fingerprint.v1",
            "captured_at_utc": datetime.now(timezone.utc).isoformat(),
            "corpus_id": CORPUS_ID,
            "stores": stores,
            "stores_sha256": hashlib.sha256(canonical(stores)).hexdigest(),
        }
        atomic_write(args.output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        driver.close()
        qdrant.close()
        mongo.close()


if __name__ == "__main__":
    raise SystemExit(main())
