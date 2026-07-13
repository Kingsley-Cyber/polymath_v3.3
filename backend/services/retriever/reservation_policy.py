"""Single corpus-reservation discipline for final evidence seats (P0.3).

Two deciders can grant a selected corpus a protected seat in the final
packet: ``planned_fusion.reserve_planned_finalists`` (post-rerank finalist
reservations, including candidates preserved earlier by
``filter_grounded_planned_candidates``) and
``ranking_policy.select_with_diversity`` (the corpus floor inside diversity
selection). Historically each applied its own threshold, so one path could
seat a corpus the other had rejected.

Ordering contract:

1. ``select_with_diversity`` picks the packet first; its corpus floor may
   only seat a corpus whose candidate passes THIS calibrated bound.
2. ``reserve_planned_finalists`` may add or protect corpus evidence only
   through the same bound — including candidates that are already selected;
   protection is what shields a candidate from the overflow trim, so a
   sub-bound candidate must not be protected as corpus evidence.
3. Neither path may inflate a score to protect a seat. Seat protection is
   expressed through the selection reason (``selected_by`` /
   ``planned_corpus_reservations``), never through score bonuses, so
   downstream ordering and diagnostics stay honest.

The bound applies to calibrated scores (0..1 score families). Uncalibrated
score families (raw logits) have no ratio semantics; callers keep their
existing behavior for those and must say so in diagnostics.
"""

from __future__ import annotations

# A selected corpus receives a reserved seat only when its best candidate is
# both absolutely relevant (MIN_SCORE) and not vanishingly weak relative to
# the packet's best evidence (MIN_SCORE_RATIO of top score).
CORPUS_RESERVATION_MIN_SCORE = 0.25
CORPUS_RESERVATION_MIN_SCORE_RATIO = 0.30


def corpus_reservation_bound(top_score: float) -> float | None:
    """Return the calibrated reservation bound, or None when the score family
    is not calibrated to 0..1 (no ratio semantics available)."""

    if 0.0 < top_score <= 1.0:
        return max(
            CORPUS_RESERVATION_MIN_SCORE,
            top_score * CORPUS_RESERVATION_MIN_SCORE_RATIO,
        )
    return None


def passes_corpus_reservation(score: float, top_score: float) -> bool:
    """Shared seat gate. True when the candidate may hold a corpus seat."""

    bound = corpus_reservation_bound(top_score)
    if bound is None:
        return True
    return 0.0 <= score <= top_score and score >= bound
