"""Replay local graph extraction on selected chunks for CUDA diagnostics.

Usage from the backend container:
  python scripts/local_graph_replay.py --corpus-id <id> --doc-id <id> --top-longest 50 --dry-run

This tool never calls the graph LLM. It is meant to validate local GLiNER /
GLiNER2 stability on the chunks most likely to trigger tokenizer/CUDA issues.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from config import get_settings
from motor.motor_asyncio import AsyncIOMotorClient

from models.schemas import IngestionConfig
from services.ghost_b import ExtractionBatchReport, ExtractionTask, SchemaContext
from services.ingestion_service import build_effective_config
from services.local_graph_extractor import extract_entities_local_first


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay local GraphRAG extraction.")
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--doc-id", required=True)
    parser.add_argument("--top-longest", type=int, default=50)
    parser.add_argument("--failed-only", action="store_true")
    parser.add_argument("--chunk-id", action="append", default=[])
    parser.add_argument("--device", default=None)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="No Neo4j writes are performed. Always true for this tool.")
    parser.add_argument("--diagnostics-dir", default=None)
    return parser.parse_args()


async def _load_config(db: Any, corpus_id: str, doc: dict[str, Any]) -> IngestionConfig:
    corpus = await db["corpora"].find_one({"corpus_id": corpus_id})
    live_cfg = (corpus or {}).get("default_ingestion_config") or {}
    return build_effective_config(
        frozen_base=doc.get("ingestion_config") or live_cfg,
        live_corpus=live_cfg,
        ingest_overrides=None,
    )


async def _main() -> None:
    args = _parse_args()
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client[settings.MONGODB_DATABASE]
    doc = await db["documents"].find_one({"corpus_id": args.corpus_id, "doc_id": args.doc_id})
    if not doc:
        raise SystemExit("Document not found")
    config = await _load_config(db, args.corpus_id, doc)
    config.graph_extraction_engine = config.graph_extraction_engine or "local_gliner"
    config.llm_fallback_enabled = False
    config.llm_fallback_max_percent = 0
    config.local_graph_diagnostics_enabled = True
    if args.diagnostics_dir:
        config.local_graph_diagnostics_dir = args.diagnostics_dir
    if args.device:
        config.local_workers = [{"device": args.device, "name": args.device, "batch_size": args.batch_size, "weight": 1}]
    else:
        config.local_workers = [
            {**row, "batch_size": args.batch_size}
            for row in (config.local_workers or [{"device": "cpu", "name": "cpu", "batch_size": args.batch_size, "weight": 1}])
        ]

    chunk_filter: dict[str, Any] = {"corpus_id": args.corpus_id, "doc_id": args.doc_id}
    if args.chunk_id:
        chunk_filter["chunk_id"] = {"$in": args.chunk_id}
    elif args.failed_only:
        failed_ids = [
            row.get("chunk_id")
            for row in (doc.get("ghost_b_failures") or [])
            if row.get("chunk_id")
        ]
        chunk_filter["chunk_id"] = {"$in": failed_ids}

    rows = await db["chunks"].find(
        chunk_filter,
        {"chunk_id": 1, "text": 1, "heading_path": 1, "chunk_kind": 1, "token_count": 1, "_id": 0},
    ).sort("token_count", -1).limit(max(1, args.top_longest)).to_list(length=None)
    tasks = [
        ExtractionTask(
            chunk_id=row["chunk_id"],
            doc_id=args.doc_id,
            corpus_id=args.corpus_id,
            text=str(row.get("text") or ""),
            document_title=str(doc.get("filename") or args.doc_id),
            heading_path=row.get("heading_path"),
            chunk_kind=row.get("chunk_kind") or "body",
        )
        for row in rows
    ]
    schema = SchemaContext(
        entity_schema=config.entity_schema,
        relation_schema=config.relation_schema,
        strict=config.schema_strict,
    )
    report = await extract_entities_local_first(
        tasks,
        config=config,
        schema=schema,
        schema_lens=doc.get("schema_lens") or (doc.get("ghost_b_metrics") or {}).get("schema_lens"),
        return_report=True,
    )
    if not isinstance(report, ExtractionBatchReport):
        raise SystemExit("Unexpected non-report local extraction result")
    print(
        {
            "selected_chunks": len(tasks),
            "results": len(report.results),
            "failures": len(report.failures),
            "metrics": report.metrics,
        }
    )


if __name__ == "__main__":
    asyncio.run(_main())
