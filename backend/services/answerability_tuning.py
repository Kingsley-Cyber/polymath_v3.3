"""Single source of truth for the answerability / relationship gate knobs.

Polymath has TWO independent answerability gates that must agree or one will
refuse what the other admits:

  * retriever-side  — services.retriever.ranking_policy._evaluate_sufficiency
  * chat-side       — services.chat_orchestrator._build_retrieval_answerability_gate

Both read their relationship strictness and coverage thresholds from here, so a
single Settings change moves both in lockstep.

Design intent (per product direction): the corpus supplies the FACTS; the LLM
supplies the BRIDGE. A "how does X relate to Y" question should answer whenever
each side has at least one retrieved source — the model is trusted to connect
them with its own reasoning rather than demanding the corpus pre-contain an
explicit cross-document link. The honesty floor is preserved structurally: a
side with ZERO retrieved evidence still surfaces as a missing concept lane and
refuses (you cannot bridge what was never retrieved). Only the relationship
*bridge* atoms are softened here; ``definition`` and ``procedure`` stay critical
because those genuinely require grounded source text.
"""

from __future__ import annotations

from config import get_settings

# The synthetic atom injected when a multi-concept relationship query lacks an
# explicit cross-document link in the evidence.
CROSS_DOCUMENT_RELATIONSHIP_ATOM = "cross_document_relationship_evidence"

# Relationship-FAMILY atoms: the bridge atom plus the bare "relationship"
# operator atom. In lenient/off mode neither forces a refusal — the LLM bridges.
RELATIONSHIP_FAMILY_ATOMS = frozenset(
    {"relationship", CROSS_DOCUMENT_RELATIONSHIP_ATOM}
)

_VALID_GATES = ("off", "lenient", "strict")


def _clamp(value: object, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def relationship_gate() -> str:
    """off | lenient | strict. Default 'lenient' (LLM bridges retrieved sides)."""
    val = str(getattr(get_settings(), "RELATIONSHIP_GATE", "lenient") or "lenient").lower()
    return val if val in _VALID_GATES else "lenient"


def rerank_evidence_support() -> bool:
    """Cross-encoder rerank for evidence-plan support retrievals (default OFF).

    Passage-level precision knob, A/B validated 2026-07-01: ON makes the
    Le Guin rhythm probe quote the actual passage instead of just landing in
    the right book — but support rerank contends with the embedder on the
    single Metal GPU and retrieval-phase p50 went ~12s -> ~31s. Opt in for
    quality-first sessions via RERANK_EVIDENCE_SUPPORT=true. Coverage support
    retrievals are never governed by this knob."""
    return bool(getattr(get_settings(), "RERANK_EVIDENCE_SUPPORT", False))


def relationship_min_distinct_docs() -> int:
    """Distinct docs across relationship lanes before the cross-doc atom counts
    as covered. Default 1 (a single distinct doc satisfies the bridge)."""
    return max(1, int(getattr(get_settings(), "RELATIONSHIP_MIN_DISTINCT_DOCS", 1) or 1))


def relationship_lane_min_sources() -> int:
    """Distinct STRONG docs each relationship lane needs to be 'covered'.
    Default 1 — a side backed by one strong doc is enough (>=1 doc per side
    answers); a side with zero evidence stays missing and refuses honestly."""
    return max(1, int(getattr(get_settings(), "RELATIONSHIP_LANE_MIN_SOURCES", 1) or 1))


def lane_strong_score() -> int:
    """Minimum evidence_lane_match_score for a chunk to count toward lane
    coverage. Default 8 (unchanged); lower to admit weaker alias/term matches."""
    return max(1, int(getattr(get_settings(), "LANE_STRONG_SCORE", 8) or 8))


def coverage_threshold() -> float:
    """Required-atom coverage to answer without the text-help branch. Default
    0.80; clamped to a sane band so a bad .env can't disable the gate."""
    return _clamp(getattr(get_settings(), "ANSWERABILITY_COVERAGE_THRESHOLD", 0.80), 0.40, 0.95, 0.80)


def text_help_threshold() -> float:
    """Coverage floor for the lexical text-help answer branch. Default 0.50."""
    return _clamp(getattr(get_settings(), "ANSWERABILITY_TEXT_HELP_THRESHOLD", 0.50), 0.30, 0.80, 0.50)


def partial_floor() -> float:
    """Coverage boundary between 'partial' (caveat answer) and 'weak'/refuse,
    and the floor for the relationship carve-out. Default 0.50."""
    return _clamp(getattr(get_settings(), "ANSWERABILITY_PARTIAL_FLOOR", 0.50), 0.20, 0.70, 0.50)


def inject_cross_doc_atom() -> bool:
    """Whether to track the cross-doc bridge atom at all. 'off' skips it; lenient
    and strict still record it (required, for coverage accounting + caveat)."""
    return relationship_gate() != "off"


def cross_doc_atom_is_critical() -> bool:
    """Only in 'strict' mode does the cross-doc bridge atom block answering."""
    return relationship_gate() == "strict"


def neutralize_relationship_critical(critical: set[str] | frozenset[str]) -> set[str]:
    """Drop relationship-family atoms from a critical set unless gate is strict.

    definition / procedure / concept:* lanes are left untouched — only the
    relationship bridge is softened, so a genuinely ungrounded side or an
    unmet definition still refuses.
    """
    if relationship_gate() == "strict":
        return set(critical)
    return {a for a in critical if a not in RELATIONSHIP_FAMILY_ATOMS}


def missing_is_relationship_only(missing_critical: list[str] | set[str]) -> bool:
    """True when every remaining critical-miss is a relationship-family atom —
    the condition under which the status ladder answers 'partial' not refuse."""
    items = {str(a) for a in (missing_critical or [])}
    return bool(items) and items <= set(RELATIONSHIP_FAMILY_ATOMS)
