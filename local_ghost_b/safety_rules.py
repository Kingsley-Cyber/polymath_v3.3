"""Shared safety filters for all relation classifiers.

Re-exports the type-plausibility gate and dangerous-pair guard from
polymath_local_extractor.py so that any classifier (cascade, glirel,
ensemble) can apply the same post-classification checks.

The cascade already calls these inline in LocalExtractor._resolve.commit().
GLiRELClassifier calls them via apply_safety() below.

This module deliberately re-exports rather than re-defines so the rules
stay single-sourced. Update polymath_local_extractor.py, both consumers
inherit the change.
"""

from __future__ import annotations

import os
from typing import Optional

# Re-export the constants and helpers from the existing extractor module.
# Anything new that needs to be classifier-agnostic goes HERE, not there.
from polymath_local_extractor import (  # noqa: F401  (public re-exports)
    Edge,
    TYPE_CONSTRAINTS,
    DANGEROUS_CLUSTERS,
    PRED_TO_DCLUSTER,
    DANGER_CUE,
    type_plausible,
    guard_dangerous,
)


def _envb(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def apply_safety(
    edge: Edge,
    pair: dict,
    *,
    type_constraints: Optional[bool] = None,
    danger_guard: Optional[bool] = None,
) -> Edge:
    """Apply type-plausibility + dangerous-cluster guards to a single edge.

    Mirrors the inline checks in LocalExtractor._resolve.commit() so a
    non-cascade classifier (e.g. GLiREL) gets identical safety behavior.

    Returns a new Edge — original is not mutated. On safety violation the
    edge is demoted to `related_to` with a tier3_related/source annotation.
    """
    if type_constraints is None:
        type_constraints = _envb("LOCAL_GHOST_B_TYPE_CONSTRAINTS", True)
    if danger_guard is None:
        danger_guard = _envb("LOCAL_GHOST_B_DANGER_GUARD", False)

    pred = edge.predicate
    if pred in ("related_to", "no_relation", "none"):
        return edge

    st = pair.get("subject_type", "Concept")
    ot = pair.get("object_type", "Concept")

    if type_constraints and not type_plausible(pred, st, ot):
        return Edge(
            subject=edge.subject,
            predicate="related_to",
            object=edge.object,
            confidence=edge.confidence,
            tier="tier3_related",
            source=f"{edge.source}+type_violation:{pred}",
        )

    if danger_guard and pred in PRED_TO_DCLUSTER:
        g = guard_dangerous(pred, pair.get("text", ""), pair.get("cue", ""), st, ot)
        if g != pred:
            if g == "related_to":
                return Edge(
                    subject=edge.subject,
                    predicate="related_to",
                    object=edge.object,
                    confidence=edge.confidence,
                    tier="tier3_related",
                    source=f"{edge.source}+danger_guard:{pred}",
                )
            return Edge(
                subject=edge.subject,
                predicate=g,
                object=edge.object,
                confidence=edge.confidence,
                tier=edge.tier,
                source=f"{edge.source}+corrected:{pred}->{g}",
            )

    return edge


def apply_safety_batch(edges: list, pairs: list) -> list:
    """Vectorized convenience wrapper for a list of (edge, pair) zips."""
    return [apply_safety(e, p) for e, p in zip(edges, pairs)]
