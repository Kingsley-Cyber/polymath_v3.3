"""Ensemble classifier: commit typed predicate only when cascade + GLiREL agree.

Drop-in replacement for LocalExtractor / GliRELClassifier — same extract(pairs)
API, same Edge output. Used when --classifier ensemble.

Decision rule (per pair):
    - both predict the same typed predicate  -> commit it, avg confidence
    - both predict `related_to`              -> related_to (consistent)
    - one types, the other says related_to   -> related_to (no quorum)
    - both type but disagree                 -> related_to (conflict)

The pair always survives (gate already accepted it) — what differs is whether
we commit a typed edge or fall back to related_to.

`safety_rules.apply_safety` is called by each member classifier separately;
the ensemble just merges their post-safety outputs, so the gates fire correctly
in each path before we vote.
"""

from __future__ import annotations

from typing import List

from polymath_local_extractor import Edge


class EnsembleClassifier:
    def __init__(self, cascade, glirel):
        if cascade is None or glirel is None:
            raise ValueError("EnsembleClassifier needs both cascade and glirel members")
        self.cascade = cascade
        self.glirel = glirel

    def extract(self, pairs: List[dict]) -> List[Edge]:
        c_edges = self.cascade.extract(pairs)
        g_edges = self.glirel.extract(pairs)
        if len(c_edges) != len(g_edges) or len(c_edges) != len(pairs):
            raise RuntimeError(
                f"ensemble length mismatch: pairs={len(pairs)} "
                f"cascade={len(c_edges)} glirel={len(g_edges)}"
            )
        out: List[Edge] = []
        for c, g in zip(c_edges, g_edges):
            out.append(self._vote(c, g))
        return out

    def config_summary(self) -> dict:
        return {
            "classifier": "ensemble",
            "members": ["cascade", "glirel"],
            "rule": "commit_typed_when_both_agree",
        }

    @staticmethod
    def _vote(c: Edge, g: Edge) -> Edge:
        c_typed = c.predicate not in ("related_to", "no_relation", "none")
        g_typed = g.predicate not in ("related_to", "no_relation", "none")

        # Both agree on a typed predicate -> commit
        if c_typed and g_typed and c.predicate == g.predicate:
            avg = round((c.confidence + g.confidence) / 2.0, 3)
            return Edge(
                subject=c.subject,
                predicate=c.predicate,
                object=c.object,
                confidence=avg,
                tier="tier1_exact",
                source=f"ensemble_agree:cascade={c.source}|glirel={g.source}",
            )

        # Both abstain (related_to) -> keep cascade's edge as the canonical
        if not c_typed and not g_typed:
            return Edge(
                subject=c.subject,
                predicate="related_to",
                object=c.object,
                confidence=max(c.confidence, g.confidence),
                tier="tier3_related",
                source="ensemble_both_abstain",
            )

        # Disagreement (one typed, other not; or both typed differently)
        return Edge(
            subject=c.subject,
            predicate="related_to",
            object=c.object,
            confidence=max(c.confidence, g.confidence),
            tier="tier3_related",
            source=(f"ensemble_disagree:cascade={c.predicate}"
                    f"({c.confidence})|glirel={g.predicate}({g.confidence})"),
        )
