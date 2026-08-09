"""Extraction coverage checkpoints — make a dead organ impossible to miss.

WHY THIS EXISTS
    `runpod_local_extraction` returned hardcoded `relations=[]` and `facts=[]`,
    and never ran the facet pass at all. THREE of the four extraction organs
    were dead on the lane that produced six of seven corpora — 362,142 of
    362,759 chunks — and nothing anywhere reported it. The pipeline looked
    healthy: chunks ingested, entities present, jobs green, readiness OK.

    Nothing was watching whether each ORGAN actually fired.

    retrieval_readiness answers "is the schema/collection wired?". It does not
    answer "did extraction actually produce anything?". This module answers the
    second question, per corpus, per organ, with a durable receipt.

THE RULE THAT WOULD HAVE CAUGHT IT
    An organ producing EXACTLY ZERO across a whole corpus is ALWAYS a failure,
    whatever the threshold. Zero is not a low number — it is a disconnected
    wire. `DEAD_ORGAN` is a distinct status from `BELOW_THRESHOLD` precisely so
    it cannot be tuned away by lowering a bar.

THRESHOLDS
    Floors, not targets. Set from measured 2026-07-31 baselines with headroom,
    so ordinary corpus variation does not cry wolf but a disconnected organ
    always trips. A corpus legitimately below a floor is recorded with its
    reason rather than silently passing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

CHECKPOINT_VERSION = "polymath.extraction_coverage.v1"

# organ -> (metric, floor, what a zero means)
ORGAN_FLOORS: dict[str, tuple[str, float, str]] = {
    "entities": ("per_chunk", 0.50,
                 "GLiNER pass-1 did not run or returned nothing"),
    "facets": ("eligible_coverage", 0.15,
               "GLiNER pass-2 (object_kind) did not run for this lane"),
    "relations": ("per_chunk", 0.010,
                  "relation lane returned [] — check for a hardcoded empty"),
    "facts": ("per_chunk", 0.20,
              "Stage D enrich rules did not run — check for a hardcoded empty"),
    "claims": ("per_chunk", 0.50,
               "claim compiler did not run"),
}

STATUS_OK = "ok"
STATUS_BELOW = "below_threshold"
STATUS_DEAD = "DEAD_ORGAN"


@dataclass
class OrganCoverage:
    organ: str
    metric: str
    value: float
    floor: float
    status: str
    total: int = 0
    chunks: int = 0
    diagnosis: str = ""

    @property
    def failed(self) -> bool:
        return self.status in (STATUS_BELOW, STATUS_DEAD)


@dataclass
class CorpusCoverage:
    corpus_id: str
    corpus_name: str
    chunks: int
    organs: list[OrganCoverage] = field(default_factory=list)
    checked_at: str = ""
    checkpoint_version: str = CHECKPOINT_VERSION

    @property
    def dead_organs(self) -> list[str]:
        return [o.organ for o in self.organs if o.status == STATUS_DEAD]

    @property
    def failing_organs(self) -> list[str]:
        return [o.organ for o in self.organs if o.failed]

    @property
    def healthy(self) -> bool:
        return not self.failing_organs

    def to_doc(self) -> dict[str, Any]:
        d = asdict(self)
        d["dead_organs"] = self.dead_organs
        d["failing_organs"] = self.failing_organs
        d["healthy"] = self.healthy
        return d


def classify(
    organ: str, value: float, total: int, chunks: int
) -> OrganCoverage:
    """Grade one organ. Zero is DEAD, never merely 'low'."""
    metric, floor, zero_means = ORGAN_FLOORS[organ]
    if chunks == 0:
        return OrganCoverage(organ, metric, 0.0, floor, STATUS_OK, total, chunks,
                             "corpus has no chunks; nothing to grade")
    if total == 0:
        return OrganCoverage(organ, metric, 0.0, floor, STATUS_DEAD, total,
                             chunks, zero_means)
    status = STATUS_OK if value >= floor else STATUS_BELOW
    diag = "" if status == STATUS_OK else (
        f"{metric}={value:.4f} is below floor {floor}; "
        f"organ is firing but under-producing"
    )
    return OrganCoverage(organ, metric, round(value, 4), floor, status,
                         total, chunks, diag)


def build_pipeline() -> list[dict[str, Any]]:
    """Mongo aggregation computing every organ in ONE pass over a corpus."""
    return [
        {"$group": {
            "_id": "$corpus_id",
            "chunks": {"$sum": 1},
            "entities": {"$sum": {"$size": {"$ifNull": ["$entities", []]}}},
            "relations": {"$sum": {"$size": {"$ifNull": ["$relations", []]}}},
            "facts": {"$sum": {"$size": {"$ifNull": ["$facts", []]}}},
            "claims": {"$sum": {
                "$size": {"$ifNull": ["$claim_compilation.claims", []]}}},
            # Facet coverage is measured over entity MENTIONS carrying a
            # non-empty object_kind, not over chunks: a chunk with one faceted
            # entity out of ten is not "covered".
            "faceted_mentions": {"$sum": {
                "$size": {"$filter": {
                    "input": {"$ifNull": ["$entities", []]},
                    "as": "e",
                    "cond": {"$and": [
                        {"$ne": ["$$e.object_kind", None]},
                        {"$ne": ["$$e.object_kind", ""]},
                    ]},
                }}}},
        }},
    ]


def coverage_from_row(row: dict[str, Any], corpus_name: str = "") -> CorpusCoverage:
    chunks = int(row.get("chunks") or 0)
    ents = int(row.get("entities") or 0)
    organs = [
        classify("entities", (ents / chunks) if chunks else 0.0, ents, chunks),
        classify(
            "facets",
            (int(row.get("faceted_mentions") or 0) / ents) if ents else 0.0,
            int(row.get("faceted_mentions") or 0), chunks,
        ),
        classify("relations",
                 (int(row.get("relations") or 0) / chunks) if chunks else 0.0,
                 int(row.get("relations") or 0), chunks),
        classify("facts",
                 (int(row.get("facts") or 0) / chunks) if chunks else 0.0,
                 int(row.get("facts") or 0), chunks),
        classify("claims",
                 (int(row.get("claims") or 0) / chunks) if chunks else 0.0,
                 int(row.get("claims") or 0), chunks),
    ]
    return CorpusCoverage(
        corpus_id=str(row.get("_id") or ""),
        corpus_name=corpus_name,
        chunks=chunks,
        organs=organs,
        checked_at=datetime.now(timezone.utc).isoformat(),
    )
