#!/usr/bin/env python3
"""Book-ingestion pressure baseline (Part 7/8 audit deliverable).

Read-only measurement of the CURRENT pressure and backpressure posture of
the live stack. It does NOT run a 1,000-file ingest — quantities that need
a controlled load run are recorded as "MEASURE_REQUIRED" together with the
exact command to produce them. Never mutates any store.

Measured now:
  * durable queue depths and status histograms (source_parse_jobs,
    extraction_jobs, summary_jobs, graph_promotion_jobs, organ_repair_jobs)
  * control-plane run states, stage_attempts receipts, dead letters
  * store sizes (Mongo chunk/parent/extraction counts, Qdrant collections,
    per-collection point counts for a sample corpus)
  * concurrency configuration read from the running worker container env
  * container memory usage vs configured mem_limit (docker stats)
  * recent stage throughput sampled from stage_attempts durations

Writes data_eval/book_ingestion_pressure_baseline.json.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient
from qdrant_client import QdrantClient

ENV_PATH = Path("/Users/king/polymath_v3.3/.env")
REPORT = Path(
    "/Users/king/polymath_v3.3/data_eval/book_ingestion_pressure_baseline.json"
)
MEASURE_REQUIRED = "MEASURE_REQUIRED"

QUEUE_COLLECTIONS = (
    "source_parse_jobs",
    "extraction_jobs",
    "summary_jobs",
    "graph_promotion_jobs",
    "organ_repair_jobs",
    "document_pipeline_jobs",
)

WORKER_ENV_KEYS = (
    "INGEST_MAX_PARSE_JOBS",
    "INGEST_MAX_ACTIVE_JOBS",
    "INGEST_MAX_MODEL_PHASE_DOCS",
    "INGEST_RSS_SOFT_LIMIT_RATIO",
    "QDRANT_INGEST_WRITE_CONCURRENCY",
    "QDRANT_UPSERT_BATCH_SIZE",
    "NEO4J_INGEST_WRITE_CONCURRENCY",
    "EMBED_BATCH_SIZE",
    "EXTRACTION_MAX_CONCURRENT",
    "SUMMARY_MAX_CONCURRENT",
    "INGEST_PROVIDER_MICROBATCH_SIZE",
)


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV_PATH.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def hist(db, coll: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for row in db[coll].aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
        out[str(row["_id"])] = row["n"]
    return out


def queue_depths(db) -> dict:
    return {coll: hist(db, coll) for coll in QUEUE_COLLECTIONS}


def control_plane(db) -> dict:
    out: dict = {}
    out["ingestion_runs"] = hist(db, "ingestion_runs")
    out["stage_attempts_total"] = db.stage_attempts.count_documents({})
    out["dead_letters"] = {
        coll: db[coll].count_documents({"status": "dead_letter"})
        for coll in QUEUE_COLLECTIONS
    }
    # Recent stage latency sample: last 200 execute receipts with durations.
    rows = list(
        db.stage_attempts.find(
            {"duration_ms": {"$exists": True}},
            {"stage": 1, "duration_ms": 1, "status": 1},
        )
        .sort("created_at", -1)
        .limit(500)
    )
    by_stage: dict[str, list[int]] = {}
    for r in rows:
        by_stage.setdefault(str(r.get("stage")), []).append(int(r.get("duration_ms") or 0))
    latency: dict[str, dict] = {}
    for stage, vals in by_stage.items():
        vals.sort()
        latency[stage] = {
            "n": len(vals),
            "p50_ms": vals[len(vals) // 2],
            "p95_ms": vals[int(len(vals) * 0.95)] if len(vals) > 1 else vals[0],
            "max_ms": vals[-1],
        }
    out["recent_stage_latency_ms"] = latency
    return out


def mongo_load(db) -> dict:
    return {
        "documents": db.documents.count_documents({}),
        "chunks": db.chunks.count_documents({}),
        "parent_chunks": db.parent_chunks.count_documents({}),
        "ghost_b_extractions": db.ghost_b_extractions.count_documents({}),
        "ghost_b_staging": db.ghost_b_staging.count_documents({}),
        "summary_tree_nodes": db.summary_tree.count_documents({}),
    }


def qdrant_load() -> dict:
    client = QdrantClient(url="http://localhost:6333")
    cols = [c.name for c in client.get_collections().collections]
    out: dict = {"collections": len(cols), "sample": {}}
    for name in sorted(cols)[:12]:
        try:
            info = client.get_collection(name)
            out["sample"][name] = info.points_count
        except Exception:  # noqa: BLE001
            out["sample"][name] = None
    return out


def worker_env() -> dict:
    try:
        raw = subprocess.run(
            ["docker", "inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}",
             "polymath_v33-ingest-worker-1"],
            capture_output=True, text=True, timeout=30,
        ).stdout.splitlines()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    envmap = dict(line.split("=", 1) for line in raw if "=" in line)
    return {k: envmap.get(k) for k in WORKER_ENV_KEYS}


def container_pressure() -> list[dict]:
    try:
        raw = subprocess.run(
            ["docker", "stats", "--no-stream",
             "--format", "{{.Name}}|{{.MemUsage}}|{{.MemPerc}}|{{.CPUPerc}}"],
            capture_output=True, text=True, timeout=60,
        ).stdout.splitlines()
    except Exception as exc:  # noqa: BLE001
        return [{"error": str(exc)}]
    rows = []
    for line in raw:
        name, mem, mempct, cpu = (line.split("|") + ["", "", ""])[:4]
        rows.append(
            {"container": name, "mem_usage": mem, "mem_pct": mempct, "cpu_pct": cpu}
        )
    return sorted(rows, key=lambda r: r.get("container", ""))


def main() -> int:
    envcfg = env()
    pw = envcfg["MONGO_PASSWORD"]
    db = MongoClient(
        f"mongodb://polymath:{pw}@localhost:27017/polymath?authSource=admin"
    ).get_database()

    report = {
        "schema_version": "book_ingestion_pressure_baseline.v1",
        "run_at": datetime.now(timezone.utc).isoformat(),
        "queue_depths": queue_depths(db),
        "control_plane": control_plane(db),
        "mongo_load": mongo_load(db),
        "qdrant_load": qdrant_load(),
        "worker_concurrency_env": worker_env(),
        "container_pressure": container_pressure(),
        "unmeasured_quantities": {
            "files_per_hour_1k_load": MEASURE_REQUIRED,
            "mb_per_hour_1k_load": MEASURE_REQUIRED,
            "chunks_per_second": MEASURE_REQUIRED,
            "relex_chunks_per_second": MEASURE_REQUIRED,
            "embedding_points_per_second": MEASURE_REQUIRED,
            "summary_records_per_second": MEASURE_REQUIRED,
            "mongo_writes_per_second": MEASURE_REQUIRED,
            "qdrant_writes_per_second": MEASURE_REQUIRED,
            "neo4j_writes_per_second": MEASURE_REQUIRED,
            "peak_ram_1k_load": MEASURE_REQUIRED,
            "peak_device_memory": MEASURE_REQUIRED,
            "temp_disk_growth": MEASURE_REQUIRED,
            "qdrant_disk_growth": MEASURE_REQUIRED,
            "time_to_query_ready_per_doc": MEASURE_REQUIRED,
            "how_to_measure": (
                "Run backend/scripts/benchmark_book_ingestion_pressure.py "
                "--files 1|10|100|1000 against a dedicated benchmark corpus; "
                "the controlled load run is intentionally not executed by the "
                "read-only audit probe."
            ),
        },
    }
    REPORT.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "queue_depths": report["queue_depths"],
                "control_plane_runs": report["control_plane"]["ingestion_runs"],
                "worker_env": report["worker_concurrency_env"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
