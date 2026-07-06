"""E1 — quality-gated RTX enrichment decision (§13-H, owner-ratified).

"Fast Local Graph + RTX Enrichment": the local GLiNER/GLiREL pass always runs
first and builds the bulk graph skeleton; this module owns the PURE decision
of whether the cloud/RTX lane runs a second pass and which chunks it sees.
RTX is a precision booster, not the engine — selection is bounded by
max_chunk_ratio so enrichment can never balloon into re-extracting the doc.

Gate signals (all present in the local pass's ExtractionBatchReport.metrics):
  coverage          extracted/requested chunks — misses and empty spans
  facts_per_chunk   GLiNER/GLiREL is weak on facts (measured 0.4/chunk vs
                    RTX 2.35/chunk, 2026-07-05)
  related_to_ratio  GLiREL predicate ambiguity — high generic ratio means
                    typed relations degraded to the sentinel

Duck-typed against result/failure/task objects (chunk_id / entities /
relations / facts / predicate attributes) — no ghost_b import, no I/O,
deterministic, standalone-testable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence


@dataclass(frozen=True)
class EnrichmentVerdict:
    enrich: bool
    reasons: tuple[str, ...]
    coverage: float
    facts_per_chunk: float
    related_to_ratio: float


def enrichment_verdict(
    metrics: dict | None,
    *,
    min_coverage: float = 0.80,
    min_facts_per_chunk: float = 1.0,
    max_related_to_ratio: float = 0.40,
) -> EnrichmentVerdict:
    """Score the local pass. Missing metric keys count as zero, which fails
    toward enrichment — a local pass that cannot account for its output does
    not get to skip the booster (silent-fallback accounting)."""
    m = metrics or {}
    requested = int(m.get("requested_chunks") or 0)
    extracted = int(m.get("extracted_chunks") or 0)
    facts = int(m.get("fact_count") or 0)
    relations = int(m.get("relation_count") or 0)
    related_to = int(m.get("related_to_count") or 0)

    coverage = extracted / requested if requested else 0.0
    facts_per_chunk = facts / extracted if extracted else 0.0
    related_ratio = related_to / relations if relations else 0.0

    reasons: list[str] = []
    if requested and coverage < min_coverage:
        reasons.append(f"coverage {coverage:.0%} < {min_coverage:.0%}")
    if extracted and facts_per_chunk < min_facts_per_chunk:
        reasons.append(
            f"facts/chunk {facts_per_chunk:.2f} < {min_facts_per_chunk:.2f}"
        )
    if relations and related_ratio > max_related_to_ratio:
        reasons.append(
            f"related_to ratio {related_ratio:.0%} > {max_related_to_ratio:.0%}"
        )

    return EnrichmentVerdict(
        enrich=bool(reasons),
        reasons=tuple(reasons),
        coverage=round(coverage, 4),
        facts_per_chunk=round(facts_per_chunk, 4),
        related_to_ratio=round(related_ratio, 4),
    )


def _chunk_id(obj: Any) -> str:
    return str(getattr(obj, "chunk_id", "") or "")


def select_enrichment_tasks(
    tasks: Sequence[Any],
    results: Sequence[Any],
    failures: Sequence[Any],
    verdict: EnrichmentVerdict,
    *,
    max_chunk_ratio: float = 0.50,
) -> list[Any]:
    """Pick WHICH chunks the RTX lane re-extracts, in priority order:

      1. gaps — tasks with no local result at all (failures + silent misses)
      2. empty results — a result object with zero entities AND relations
      3. predicate-ambiguous — chunks whose relations are >50% related_to
         (only when the verdict tripped on related_to ratio)
      4. fact-thin — chunks with zero facts (only when the verdict tripped
         on facts/chunk)

    Deterministic: task order is preserved within each priority band; the
    total is capped at ceil(len(tasks) * max_chunk_ratio) so RTX stays the
    booster. Returns the original task objects, ready for the cloud engine.
    """
    if not verdict.enrich or not tasks:
        return []

    by_id = {_chunk_id(r): r for r in results if _chunk_id(r)}
    picked: list[Any] = []
    picked_ids: set[str] = set()
    cap = max(1, math.ceil(len(tasks) * max(0.0, min(1.0, max_chunk_ratio))))

    def _take(task: Any) -> bool:
        cid = _chunk_id(task)
        if not cid or cid in picked_ids:
            return False
        picked.append(task)
        picked_ids.add(cid)
        return len(picked) >= cap

    # 1. gaps — no result object for the chunk
    for t in tasks:
        if _chunk_id(t) not in by_id:
            if _take(t):
                return picked

    # 2. empty results
    for t in tasks:
        r = by_id.get(_chunk_id(t))
        if r is not None and not getattr(r, "entities", None) and not getattr(
            r, "relations", None
        ):
            if _take(t):
                return picked

    # 3. predicate-ambiguous chunks
    if any(reason.startswith("related_to") for reason in verdict.reasons):
        for t in tasks:
            r = by_id.get(_chunk_id(t))
            rels = list(getattr(r, "relations", None) or []) if r else []
            if rels:
                generic = sum(
                    1
                    for rel in rels
                    if str(getattr(rel, "predicate", "")) == "related_to"
                )
                if generic / len(rels) > 0.5:
                    if _take(t):
                        return picked

    # 4. fact-thin chunks
    if any(reason.startswith("facts/chunk") for reason in verdict.reasons):
        for t in tasks:
            r = by_id.get(_chunk_id(t))
            if r is not None and not getattr(r, "facts", None):
                if _take(t):
                    return picked

    return picked
