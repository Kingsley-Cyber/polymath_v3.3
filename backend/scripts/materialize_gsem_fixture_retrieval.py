#!/usr/bin/env python3
"""Materialize Qdrant + Neo4j for the isolated gsem fixture (fixture-only)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
CORPUS_ID = os.environ.get("GSEM_FIXTURE_CORPUS_ID", "gsem-e2e-20260804a")
OUT_DIR = Path(os.environ.get("GSEM_OUT", "/app/data_eval/knowledge_e2e"))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def main() -> int:
    from motor.motor_asyncio import AsyncIOMotorClient
    from neo4j import AsyncGraphDatabase
    from qdrant_client import AsyncQdrantClient

    from config import get_settings
    from models.schemas import IngestionConfig, SourceTier
    from services import embedder
    from services.ghost_b import EntityItem, ExtractionResult, RelationItem
    from services.graph.neo4j_writer import write_document_graph
    from services.graph.projection_runner import project_document_via_control_plane
    from services.ingestion.fixture_knowledge_pipeline import assert_fixture_safe
    from services.ingestion.section_classifier import ChunkKind
    from services.ingestion.tier_chunker import ChildChunk
    from services.ingestion.worker import _write_qdrant_for_doc
    from services.storage.qdrant_writer import ensure_collections_for_corpus

    def _entity(item: object) -> EntityItem:
        if isinstance(item, EntityItem):
            return item
        d = dict(item or {})
        return EntityItem(
            canonical_name=str(d.get("canonical_name") or d.get("name") or ""),
            surface_form=str(d.get("surface_form") or d.get("surface") or ""),
            entity_type=str(d.get("entity_type") or d.get("type") or "entity"),
            confidence=float(d.get("confidence") or 0.0),
            query_aliases=list(d.get("query_aliases") or []),
        )

    def _relation(item: object) -> RelationItem:
        if isinstance(item, RelationItem):
            return item
        d = dict(item or {})
        return RelationItem(
            subject=str(d.get("subject") or ""),
            predicate=str(d.get("predicate") or ""),
            object=str(d.get("object") or ""),
            object_kind=str(d.get("object_kind") or "entity"),
            confidence=float(d.get("confidence") or 0.0),
            evidence_phrase=str(d.get("evidence_phrase") or d.get("evidence_text") or ""),
            relation_cue=str(d.get("relation_cue") or ""),
            source_predicate=d.get("source_predicate"),
            validation_status=str(d.get("validation_status") or "") or None,
        )

    def _extraction_from_row(row: dict, *, doc_id: str) -> ExtractionResult:
        return ExtractionResult(
            schema_version=str(row.get("schema_version") or "extraction_result.v1"),
            chunk_id=str(row.get("chunk_id") or ""),
            doc_id=doc_id,
            corpus_id=CORPUS_ID,
            text=str(row.get("text") or ""),
            entities=[_entity(e) for e in (row.get("entities") or [])],
            relations=[_relation(r) for r in (row.get("relations") or [])],
            facts=[],
            local_extraction=dict(row.get("local_extraction") or {}),
        )

    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    corpus = await db["corpora"].find_one({"corpus_id": CORPUS_ID})
    assert corpus, f"missing {CORPUS_ID}"
    assert_fixture_safe(corpus)

    dim = int(
        ((corpus.get("default_ingestion_config") or {}).get("embedding_dimension"))
        or 1024
    )
    target_cols = ["naive", "hrag", "graph"]
    await db["corpora"].update_one(
        {"corpus_id": CORPUS_ID},
        {
            "$set": {
                "default_ingestion_config.target_qdrant_collections": target_cols,
                "default_ingestion_config.embedding_dimension": dim,
                "default_ingestion_config.embed_mode": "local",
            }
        },
    )

    qdrant = AsyncQdrantClient(url=settings.QDRANT_URL, timeout=120)
    await ensure_collections_for_corpus(
        qdrant, CORPUS_ID, dim=dim, corpus_name=corpus.get("name")
    )

    docs = await db["documents"].find({"corpus_id": CORPUS_ID}).to_list(100)
    user_id = str(corpus.get("user_id") or corpus.get("owner_id") or "fixture")
    config = IngestionConfig(
        target_qdrant_collections=target_cols,
        embedding_dimension=dim,
    )
    embed_cfg = {"embedding_dimension": dim, "embed_mode": "local"}

    embedded_docs = 0
    vectors_written = 0
    neo4j_docs = 0
    driver = AsyncGraphDatabase.driver(
        settings.NEO4J_URI,
        auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    try:
        for doc in docs:
            doc_id = str(doc["doc_id"])
            chunks = await db["chunks"].find(
                {"corpus_id": CORPUS_ID, "doc_id": doc_id}
            ).to_list(100)
            body_chunks = [
                ch
                for ch in chunks
                if str(ch.get("chunk_kind") or "body") != "parent"
            ]
            if not body_chunks:
                continue

            for ch in body_chunks:
                pid = str(ch.get("parent_id") or f"{doc_id}_p0")
                await db["chunks"].update_one(
                    {"chunk_id": pid, "corpus_id": CORPUS_ID},
                    {
                        "$setOnInsert": {
                            "chunk_id": pid,
                            "doc_id": doc_id,
                            "corpus_id": CORPUS_ID,
                            "parent_id": "",
                            "text": ch.get("text") or "",
                            "chunk_kind": "parent",
                            "summary": (ch.get("text") or "")[:400],
                        }
                    },
                    upsert=True,
                )

            children = [
                ChildChunk(
                    chunk_id=str(ch["chunk_id"]),
                    parent_id=str(ch.get("parent_id") or f"{doc_id}_p0"),
                    doc_id=doc_id,
                    corpus_id=CORPUS_ID,
                    text=str(ch.get("text") or ""),
                    heading_path=[],
                    source_tier=SourceTier.tier_a.value,
                    token_count=max(1, len(str(ch.get("text") or "").split())),
                    chunk_kind=ChunkKind.BODY,
                )
                for ch in body_chunks
            ]
            texts = [c.text for c in children]
            vectors = await embedder.embed_documents(texts, embed_cfg)
            if len(vectors) != len(children):
                raise RuntimeError(
                    f"embed count mismatch doc={doc_id}: {len(vectors)} vs {len(children)}"
                )
            # Drop zero vectors — prove embedder is live.
            if not vectors or not any(vectors[0]):
                raise RuntimeError(f"empty embedding vector for doc={doc_id}")
            vec_map = {c.chunk_id: v for c, v in zip(children, vectors, strict=True)}
            await _write_qdrant_for_doc(
                qdrant_client=qdrant,
                doc_id=doc_id,
                corpus_id=CORPUS_ID,
                user_id=user_id,
                filename=str(doc.get("filename") or doc_id),
                parents=[],
                children=children,
                vec_map=vec_map,
                summaries=None,
                summary_vec_map={},
                config=config,
            )
            embedded_docs += 1
            vectors_written += len(children)

            await db["documents"].update_one(
                {"doc_id": doc_id, "corpus_id": CORPUS_ID},
                {
                    "$set": {
                        "write_state.mongo_written": True,
                        "write_state.qdrant_written": True,
                        "write_state.neo4j_written": False,
                        "ingestion_config.target_qdrant_collections": target_cols,
                        "user_id": user_id,
                        "updated_at": _utcnow(),
                    }
                },
            )

            ghost_rows = await db["ghost_b_extractions"].find(
                {"corpus_id": CORPUS_ID, "doc_id": doc_id}
            ).to_list(100)
            extraction_results = [
                _extraction_from_row(row, doc_id=doc_id) for row in ghost_rows
            ]

            await project_document_via_control_plane(
                db=db,
                neo4j_driver=driver,
                corpus_id=CORPUS_ID,
                doc_id=doc_id,
                write_fn=write_document_graph,
                write_kwargs={
                    "driver": driver,
                    "doc_id": doc_id,
                    "corpus_id": CORPUS_ID,
                    "extraction_results": extraction_results,
                    "user_id": user_id,
                    "file_id": None,
                    "all_chunk_ids": [c.chunk_id for c in children],
                    "filename": str(doc.get("filename") or doc_id),
                    "parent_count": 1,
                    "db": db,
                    "chunk_parent_ids": {c.chunk_id: c.parent_id for c in children},
                },
            )
            await db["documents"].update_one(
                {"doc_id": doc_id, "corpus_id": CORPUS_ID},
                {"$set": {"write_state.neo4j_written": True}},
            )
            neo4j_docs += 1
    finally:
        await driver.close()
        await qdrant.close()

    report = {
        "corpus_id": CORPUS_ID,
        "embedded_docs": embedded_docs,
        "vectors_written": vectors_written,
        "neo4j_docs": neo4j_docs,
        "target_collections": target_cols,
        "finished_at": _utcnow().isoformat(),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "fixture_retrieval_materialization.json").write_text(
        json.dumps(report, indent=2)
    )
    print(json.dumps(report, indent=2))
    client.close()
    return 0 if embedded_docs > 0 and neo4j_docs > 0 else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
