"""Ingestion pressure/readiness signals.

This is a read-model layer for the control plane. The worker still owns the
hard semaphores; this module makes the current pressure and recommended
backpressure visible to APIs/UI/repair cycles.
"""

from __future__ import annotations

from typing import Any


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ratio(value: int | None, total: int | None) -> float | None:
    if not value or not total or total <= 0:
        return None
    return round(max(0.0, value / total), 4)


def _float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else None


def _pressure_level(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"critical", "stop", "blocked", "high", "red"}:
        return "high"
    if text in {"warn", "warning", "elevated", "yellow", "degraded"}:
        return "elevated"
    return "normal"


def _writer_pressure_snapshot(
    payload: dict[str, Any] | None,
    *,
    default_latency_warn_ms: float,
    default_latency_stop_ms: float,
    default_queue_warn: int,
    default_queue_stop: int,
) -> dict[str, Any]:
    data = payload or {}
    level = _pressure_level(data.get("status") or data.get("level"))
    reasons: list[str] = []
    latency_ms = _float_or_none(data.get("write_latency_ms") or data.get("latency_ms"))
    latency_warn_ms = _float_or_none(data.get("write_latency_warn_ms")) or default_latency_warn_ms
    latency_stop_ms = _float_or_none(data.get("write_latency_stop_ms")) or default_latency_stop_ms
    queue_depth = _int(data.get("queue_depth") or data.get("pending_writes"))
    queue_warn = _int(data.get("queue_warn") or default_queue_warn)
    queue_stop = _int(data.get("queue_stop") or default_queue_stop)

    if latency_ms is not None:
        if latency_stop_ms > 0 and latency_ms >= latency_stop_ms:
            level = "high"
            reasons.append("write_latency_over_stop_limit")
        elif (
            level != "high"
            and latency_warn_ms > 0
            and latency_ms >= latency_warn_ms
        ):
            level = "elevated"
            reasons.append("write_latency_near_stop_limit")

    if queue_depth:
        if queue_stop > 0 and queue_depth >= queue_stop:
            level = "high"
            reasons.append("write_queue_over_stop_limit")
        elif level != "high" and queue_warn > 0 and queue_depth >= queue_warn:
            level = "elevated"
            reasons.append("write_queue_near_stop_limit")

    explicit_reason = data.get("reason") or data.get("reasons")
    if isinstance(explicit_reason, str) and explicit_reason:
        reasons.append(explicit_reason)
    elif isinstance(explicit_reason, list):
        reasons.extend(str(item) for item in explicit_reason if str(item).strip())

    snapshot = {
        "status": level,
        "reasons": list(dict.fromkeys(reasons)),
        "write_latency_ms": latency_ms,
        "write_latency_warn_ms": latency_warn_ms,
        "write_latency_stop_ms": latency_stop_ms,
        "queue_depth": queue_depth,
        "queue_warn": queue_warn,
        "queue_stop": queue_stop,
    }
    for key in (
        "source",
        "max_queue_depth",
        "deferred_points",
        "collections_total",
        "vectors_total",
        "memory_resident_bytes",
        "memory_allocated_bytes",
        "memory_active_bytes",
        "memory_retained_bytes",
        "memory_limit_bytes",
        "memory_pressure",
        "memory_warn_ratio",
        "memory_stop_ratio",
        "sample_size",
        "latest_write_latency_ms",
        "max_write_latency_ms",
        "avg_write_latency_ms",
    ):
        if key in data:
            snapshot[key] = data.get(key)
    return snapshot


def build_ingestion_pressure_snapshot(
    *,
    backend_rss_mb: int | None = None,
    ram_cap_mb: int | None = None,
    rss_soft_limit_mb: int | None = None,
    active_repairs: int = 0,
    graph_jobs: dict[str, int] | None = None,
    extraction_jobs: dict[str, int] | None = None,
    summary_missing: int = 0,
    mongo_stats: dict[str, Any] | None = None,
    mongo_storage_warn_ratio: float = 0.85,
    mongo_storage_stop_ratio: float = 0.90,
    qdrant_pressure: dict[str, Any] | None = None,
    neo4j_pressure: dict[str, Any] | None = None,
    qdrant_write_concurrency: int | None = None,
    neo4j_write_concurrency: int | None = None,
) -> dict[str, Any]:
    graph_jobs = graph_jobs or {}
    extraction_jobs = extraction_jobs or {}
    mongo_stats = mongo_stats or {}

    rss_pressure = _ratio(backend_rss_mb, rss_soft_limit_mb)
    mongo_fs_used_bytes = _int(mongo_stats.get("fsUsedSize"))
    mongo_fs_total_bytes = _int(mongo_stats.get("fsTotalSize"))
    mongo_fs_pressure = _ratio(mongo_fs_used_bytes, mongo_fs_total_bytes)
    graph_pending = sum(
        _int(graph_jobs.get(status))
        for status in ("queued", "running", "blocked_failed_chunks", "blocked_no_extractions")
    )
    extraction_pending = sum(
        _int(extraction_jobs.get(status))
        for status in ("queued", "running", "provider_failed", "validation_failed", "failed")
    )

    reasons: list[str] = []
    recommendations: list[str] = []
    status = "normal"
    if rss_pressure is not None and rss_pressure >= 1.0:
        status = "high"
        reasons.append("backend_rss_over_soft_limit")
        recommendations.extend([
            "pause_nonessential_backfills",
            "reduce_extraction_backfill_pressure",
            "let_write_queues_drain",
        ])
    elif rss_pressure is not None and rss_pressure >= 0.75:
        status = "elevated"
        reasons.append("backend_rss_near_soft_limit")
        recommendations.append("run_bounded_repairs_only")

    stop_ratio = max(0.0, min(float(mongo_storage_stop_ratio or 0.90), 1.0))
    warn_ratio = max(0.0, min(float(mongo_storage_warn_ratio or 0.85), stop_ratio))
    if mongo_fs_pressure is not None and mongo_fs_pressure >= stop_ratio:
        status = "high"
        reasons.append("mongo_storage_over_stop_limit")
        recommendations.extend([
            "pause_nonessential_backfills",
            "free_mongo_storage_or_expand_volume",
            "let_write_queues_drain",
        ])
    elif mongo_fs_pressure is not None and mongo_fs_pressure >= warn_ratio:
        if status == "normal":
            status = "elevated"
        reasons.append("mongo_storage_near_stop_limit")
        recommendations.append("run_bounded_repairs_only")

    if active_repairs > 0:
        if status == "normal":
            status = "elevated"
        reasons.append("repair_runs_active")
    if graph_pending or extraction_pending or summary_missing:
        recommendations.append("continue_incremental_repair")

    qdrant_writer = _writer_pressure_snapshot(
        qdrant_pressure,
        default_latency_warn_ms=2_000,
        default_latency_stop_ms=5_000,
        default_queue_warn=1_000,
        default_queue_stop=5_000,
    )
    neo4j_writer = _writer_pressure_snapshot(
        neo4j_pressure,
        default_latency_warn_ms=3_000,
        default_latency_stop_ms=10_000,
        default_queue_warn=500,
        default_queue_stop=2_000,
    )
    qdrant_blocked = qdrant_writer["status"] == "high"
    neo4j_blocked = neo4j_writer["status"] == "high"
    if qdrant_writer["status"] != "normal":
        if status == "normal" or qdrant_blocked:
            status = qdrant_writer["status"]
        reasons.append(f"qdrant_write_pressure_{qdrant_writer['status']}")
        recommendations.append(
            "pause_qdrant_indexing" if qdrant_blocked else "run_bounded_qdrant_writes"
        )
    if neo4j_writer["status"] != "normal":
        if status == "normal" or neo4j_blocked:
            status = neo4j_writer["status"]
        reasons.append(f"neo4j_write_pressure_{neo4j_writer['status']}")
        recommendations.append(
            "pause_neo4j_promotion" if neo4j_blocked else "run_bounded_graph_writes"
        )

    block_all = any(
        reason in reasons
        for reason in ("backend_rss_over_soft_limit", "mongo_storage_over_stop_limit")
    )
    return {
        "status": status,
        "reasons": list(dict.fromkeys(reasons)),
        "recommendations": list(dict.fromkeys(recommendations)),
        "resources": {
            "backend_rss_mb": backend_rss_mb,
            "ram_cap_mb": ram_cap_mb,
            "rss_soft_limit_mb": rss_soft_limit_mb,
            "rss_pressure": rss_pressure,
        },
        "queues": {
            "active_repairs": _int(active_repairs),
            "graph_pending": graph_pending,
            "extraction_pending": extraction_pending,
            "summary_missing": _int(summary_missing),
        },
        "storage": {
            "mongo_storage_bytes": _int(mongo_stats.get("storageSize")),
            "mongo_data_bytes": _int(mongo_stats.get("dataSize")),
            "mongo_index_bytes": _int(mongo_stats.get("indexSize")),
            "mongo_objects": _int(mongo_stats.get("objects")),
            "mongo_fs_used_bytes": mongo_fs_used_bytes,
            "mongo_fs_total_bytes": mongo_fs_total_bytes,
            "mongo_fs_pressure": mongo_fs_pressure,
            "mongo_storage_warn_ratio": warn_ratio,
            "mongo_storage_stop_ratio": stop_ratio,
        },
        "limits": {
            "qdrant_write_concurrency": qdrant_write_concurrency,
            "neo4j_write_concurrency": neo4j_write_concurrency,
        },
        "writers": {
            "qdrant": qdrant_writer,
            "neo4j": neo4j_writer,
        },
        "backpressure": {
            "source_parse_allowed": not block_all,
            "document_pipeline_allowed": not block_all and not qdrant_blocked,
            # Summary text generation and document-summary repair are durable
            # Mongo/provider work. Qdrant pressure should pause vector/index
            # writes, not freeze the semantic summary backlog entirely.
            "summary_generation_allowed": not block_all,
            "summary_indexing_allowed": not block_all and not qdrant_blocked,
            # Backwards-compatible alias used by existing repair/UI paths.
            "summary_backfill_allowed": not block_all,
            "extraction_backfill_allowed": not block_all,
            "graph_promotion_allowed": not block_all and not neo4j_blocked,
        },
    }
