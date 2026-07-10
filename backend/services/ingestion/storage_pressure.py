"""Storage service pressure samplers for ingestion readiness.

The pressure model is intentionally provider-agnostic. This module translates
service-specific telemetry into the small writer-pressure payload consumed by
``build_ingestion_pressure_snapshot``.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

_METRIC_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+"
    r"(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$"
)
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"')
_MEMORY_RE = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[kmgtp]?i?b?|[kmgtp])?\s*$", re.I)


def parse_memory_limit_bytes(value: Any) -> int | None:
    """Parse Docker-style memory strings such as ``5g`` or ``512MiB``."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value > 0 else None
    match = _MEMORY_RE.match(str(value))
    if not match:
        return None
    amount = float(match.group("value"))
    unit = (match.group("unit") or "b").lower()
    multipliers = {
        "": 1,
        "b": 1,
        "k": 1024,
        "kb": 1024,
        "ki": 1024,
        "kib": 1024,
        "m": 1024**2,
        "mb": 1024**2,
        "mi": 1024**2,
        "mib": 1024**2,
        "g": 1024**3,
        "gb": 1024**3,
        "gi": 1024**3,
        "gib": 1024**3,
        "t": 1024**4,
        "tb": 1024**4,
        "ti": 1024**4,
        "tib": 1024**4,
        "p": 1024**5,
        "pb": 1024**5,
        "pi": 1024**5,
        "pib": 1024**5,
    }
    multiplier = multipliers.get(unit)
    if multiplier is None:
        return None
    return int(amount * multiplier)


def _metric_labels(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    return {
        key: value.replace(r"\"", '"').replace(r"\\", "\\")
        for key, value in _LABEL_RE.findall(raw)
    }


def _metric_samples(metrics_text: str, metric_name: str) -> list[tuple[dict[str, str], float]]:
    samples: list[tuple[dict[str, str], float]] = []
    for line in metrics_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = _METRIC_RE.match(line)
        if not match or match.group("name") != metric_name:
            continue
        try:
            value = float(match.group("value"))
        except (TypeError, ValueError):
            continue
        samples.append((_metric_labels(match.group("labels")), value))
    return samples


def qdrant_pressure_from_prometheus(
    metrics_text: str,
    *,
    memory_limit_bytes: int | None = None,
    memory_warn_ratio: float = 0.85,
    memory_stop_ratio: float = 0.90,
) -> dict[str, Any]:
    """Build a writer-pressure payload from Qdrant Prometheus metrics.

    Qdrant does not expose container RSS in its service telemetry. What it does
    expose reliably is update queue depth and REST write latency. Those are the
    signals this app can sample without a Docker socket or sidecar metrics
    collector.
    """

    queue_values = [
        int(value)
        for _labels, value in _metric_samples(metrics_text, "collection_update_queue_length")
    ]
    deferred_values = [
        int(value)
        for _labels, value in _metric_samples(
            metrics_text,
            "collection_update_queue_deferred_points",
        )
    ]
    collections_total = next(
        (int(value) for _labels, value in _metric_samples(metrics_text, "collections_total")),
        None,
    )
    vectors_total = next(
        (
            int(value)
            for _labels, value in _metric_samples(metrics_text, "collections_vector_total")
        ),
        None,
    )
    memory_resident_bytes = next(
        (
            int(value)
            for _labels, value in _metric_samples(metrics_text, "memory_resident_bytes")
        ),
        None,
    )
    memory_allocated_bytes = next(
        (
            int(value)
            for _labels, value in _metric_samples(metrics_text, "memory_allocated_bytes")
        ),
        None,
    )
    memory_active_bytes = next(
        (int(value) for _labels, value in _metric_samples(metrics_text, "memory_active_bytes")),
        None,
    )
    memory_retained_bytes = next(
        (
            int(value)
            for _labels, value in _metric_samples(metrics_text, "memory_retained_bytes")
        ),
        None,
    )

    write_endpoint_names = {
        "/collections/{collection_name}/points",
        "/collections/{collection_name}/points/payload",
        "/collections/{collection_name}/index",
    }
    write_latencies_s = [
        value
        for labels, value in _metric_samples(
            metrics_text,
            "rest_responses_avg_duration_seconds",
        )
        if labels.get("status") == "200"
        and labels.get("endpoint") in write_endpoint_names
    ]

    queue_depth = sum(queue_values) + sum(deferred_values)
    payload: dict[str, Any] = {
        "source": "qdrant_metrics",
        "queue_depth": queue_depth,
        "max_queue_depth": max(queue_values or [0]),
        "deferred_points": sum(deferred_values),
    }
    if collections_total is not None:
        payload["collections_total"] = collections_total
    if vectors_total is not None:
        payload["vectors_total"] = vectors_total
    if memory_resident_bytes is not None:
        payload["memory_resident_bytes"] = memory_resident_bytes
    if memory_allocated_bytes is not None:
        payload["memory_allocated_bytes"] = memory_allocated_bytes
    if memory_active_bytes is not None:
        payload["memory_active_bytes"] = memory_active_bytes
    if memory_retained_bytes is not None:
        payload["memory_retained_bytes"] = memory_retained_bytes
    if memory_limit_bytes and memory_limit_bytes > 0:
        memory_used_bytes = memory_resident_bytes or memory_allocated_bytes or memory_active_bytes
        payload["memory_limit_bytes"] = int(memory_limit_bytes)
        payload["memory_warn_ratio"] = max(0.0, min(float(memory_warn_ratio or 0.85), 1.0))
        payload["memory_stop_ratio"] = max(
            0.0,
            min(float(memory_stop_ratio or 0.90), 1.0),
        )
        if memory_used_bytes:
            memory_pressure = round(memory_used_bytes / memory_limit_bytes, 4)
            payload["memory_pressure"] = memory_pressure
            if memory_pressure >= payload["memory_stop_ratio"]:
                payload["status"] = "high"
                payload["reasons"] = ["qdrant_memory_over_stop_limit"]
            elif memory_pressure >= payload["memory_warn_ratio"]:
                payload["status"] = "elevated"
                payload["reasons"] = ["qdrant_memory_near_stop_limit"]
    if write_latencies_s:
        payload["write_latency_ms"] = round(max(write_latencies_s) * 1000, 2)
    return payload


async def sample_qdrant_pressure(
    qdrant_url: str | None,
    *,
    timeout_s: float = 1.0,
    memory_limit_bytes: int | None = None,
    memory_warn_ratio: float = 0.85,
    memory_stop_ratio: float = 0.90,
) -> dict[str, Any]:
    """Fetch Qdrant writer pressure, returning an empty payload on failure."""

    base = str(qdrant_url or "").strip().rstrip("/")
    if not base:
        return {}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.get(f"{base}/metrics")
            response.raise_for_status()
        return qdrant_pressure_from_prometheus(
            response.text,
            memory_limit_bytes=memory_limit_bytes,
            memory_warn_ratio=memory_warn_ratio,
            memory_stop_ratio=memory_stop_ratio,
        )
    except Exception:
        return {}
