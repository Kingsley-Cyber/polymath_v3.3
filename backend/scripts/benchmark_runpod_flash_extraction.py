#!/usr/bin/env python3
"""Benchmark Runpod Flash extraction without mutating ingestion artifacts.

Dry-run is the default. ``--execute`` loads the encrypted Runpod credential
from Settings, sends a deterministic corpus sample, validates the returned
wire contract, and prints aggregate JSON only. Source text and credentials are
never printed or written to the benchmark result.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import heapq
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from motor.motor_asyncio import AsyncIOMotorClient

from config import get_settings
from services.ghost_b import (
    UNIVERSAL_ENTITY_SCHEMA,
    UNIVERSAL_RELATION_SCHEMA,
    ExtractionTask,
    SchemaContext,
)
from services.ingestion.section_classifier import should_skip_ghost_b
from services.runpod_flash_extraction import extract_entities
from services.settings import settings_service
from services.storage.record_status import with_active_records


def _rank(seed: str, chunk_id: str) -> int:
    digest = hashlib.sha256(f"{seed}\0{chunk_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


async def _sample_tasks(
    db: Any,
    *,
    corpus_id: str,
    limit: int,
    seed: str,
) -> tuple[list[ExtractionTask], int, int]:
    active_docs = await db["documents"].distinct(
        "doc_id", with_active_records({"corpus_id": corpus_id})
    )
    query: dict[str, Any] = {"corpus_id": corpus_id}
    if active_docs:
        query["doc_id"] = {"$in": active_docs}

    # Max-heap by deterministic hash. The scan is stable across retries and
    # does not ask Mongo to random-sort a large collection in memory.
    sample: list[tuple[int, str, dict[str, Any]]] = []
    eligible = 0
    source_bytes = 0
    cursor = db["chunks"].find(
        query,
        {
            "_id": 0,
            "chunk_id": 1,
            "doc_id": 1,
            "corpus_id": 1,
            "text": 1,
            "chunk_kind": 1,
            "columns": 1,
        },
    ).batch_size(2048)
    async for row in cursor:
        chunk_id = str(row.get("chunk_id") or "")
        text = str(row.get("text") or "")
        if (
            not chunk_id
            or not text.strip()
            or should_skip_ghost_b(str(row.get("chunk_kind") or "body"))
        ):
            continue
        eligible += 1
        source_bytes += len(text.encode("utf-8", errors="replace"))
        rank = _rank(seed, chunk_id)
        item = (-rank, chunk_id, row)
        if len(sample) < limit:
            heapq.heappush(sample, item)
        elif rank < -sample[0][0]:
            heapq.heapreplace(sample, item)

    selected = [item[2] for item in sorted(sample, key=lambda value: value[1])]
    tasks = [
        ExtractionTask(
            chunk_id=str(row["chunk_id"]),
            doc_id=str(row.get("doc_id") or ""),
            corpus_id=corpus_id,
            text=str(row.get("text") or ""),
            chunk_kind=str(row.get("chunk_kind") or "body"),
            metadata={"columns": list(row.get("columns") or [])},
        )
        for row in selected
    ]
    return tasks, eligible, source_bytes


def _relation_contract(report: Any) -> tuple[bool, bool]:
    endpoints_valid = True
    evidence_valid = True
    for result in report.results:
        names = {entity.canonical_name for entity in result.entities}
        for relation in result.relations:
            endpoints_valid = endpoints_valid and (
                relation.subject in names and relation.object in names
            )
            evidence_valid = evidence_valid and bool(
                relation.evidence_phrase
                and relation.evidence_phrase in (result.text or "")
            )
    return endpoints_valid, evidence_valid


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = client.get_default_database()
        except Exception:
            db = client[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        config, api_key = await settings_service.get_system_runpod_flash()
        requested = 1 if args.canary else max(1, int(args.limit or config.benchmark_chunks))
        if args.canary:
            canary_text = (
                "The Eiffel Tower is located in Paris and was created by "
                "engineer Gustave Eiffel."
            )
            tasks = [
                ExtractionTask(
                    chunk_id="runpod-flash-canary",
                    doc_id="runpod-flash-canary",
                    corpus_id="runpod-flash-canary",
                    text=canary_text,
                )
            ]
            eligible, source_bytes = 1, len(canary_text.encode("utf-8"))
        else:
            tasks, eligible, source_bytes = await _sample_tasks(
                db,
                corpus_id=args.corpus_id,
                limit=requested,
                seed=args.seed,
            )
        base = {
            "schema_version": "polymath.runpod_flash_benchmark.v1",
            "execute": bool(args.execute),
            "corpus_id": args.corpus_id,
            "requested_sample": requested,
            "sampled_chunks": len(tasks),
            "eligible_chunks": eligible,
            "eligible_source_bytes": source_bytes,
            "endpoint_configured": bool(config.endpoint_id.strip()),
            "engine_enabled": bool(config.enabled),
            "credential_configured": bool(api_key),
            "model": config.model_id,
            "dispatch": {
                "chunks_per_request": config.request_batch_size,
                "in_flight_requests": config.request_concurrency,
                "model_batch_size": config.model_batch_size,
                "max_workers_contract": config.max_workers,
            },
        }
        if not args.execute:
            return base
        if len(tasks) != requested:
            raise RuntimeError(
                f"requested {requested} benchmark chunks but only sampled {len(tasks)}"
            )

        started = time.perf_counter()
        report = await extract_entities(
            tasks,
            schema=SchemaContext(
                entity_schema=list(UNIVERSAL_ENTITY_SCHEMA),
                relation_schema=list(UNIVERSAL_RELATION_SCHEMA),
                strict="soft",
            ),
            runpod_config=config,
            runpod_api_key=api_key,
            return_report=True,
        )
        wall_seconds = time.perf_counter() - started
        processed = len(report.results)
        entity_count = sum(len(result.entities) for result in report.results)
        relation_count = sum(len(result.relations) for result in report.results)
        entity_bearing_chunks = sum(bool(result.entities) for result in report.results)
        relation_bearing_chunks = sum(bool(result.relations) for result in report.results)
        entity_bearing_rate = entity_bearing_chunks / processed if processed else 0.0
        relation_bearing_rate = relation_bearing_chunks / processed if processed else 0.0
        endpoints_valid, evidence_valid = _relation_contract(report)
        sample_cost = float(report.metrics.get("estimated_compute_cost_usd") or 0.0)
        projected_cost = (
            sample_cost * (eligible / processed) if processed else math.inf
        )
        throughput = processed / wall_seconds if wall_seconds else 0.0
        baseline = max(0.0, float(args.baseline_chunks_per_second or 0.0))
        speedup = throughput / baseline if baseline else None
        pass_rate = float(report.metrics.get("schema_evidence_pass_rate") or 0.0)
        acceptance = {
            "processed_equals_submitted": processed == len(tasks),
            "failed_batches_zero": len(report.failures) == 0,
            "schema_evidence_pass_rate_gte_0_999": pass_rate >= 0.999,
            "relations_reference_valid_entities": endpoints_valid,
            "relation_evidence_is_source_substring": evidence_valid,
            "canary_entities_present": entity_count >= 2 if args.canary else None,
            "canary_relation_present": relation_count >= 1 if args.canary else None,
            "entity_bearing_rate_gte_0_95": entity_bearing_rate >= 0.95,
            "relation_bearing_rate_gte_0_10": relation_bearing_rate >= 0.10,
            "projected_cost_within_budget": (
                projected_cost <= config.budget_cap_usd
                if config.budget_cap_usd > 0
                else None
            ),
            "target_speedup_met": (
                speedup >= config.target_speedup if speedup is not None else None
            ),
        }
        return {
            **base,
            "processed_chunks": processed,
            "failed_chunks": len(report.failures),
            "entity_count": entity_count,
            "relation_count": relation_count,
            "entity_bearing_chunk_rate": round(entity_bearing_rate, 4),
            "relation_bearing_chunk_rate": round(relation_bearing_rate, 4),
            "failure_types": sorted({item.error_type for item in report.failures}),
            "wall_seconds": round(wall_seconds, 3),
            "chunks_per_second": round(throughput, 3),
            "baseline_chunks_per_second": baseline or None,
            "measured_speedup": round(speedup, 3) if speedup is not None else None,
            "sample_estimated_cost_usd": round(sample_cost, 6),
            "projected_full_cost_usd": (
                round(projected_cost, 4) if math.isfinite(projected_cost) else None
            ),
            "cost_estimate_only": True,
            "metrics": report.metrics,
            "acceptance": acceptance,
            "accepted": all(value is not False for value in acceptance.values()),
        }
    finally:
        client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-id", default="runpod-flash-canary")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--seed", default="runpod-flash-benchmark-v1")
    parser.add_argument("--baseline-chunks-per-second", type=float, default=0.0)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--canary",
        action="store_true",
        help="Use one fixed ontology canary instead of scanning corpus chunks.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run(parse_args())), indent=2, sort_keys=True))
