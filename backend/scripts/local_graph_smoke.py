"""Smoke test the optional GLiNER local graph extractor.

Run inside the backend container built with local graph extras:

    python scripts/local_graph_smoke.py --device cuda:0

The script intentionally avoids Mongo/Qdrant/Neo4j. It only proves that the
runtime can import GLiNER/Torch, load the configured model, and produce the
same ExtractionBatchReport shape used by the ingestion worker.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from models.schemas import IngestionConfig  # noqa: E402
from services.ghost_b import (  # noqa: E402
    ExtractionBatchReport,
    ExtractionTask,
    SchemaContext,
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
)
from services.local_graph_extractor import extract_entities_local_first  # noqa: E402


def _cuda_report() -> dict:
    try:
        import torch  # type: ignore

        return {
            "torch_imported": True,
            "cuda_available": bool(torch.cuda.is_available()),
            "cuda_device_count": int(torch.cuda.device_count()) if torch.cuda.is_available() else 0,
            "cuda_devices": [
                torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())
            ]
            if torch.cuda.is_available()
            else [],
        }
    except Exception as exc:
        return {
            "torch_imported": False,
            "cuda_available": False,
            "cuda_device_count": 0,
            "cuda_devices": [],
            "torch_error": str(exc),
        }


async def _run(args: argparse.Namespace) -> int:
    started = perf_counter()
    config = IngestionConfig(
        graph_extraction_engine="local_gliner",
        llm_fallback_enabled=False,
        local_extractor_model=args.model,
        local_workers=[
            {
                "device": args.device,
                "name": args.device.replace(":", "_"),
                "batch_size": args.batch_size,
                "weight": 1,
            }
        ],
        max_chunk_tokens_for_local_extractor=args.max_tokens,
    )
    schema = SchemaContext(
        entity_schema=UNIVERSAL_ENTITY_SCHEMA,
        relation_schema=UNIVERSAL_RELATION_SCHEMA,
        strict="soft",
    )
    tasks = [
        ExtractionTask(
            chunk_id="smoke-1",
            doc_id="local-graph-smoke",
            corpus_id="smoke",
            document_title="Local Graph Smoke",
            heading_path=["Android ML Deployment"],
            chunk_kind="body",
            text="TensorFlow Lite runs on Android and ML Kit supports object detection.",
        ),
        ExtractionTask(
            chunk_id="smoke-2",
            doc_id="local-graph-smoke",
            corpus_id="smoke",
            document_title="Local Graph Smoke",
            heading_path=["Model Runtime"],
            chunk_kind="body",
            text="The mobile app uses CameraX and stores prediction results in SQLite.",
        ),
    ]
    report = await extract_entities_local_first(
        tasks,
        config=config,
        schema=schema,
        llm_kwargs={"return_report": True},
        return_report=True,
    )
    assert isinstance(report, ExtractionBatchReport)
    payload = {
        "ok": bool(report.results) and not report.failures,
        "duration_seconds": round(perf_counter() - started, 3),
        "cuda": _cuda_report(),
        "metrics": report.metrics,
        "results": [
            {
                "chunk_id": result.chunk_id,
                "entities": [
                    {
                        "name": entity.canonical_name,
                        "type": entity.entity_type,
                        "confidence": entity.confidence,
                    }
                    for entity in result.entities[:10]
                ],
                "relations": [
                    {
                        "subject": relation.subject,
                        "predicate": relation.predicate,
                        "object": relation.object,
                        "confidence": relation.confidence,
                    }
                    for relation in result.relations[:10]
                ],
            }
            for result in report.results
        ],
        "failures": [
            {
                "chunk_id": failure.chunk_id,
                "error_type": failure.error_type,
                "error_message": failure.error_message,
            }
            for failure in report.failures
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["ok"] or args.allow_unavailable else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test local GLiNER graph extraction.")
    parser.add_argument("--model", default="knowledgator/gliner-relex-large-v0.5")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=768)
    parser.add_argument(
        "--allow-unavailable",
        action="store_true",
        help="Exit 0 even if optional GLiNER dependencies/model weights are unavailable.",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
