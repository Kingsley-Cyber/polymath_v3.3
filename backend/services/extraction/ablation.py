"""Ablation profile configuration for the unified shadow pipeline.

Provides a single immutable feature object resolved once at startup.
Components receive the object and gate their behavior accordingly.
No scattered --disable-* flags, no global env vars, no runtime mutation.

Profiles A–F form a cumulative ablation:
    B − A = head-token join effect
    C − B = open-relation preservation effect
    D − C = credit-pattern effect
    E − D = verb-preposition frame effect
    F − E = typed cross-sentence link policy effect

Profile A reproduces the typed-v2 pre-P2 system (P1A types, endpoint
signatures, SHADOW_RELEX_HIGH demotion, exact-offset evaluation,
cross-sentence containment, corrected deduplication). Only the five
ablated features vary.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtractionFeatures:
    """Immutable feature switches for one ablation profile.

    Passed into pipeline components at startup. Components must not
    read CLI state or environment variables — only this object.
    """

    head_token_join: bool
    open_relation_lane: bool
    credit_patterns: bool
    verb_prep_frames: bool
    typed_cross_sentence_links: bool


ABLATION_PROFILES: dict[str, ExtractionFeatures] = {
    "A": ExtractionFeatures(False, False, False, False, False),
    "B": ExtractionFeatures(True,  False, False, False, False),
    "C": ExtractionFeatures(True,  True,  False, False, False),
    "D": ExtractionFeatures(True,  True,  True,  False, False),
    "E": ExtractionFeatures(True,  True,  True,  True,  False),
    "F": ExtractionFeatures(True,  True,  True,  True,  True),
}

# Full production configuration (all features enabled).
PRODUCTION_FEATURES = ABLATION_PROFILES["F"]


def resolve_profile(name: str | None) -> ExtractionFeatures:
    """Resolve a profile name to its feature object.

    None → production (all features enabled).
    Raises ValueError for unknown profile names.
    """
    if name is None:
        return PRODUCTION_FEATURES
    key = name.upper()
    if key not in ABLATION_PROFILES:
        valid = ", ".join(sorted(ABLATION_PROFILES))
        raise ValueError(
            f"Unknown ablation profile {name!r}. Valid profiles: {valid}"
        )
    return ABLATION_PROFILES[key]
