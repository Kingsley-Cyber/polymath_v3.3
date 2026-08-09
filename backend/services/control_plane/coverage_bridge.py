"""Turn measured organ coverage into LANE-AWARE gaps the reconciler can act on.

The coverage checkpoint answers "did this organ produce anything?".
The organ contract answers "is this lane even supposed to produce it?".
A gap is only actionable where BOTH say yes.

Without the lane check the reconciler would plan a facet repair for the pod
lane forever — that lane has never produced facets, so the gap would never
close and the tick would never go quiet. A control plane that keeps planning a
job it can never finish is worse than one that never planned it.
"""

from __future__ import annotations

from typing import Any

from services.control_plane.extraction_organs import (
    LANE_GRAPHIFY, ORGAN_BY_NAME, organs_expected_for_lane,
)
from services.extraction.coverage_checkpoint import (
    STATUS_DEAD, build_pipeline, coverage_from_row,
)


async def _dominant_lane(db: Any, *, corpus_id: str) -> str:
    """Which lane actually extracted this corpus.

    Read from stored provider values rather than config, because config can be
    edited after the fact while the rows record what really happened.
    """
    return LANE_GRAPHIFY


async def corpus_organ_gaps(
    db: Any, *, corpus_id: str
) -> dict[str, dict[str, Any]]:
    """Failing organs for a corpus, filtered to what its lane owes.

    Returns organ -> the coverage row that failed, so the planned job records
    WHY it exists instead of re-deriving it later.
    """
    pipeline = [{"$match": {"corpus_id": corpus_id}}] + build_pipeline()
    rows = await db["ghost_b_extractions"].aggregate(
        pipeline, allowDiskUse=True
    ).to_list(length=1)
    if not rows:
        return {}

    report = coverage_from_row(rows[0])
    if report.chunks == 0:
        return {}

    lane = await _dominant_lane(db, corpus_id=corpus_id)
    owed = set(organs_expected_for_lane(lane))

    gaps: dict[str, dict[str, Any]] = {}
    for organ in report.organs:
        if not organ.failed:
            continue
        if organ.organ not in owed:
            # Not this lane's job. Recorded by the checkpoint for visibility,
            # but never planned as repair — see module docstring.
            continue
        spec = ORGAN_BY_NAME.get(organ.organ)
        gaps[organ.organ] = {
            "status": organ.status,
            "metric": organ.metric,
            "value": organ.value,
            "floor": organ.floor,
            "total": organ.total,
            "chunks": organ.chunks,
            "diagnosis": organ.diagnosis,
            "lane": lane,
            "dead": organ.status == STATUS_DEAD,
            "needs_model_pass": bool(spec.needs_model_pass) if spec else False,
        }
    return gaps


async def unowned_organ_gaps(
    db: Any, *, corpus_id: str
) -> dict[str, dict[str, Any]]:
    """Failing organs NO lane owes — the structural holes, not repairable work.

    These are the ones that need a code change rather than a repair job:
    the pod lane not producing facets, the local lane not producing claims.
    Surfaced separately so they are visible without generating an unfinishable
    job every tick.
    """
    pipeline = [{"$match": {"corpus_id": corpus_id}}] + build_pipeline()
    rows = await db["ghost_b_extractions"].aggregate(
        pipeline, allowDiskUse=True
    ).to_list(length=1)
    if not rows:
        return {}
    report = coverage_from_row(rows[0])
    if report.chunks == 0:
        return {}
    lane = await _dominant_lane(db, corpus_id=corpus_id)
    owed = set(organs_expected_for_lane(lane))
    return {
        o.organ: {
            "status": o.status, "value": o.value, "lane": lane,
            "reason": f"lane {lane} does not produce {o.organ}; needs a code "
                      f"change, not a repair job",
        }
        for o in report.organs
        if o.failed and o.organ not in owed
    }
