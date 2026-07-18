"""Deterministic metadata merging for planned-retrieval candidates."""

from __future__ import annotations

from typing import Any


def _max_numeric_map(*values: Any) -> dict[str, float]:
    merged: dict[str, float] = {}
    for value in values:
        if not isinstance(value, dict):
            continue
        for key, raw in value.items():
            try:
                score = float(raw or 0.0)
            except (TypeError, ValueError):
                continue
            normalized = str(key or "").strip()
            if normalized:
                merged[normalized] = max(merged.get(normalized, 0.0), score)
    return merged


def merge_planned_attribution(
    base: dict[str, Any] | None,
    incoming: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge lane membership/affinity without changing candidate scores."""

    other = dict(incoming or {})
    output = dict(other)
    output.update(dict(base or {}))
    lanes = sorted(
        {
            str(value)
            for value in [
                *(output.get("planned_lanes") or []),
                *(other.get("planned_lanes") or []),
            ]
            if str(value or "").strip()
        }
    )
    grounding = _max_numeric_map(
        output.get("planned_lane_grounding"),
        other.get("planned_lane_grounding"),
    )
    routes = _max_numeric_map(
        output.get("document_route_lanes"),
        other.get("document_route_lanes"),
    )
    affinity = _max_numeric_map(
        output.get("planned_lane_affinity"),
        other.get("planned_lane_affinity"),
        grounding,
        routes,
    )
    if lanes:
        output["planned_lanes"] = lanes
    if grounding:
        output["planned_lane_grounding"] = grounding
    if routes:
        output["document_route_lanes"] = routes
    if affinity:
        output["planned_lane_affinity"] = affinity
        output["planned_max_affinity_lane"] = min(
            affinity,
            key=lambda lane_id: (-affinity[lane_id], lane_id),
        )
        output["planned_max_affinity"] = affinity[output["planned_max_affinity_lane"]]
    return output
