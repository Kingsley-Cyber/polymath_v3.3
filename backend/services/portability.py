"""Browser-driven runtime portability.

Creates/restores a logical archive over the live services so Settings can offer
Download / Upload buttons. This intentionally does not raw-copy database files:
Mongo, Qdrant, and Neo4j remain running and are accessed through their normal
clients.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from bson import json_util
from qdrant_client.models import PointStruct

from services.ingestion_service import ingestion_service
from services.storage.qdrant_writer import (
    _col_for_corpus,
    ensure_collections_for_corpus,
)

QDRANT_KINDS = ("naive", "hrag", "graph", "schemas")

MONGO_COLLECTIONS = (
    "corpora",
    "documents",
    "chunks",
    "settings",
    "model_pool",
    "model_profiles",
    "user_query_preferences",
    "tools",
    "skills",
    "conversations",
    "messages",
    "graph_sessions",
    "graph_domain_cache",
    "graph_metrics_cache",
    "graph_anchor_cache",
)


def _json_ready(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(v) for v in value]
    return value


def _bson_to_jsonable(docs: list[dict[str, Any]]) -> Any:
    return json.loads(json_util.dumps(docs))


def _jsonable_to_bson(value: Any) -> Any:
    return json_util.loads(json.dumps(value))


def _write_json(zf: zipfile.ZipFile, name: str, value: Any) -> None:
    zf.writestr(name, json.dumps(_json_ready(value), ensure_ascii=False, indent=2))


def _strip_id(doc: dict[str, Any]) -> dict[str, Any]:
    doc = dict(doc)
    doc.pop("_id", None)
    return doc


async def _owned_corpus_ids(db: Any, user_id: str) -> list[str]:
    cursor = db["corpora"].find({"user_id": user_id}, {"corpus_id": 1, "_id": 0})
    ids = [row["corpus_id"] async for row in cursor]
    if ids:
        return ids
    # Single-user legacy fallback: older local corpora may not carry user_id.
    cursor = db["corpora"].find({}, {"corpus_id": 1, "_id": 0})
    return [row["corpus_id"] async for row in cursor]


def _mongo_query(collection: str, user_id: str, corpus_ids: list[str]) -> dict[str, Any]:
    if collection in {"corpora", "documents", "chunks", "graph_sessions"}:
        return {"corpus_id": {"$in": corpus_ids}}
    if collection in {"settings", "model_pool", "model_profiles", "user_query_preferences"}:
        return {"user_id": user_id}
    if collection in {"graph_domain_cache", "graph_metrics_cache", "graph_anchor_cache"}:
        return {"corpus_id": {"$in": corpus_ids}}
    # tools/skills/conversations/messages are legacy global collections in this
    # app, so include them whole. This preserves the single-user desktop flow.
    return {}


async def export_portability_archive(user_id: str) -> tuple[Path, dict[str, Any]]:
    db = ingestion_service.db
    qdrant = ingestion_service.qdrant_client
    if db is None:
        raise RuntimeError("MongoDB is not connected")
    if qdrant is None:
        raise RuntimeError("Qdrant is not connected")

    corpus_ids = await _owned_corpus_ids(db, user_id)
    created_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    tmp = tempfile.NamedTemporaryFile(prefix="polymath-portability-", suffix=".zip", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    stats: dict[str, Any] = {
        "corpora": len(corpus_ids),
        "mongo_documents": {},
        "qdrant_points": {},
        "neo4j_nodes": 0,
        "neo4j_relationships": 0,
    }

    with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        manifest = {
            "format": "polymath-portability-v1",
            "created_at": created_at,
            "corpus_ids": corpus_ids,
            "notes": [
                "Logical browser export generated from Mongo, Qdrant, and Neo4j.",
                "Import merges/upserts records into the selected account.",
                "Use the same embedding model and dimension after import.",
            ],
        }
        _write_json(zf, "manifest.json", manifest)

        for collection in MONGO_COLLECTIONS:
            query = _mongo_query(collection, user_id, corpus_ids)
            docs = await db[collection].find(query).to_list(length=None)
            stats["mongo_documents"][collection] = len(docs)
            _write_json(zf, f"mongo/{collection}.json", _bson_to_jsonable(docs))

        for corpus_id in corpus_ids:
            for kind in QDRANT_KINDS:
                collection_name = _col_for_corpus(corpus_id, kind)
                try:
                    exists = await qdrant.collection_exists(collection_name)
                except Exception:
                    exists = False
                if not exists:
                    continue
                points: list[dict[str, Any]] = []
                offset = None
                while True:
                    records, offset = await qdrant.scroll(
                        collection_name=collection_name,
                        limit=256,
                        offset=offset,
                        with_payload=True,
                        with_vectors=True,
                    )
                    points.extend(
                        {
                            "id": r.id,
                            "payload": r.payload or {},
                            "vector": _json_ready(r.vector),
                        }
                        for r in records
                    )
                    if offset is None:
                        break
                stats["qdrant_points"][collection_name] = len(points)
                _write_json(
                    zf,
                    f"qdrant/{collection_name}.json",
                    {
                        "corpus_id": corpus_id,
                        "kind": kind,
                        "collection_name": collection_name,
                        "points": points,
                    },
                )

        neo4j = ingestion_service.neo4j_driver
        graph_payload = {"nodes": [], "relationships": []}
        if neo4j is not None and corpus_ids:
            async with neo4j.session() as session:
                nodes_result = await session.run(
                    """
                    MATCH (n)
                    WHERE n.corpus_id IN $corpus_ids
                       OR (
                         n:Entity AND EXISTS {
                           MATCH (n)<-[:MENTIONS]-(c:Chunk)
                           WHERE c.corpus_id IN $corpus_ids
                         }
                       )
                    RETURN labels(n) AS labels, properties(n) AS props
                    """,
                    corpus_ids=corpus_ids,
                )
                graph_payload["nodes"] = [record.data() async for record in nodes_result]

                rels_result = await session.run(
                    """
                    MATCH (a)-[r]->(b)
                    WHERE a.corpus_id IN $corpus_ids
                       OR b.corpus_id IN $corpus_ids
                       OR any(cid IN coalesce(r.corpus_ids, []) WHERE cid IN $corpus_ids)
                    RETURN labels(a) AS start_labels,
                           properties(a) AS start_props,
                           type(r) AS type,
                           properties(r) AS props,
                           labels(b) AS end_labels,
                           properties(b) AS end_props
                    """,
                    corpus_ids=corpus_ids,
                )
                graph_payload["relationships"] = [
                    record.data() async for record in rels_result
                ]
        stats["neo4j_nodes"] = len(graph_payload["nodes"])
        stats["neo4j_relationships"] = len(graph_payload["relationships"])
        _write_json(zf, "neo4j/graph.json", graph_payload)
        _write_json(zf, "stats.json", stats)

    return tmp_path, stats


async def import_portability_archive(archive_path: Path, user_id: str) -> dict[str, Any]:
    db = ingestion_service.db
    qdrant = ingestion_service.qdrant_client
    if db is None:
        raise RuntimeError("MongoDB is not connected")
    if qdrant is None:
        raise RuntimeError("Qdrant is not connected")

    stats: dict[str, Any] = {
        "mongo_documents": {},
        "qdrant_points": {},
        "neo4j_nodes": 0,
        "neo4j_relationships": 0,
    }

    with zipfile.ZipFile(archive_path, mode="r") as zf:
        manifest = json.loads(zf.read("manifest.json"))
        if manifest.get("format") != "polymath-portability-v1":
            raise ValueError("Unsupported Polymath archive format")
        corpus_ids = list(manifest.get("corpus_ids") or [])

        for collection in MONGO_COLLECTIONS:
            name = f"mongo/{collection}.json"
            if name not in zf.namelist():
                continue
            docs = _jsonable_to_bson(json.loads(zf.read(name)))
            imported = 0
            for raw in docs:
                doc = dict(raw)
                if collection == "corpora":
                    doc = _strip_id(doc)
                    doc["user_id"] = user_id
                    await db[collection].replace_one(
                        {"corpus_id": doc["corpus_id"]}, doc, upsert=True
                    )
                elif collection in {"documents", "chunks"}:
                    doc = _strip_id(doc)
                    doc["user_id"] = user_id
                    await db[collection].replace_one(
                        {"corpus_id": doc["corpus_id"], "chunk_id": doc.get("chunk_id")}
                        if collection == "chunks"
                        else {"corpus_id": doc["corpus_id"], "doc_id": doc["doc_id"]},
                        doc,
                        upsert=True,
                    )
                elif collection in {"settings", "user_query_preferences"}:
                    doc = _strip_id(doc)
                    doc["user_id"] = user_id
                    await db[collection].replace_one({"user_id": user_id}, doc, upsert=True)
                elif collection in {"model_pool", "model_profiles"}:
                    doc = _strip_id(doc)
                    doc["user_id"] = user_id
                    if doc.get("entry_id"):
                        key = {"user_id": user_id, "entry_id": doc["entry_id"]}
                    elif doc.get("profile_id"):
                        key = {"user_id": user_id, "profile_id": doc["profile_id"]}
                    else:
                        key = {"user_id": user_id, "label": doc.get("label")}
                    await db[collection].replace_one(key, doc, upsert=True)
                elif collection in {"graph_domain_cache", "graph_metrics_cache", "graph_anchor_cache", "graph_sessions"}:
                    doc = _strip_id(doc)
                    if "user_id" in doc:
                        doc["user_id"] = user_id
                    key = {
                        k: doc[k]
                        for k in ("corpus_id", "session_id", "signature", "cache_key")
                        if k in doc
                    }
                    await db[collection].replace_one(key or doc, doc, upsert=True)
                else:
                    if "_id" in doc:
                        await db[collection].replace_one({"_id": doc["_id"]}, doc, upsert=True)
                    else:
                        await db[collection].insert_one(doc)
                imported += 1
            stats["mongo_documents"][collection] = imported

        corpus_meta = {
            doc["corpus_id"]: doc
            for doc in await db["corpora"].find(
                {"corpus_id": {"$in": corpus_ids}},
                {"corpus_id": 1, "name": 1, "default_ingestion_config": 1, "_id": 0},
            ).to_list(length=None)
        }

        for name in zf.namelist():
            if not name.startswith("qdrant/") or not name.endswith(".json"):
                continue
            payload = json.loads(zf.read(name))
            corpus_id = payload["corpus_id"]
            collection_name = payload["collection_name"]
            points = payload.get("points") or []
            cfg = corpus_meta.get(corpus_id, {}).get("default_ingestion_config") or {}
            dim = int(cfg.get("embedding_dimension") or 1024)
            await ensure_collections_for_corpus(
                qdrant,
                corpus_id,
                dim=dim,
                corpus_name=corpus_meta.get(corpus_id, {}).get("name"),
            )
            batch: list[PointStruct] = []
            imported = 0
            for p in points:
                batch.append(
                    PointStruct(
                        id=p["id"],
                        vector=p.get("vector"),
                        payload=p.get("payload") or {},
                    )
                )
                if len(batch) >= 128:
                    await qdrant.upsert(collection_name=collection_name, points=batch)
                    imported += len(batch)
                    batch = []
            if batch:
                await qdrant.upsert(collection_name=collection_name, points=batch)
                imported += len(batch)
            stats["qdrant_points"][collection_name] = imported

        if "neo4j/graph.json" in zf.namelist() and ingestion_service.neo4j_driver is not None:
            graph = json.loads(zf.read("neo4j/graph.json"))
            stats.update(await _import_neo4j_graph(graph, user_id))

    return stats


def _node_id(labels: list[str], props: dict[str, Any]) -> tuple[str, str] | None:
    if "Entity" in labels and props.get("entity_id"):
        return "Entity", props["entity_id"]
    if "Chunk" in labels and props.get("chunk_id"):
        return "Chunk", props["chunk_id"]
    if "Document" in labels and props.get("doc_id"):
        return "Document", props["doc_id"]
    if "Fact" in labels and props.get("fact_id"):
        return "Fact", props["fact_id"]
    return None


async def _import_neo4j_graph(graph: dict[str, Any], user_id: str) -> dict[str, int]:
    driver = ingestion_service.neo4j_driver
    if driver is None:
        return {"neo4j_nodes": 0, "neo4j_relationships": 0}
    nodes = graph.get("nodes") or []
    rels = graph.get("relationships") or []
    rows_by_label: dict[str, list[dict[str, Any]]] = {
        "Document": [],
        "Chunk": [],
        "Entity": [],
        "Fact": [],
    }
    for node in nodes:
        labels = node.get("labels") or []
        props = dict(node.get("props") or {})
        if "user_id" in props:
            props["user_id"] = user_id
        node_key = _node_id(labels, props)
        if node_key:
            label, value = node_key
            rows_by_label[label].append({"id": value, "props": props})

    relationship_rows: dict[str, list[dict[str, Any]]] = {
        "HAS_CHUNK": [],
        "MENTIONS": [],
        "RELATES_TO": [],
        "HAS_FACT": [],
        "SUPPORTS_FACT": [],
    }
    for rel in rels:
        rel_type = rel.get("type")
        if rel_type not in relationship_rows:
            continue
        start = _node_id(rel.get("start_labels") or [], rel.get("start_props") or {})
        end = _node_id(rel.get("end_labels") or [], rel.get("end_props") or {})
        if not start or not end:
            continue
        relationship_rows[rel_type].append(
            {
                "start": start[1],
                "end": end[1],
                "props": rel.get("props") or {},
            }
        )

    async with driver.session() as session:
        if rows_by_label["Document"]:
            await session.run(
                """
                UNWIND $rows AS row
                MERGE (n:Document {doc_id: row.id})
                SET n += row.props
                """,
                rows=rows_by_label["Document"],
            )
        if rows_by_label["Chunk"]:
            await session.run(
                """
                UNWIND $rows AS row
                MERGE (n:Chunk {chunk_id: row.id})
                SET n += row.props
                """,
                rows=rows_by_label["Chunk"],
            )
        if rows_by_label["Entity"]:
            await session.run(
                """
                UNWIND $rows AS row
                MERGE (n:Entity {entity_id: row.id})
                SET n += row.props
                """,
                rows=rows_by_label["Entity"],
            )
        if rows_by_label["Fact"]:
            await session.run(
                """
                UNWIND $rows AS row
                MERGE (n:Fact {fact_id: row.id})
                SET n += row.props
                """,
                rows=rows_by_label["Fact"],
            )
        if relationship_rows["HAS_CHUNK"]:
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (a:Document {doc_id: row.start})
                MATCH (b:Chunk {chunk_id: row.end})
                MERGE (a)-[r:HAS_CHUNK]->(b)
                SET r += row.props
                """,
                rows=relationship_rows["HAS_CHUNK"],
            )
        if relationship_rows["MENTIONS"]:
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (a:Chunk {chunk_id: row.start})
                MATCH (b:Entity {entity_id: row.end})
                MERGE (a)-[r:MENTIONS]->(b)
                SET r += row.props
                """,
                rows=relationship_rows["MENTIONS"],
            )
        if relationship_rows["RELATES_TO"]:
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (a:Entity {entity_id: row.start})
                MATCH (b:Entity {entity_id: row.end})
                MERGE (a)-[r:RELATES_TO {predicate: coalesce(row.props.predicate, 'related_to')}]->(b)
                SET r += row.props
                """,
                rows=relationship_rows["RELATES_TO"],
            )
        if relationship_rows["HAS_FACT"]:
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (a:Entity {entity_id: row.start})
                MATCH (b:Fact {fact_id: row.end})
                MERGE (a)-[r:HAS_FACT]->(b)
                SET r += row.props
                """,
                rows=relationship_rows["HAS_FACT"],
            )
        if relationship_rows["SUPPORTS_FACT"]:
            await session.run(
                """
                UNWIND $rows AS row
                MATCH (a:Chunk {chunk_id: row.start})
                MATCH (b:Fact {fact_id: row.end})
                MERGE (a)-[r:SUPPORTS_FACT]->(b)
                SET r += row.props
                """,
                rows=relationship_rows["SUPPORTS_FACT"],
            )

    return {
        "neo4j_nodes": sum(len(v) for v in rows_by_label.values()),
        "neo4j_relationships": sum(len(v) for v in relationship_rows.values()),
    }
