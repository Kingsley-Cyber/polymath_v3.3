"""Background drain for the durable graph relation repair queue."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any

from config import get_settings
from models.schemas import IngestionConfig
from motor.motor_asyncio import AsyncIOMotorDatabase
from qdrant_client import AsyncQdrantClient

from services.ghost_b import (
    EntityItem,
    ExtractionResult,
    ExtractionTask,
    RelationItem,
    SchemaContext,
    _repair_target_relation_with_gemma,
)
from services.graph.entity_quality import mark_graph_metrics_stale
from services.graph.neo4j_writer import write_document_graph
from services.ingestion import repair_queue
from services.ingestion.worker import _build_ghost_pool, _write_graph_vectors_for_doc

logger = logging.getLogger(__name__)


def _relation_from_row(row: dict[str, Any]) -> RelationItem:
    allowed = RelationItem.__dataclass_fields__.keys()
    data = {key: row.get(key) for key in allowed if key in row}
    data.setdefault("subject", str(row.get("subject") or ""))
    data.setdefault("predicate", str(row.get("predicate") or ""))
    data.setdefault("object", str(row.get("object") or ""))
    data.setdefault("object_kind", str(row.get("object_kind") or "entity"))
    data.setdefault("confidence", float(row.get("confidence") or 0.0))
    data.setdefault("source_sentence", str(row.get("source_sentence") or ""))
    data.setdefault("evidence_phrase", str(row.get("evidence_phrase") or data["source_sentence"]))
    data.setdefault("repair_reasons", list(row.get("repair_reasons") or []))
    return RelationItem(**data)


def _entity_from_row(row: dict[str, Any]) -> EntityItem | None:
    if not isinstance(row, dict):
        return None
    name = str(row.get("canonical_name") or row.get("surface_form") or "").strip()
    if not name:
        return None
    return EntityItem(
        canonical_name=name,
        surface_form=str(row.get("surface_form") or name),
        entity_type=str(row.get("entity_type") or row.get("type") or "other"),
        confidence=float(row.get("confidence") or 0.75),
        aliases=list(row.get("aliases") or []),
        description=str(row.get("description") or ""),
    )


def _schema_from_repair(repair: dict[str, Any], config: IngestionConfig) -> SchemaContext:
    snapshot = repair.get("schema_snapshot") or {}
    return SchemaContext(
        entity_schema=snapshot.get("entity_schema") or config.entity_schema,
        relation_schema=snapshot.get("relation_schema") or config.relation_schema,
        strict=str(snapshot.get("strict") or config.schema_strict or "soft"),
    )


async def _effective_config_for_doc(
    db: AsyncIOMotorDatabase,
    *,
    corpus_id: str,
    doc: dict,
) -> IngestionConfig:
    from services.ingestion_service import build_effective_config

    corpus = await db["corpora"].find_one({"corpus_id": corpus_id})
    live_cfg = (corpus or {}).get("default_ingestion_config") or {}
    return build_effective_config(
        frozen_base=doc.get("ingestion_config") or live_cfg,
        live_corpus=live_cfg,
        ingest_overrides=None,
    )


async def process_repair_job(
    *,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver: Any,
    repair: dict,
    owner: str,
) -> bool:
    """Process one leased repair job. Returns True when work was terminal."""
    settings = get_settings()
    corpus_id = str(repair.get("corpus_id") or "")
    doc_id = str(repair.get("doc_id") or "")
    repair_id = str(repair.get("repair_id") or "")
    if not neo4j_driver:
        await repair_queue.mark_repair_failed(
            db,
            repair=repair,
            error="Neo4j driver is unavailable",
            backoff_seconds=300,
        )
        await repair_queue.refresh_document_repair_state(
            db,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        return False

    doc = await db["documents"].find_one({"corpus_id": corpus_id, "doc_id": doc_id})
    if not doc:
        await repair_queue.mark_repair_failed(
            db,
            repair=repair,
            error="Document not found for repair job",
            backoff_seconds=3600,
        )
        return False

    config = await _effective_config_for_doc(db, corpus_id=corpus_id, doc=doc)
    repair_pool = _build_ghost_pool(getattr(config, "extraction_repair_models", None) or [])
    if not repair_pool:
        await repair_queue.mark_repair_failed(
            db,
            repair=repair,
            error="No extraction_repair_models configured",
            backoff_seconds=3600,
        )
        await repair_queue.refresh_document_repair_state(
            db,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        return False

    failed_relation = _relation_from_row(repair.get("failed_triple") or {})
    failed_relation.source_sentence = str(
        repair.get("source_sentence")
        or failed_relation.source_sentence
        or failed_relation.evidence_phrase
        or ""
    )
    failed_relation.evidence_phrase = failed_relation.source_sentence
    reasons = list(repair.get("reasons") or failed_relation.repair_reasons or [])
    entity_names = {str(name) for name in (repair.get("entity_names") or []) if name}
    entities = [
        item
        for item in (_entity_from_row(row) for row in (repair.get("entity_snapshot") or []))
        if item is not None
    ]
    if not entity_names:
        entity_names = {entity.canonical_name for entity in entities}
    schema_ctx = _schema_from_repair(repair, config)
    task = ExtractionTask(
        chunk_id=str(repair.get("chunk_id") or ""),
        doc_id=doc_id,
        corpus_id=corpus_id,
        text="",
        document_title=str(doc.get("filename") or doc_id),
    )
    headers = {
        "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
        "Content-Type": "application/json",
    }
    repair_entry = repair_pool[max(int(repair.get("attempts") or 1) - 1, 0) % len(repair_pool)]
    try:
        repaired = await _repair_target_relation_with_gemma(
            repair_entry=repair_entry,
            task=task,
            relation=failed_relation,
            reasons=reasons,
            entity_names=entity_names,
            schema=schema_ctx,
            settings=settings,
            headers=headers,
        )
        if repaired is None:
            await repair_queue.mark_repair_succeeded(
                db,
                repair_id=repair_id,
                result_relation=None,
                discarded=True,
            )
            await repair_queue.refresh_document_repair_state(
                db,
                corpus_id=corpus_id,
                doc_id=doc_id,
            )
            return True

        result = ExtractionResult(
            schema_version=str(repair.get("schema_version") or "polymath.extract.v2"),
            chunk_id=task.chunk_id,
            doc_id=doc_id,
            corpus_id=corpus_id,
            entities=entities,
            relations=[repaired],
            schema_lens_id=repair.get("schema_lens_id"),
        )
        all_chunk_ids = [
            row["chunk_id"]
            async for row in db["chunks"].find(
                {"doc_id": doc_id, "corpus_id": corpus_id},
                {"chunk_id": 1, "_id": 0},
            )
            if row.get("chunk_id")
        ]
        await _write_graph_vectors_for_doc(
            qdrant_client=qdrant_client,
            corpus_id=corpus_id,
            ghost_b_out=[result],
            config=config,
        )
        await write_document_graph(
            driver=neo4j_driver,
            doc_id=doc_id,
            corpus_id=corpus_id,
            extraction_results=[result],
            user_id=str(doc.get("user_id") or ""),
            file_id=doc.get("file_id"),
            all_chunk_ids=all_chunk_ids,
        )
        await mark_graph_metrics_stale(
            db,
            corpus_id,
            reason="relation_repair_write",
        )
        await repair_queue.mark_repair_succeeded(
            db,
            repair_id=repair_id,
            result_relation=asdict(repaired),
        )
        await repair_queue.refresh_document_repair_state(
            db,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        logger.info(
            "phase=graph_relation_repair status=succeeded owner=%s repair=%s doc=%s corpus=%s",
            owner,
            repair_id[-12:],
            doc_id[:12],
            corpus_id[:8],
        )
        return True
    except Exception as exc:
        attempts = int(repair.get("attempts") or 1)
        await repair_queue.mark_repair_failed(
            db,
            repair=repair,
            error=str(exc),
            backoff_seconds=min(3600, 30 * (2 ** max(attempts - 1, 0))),
        )
        await repair_queue.refresh_document_repair_state(
            db,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        logger.warning(
            "phase=graph_relation_repair status=failed owner=%s repair=%s doc=%s corpus=%s error=%s",
            owner,
            repair_id[-12:],
            doc_id[:12],
            corpus_id[:8],
            exc,
        )
        return False


async def drain_repair_queue_once(
    *,
    db: AsyncIOMotorDatabase,
    qdrant_client: AsyncQdrantClient,
    neo4j_driver: Any,
    owner: str | None = None,
    limit: int = 32,
    corpus_id: str | None = None,
    doc_id: str | None = None,
) -> dict[str, int]:
    settings = get_settings()
    owner = owner or f"repair-drain:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
    processed = 0
    terminal = 0
    failed = 0
    for _ in range(max(1, int(limit or 1))):
        repair = await repair_queue.claim_next_repair(
            db,
            owner=owner,
            lease_seconds=settings.GRAPH_REPAIR_LEASE_SECONDS,
            corpus_id=corpus_id,
            doc_id=doc_id,
        )
        if not repair:
            break
        processed += 1
        if await process_repair_job(
            db=db,
            qdrant_client=qdrant_client,
            neo4j_driver=neo4j_driver,
            repair=repair,
            owner=owner,
        ):
            terminal += 1
        else:
            failed += 1
    return {"processed": processed, "terminal": terminal, "failed": failed}


class GraphRepairWorker:
    def __init__(
        self,
        *,
        db: AsyncIOMotorDatabase,
        qdrant_client: AsyncQdrantClient,
        neo4j_driver: Any,
    ) -> None:
        self._db = db
        self._qdrant = qdrant_client
        self._neo4j = neo4j_driver
        self._settings = get_settings()
        self._owner = f"repair-worker:{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None or not self._settings.GRAPH_REPAIR_WORKER_ENABLED:
            return
        self._task = asyncio.create_task(self._run(), name="graph-repair-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        logger.info(
            "GraphRepairWorker started owner=%s concurrency=%d",
            self._owner,
            self._settings.GRAPH_REPAIR_WORKER_CONCURRENCY,
        )
        while not self._stop.is_set():
            try:
                workers = [
                    drain_repair_queue_once(
                        db=self._db,
                        qdrant_client=self._qdrant,
                        neo4j_driver=self._neo4j,
                        owner=f"{self._owner}:{idx}",
                        limit=1,
                    )
                    for idx in range(self._settings.GRAPH_REPAIR_WORKER_CONCURRENCY)
                ]
                results = await asyncio.gather(*workers, return_exceptions=True)
                processed = sum(
                    int(result.get("processed") or 0)
                    for result in results
                    if isinstance(result, dict)
                )
                if processed == 0:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=self._settings.GRAPH_REPAIR_WORKER_POLL_SECONDS,
                    )
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.warning("GraphRepairWorker loop error: %s", exc)
                await asyncio.sleep(self._settings.GRAPH_REPAIR_WORKER_POLL_SECONDS)
        logger.info("GraphRepairWorker stopped owner=%s", self._owner)
