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

import re
from collections.abc import Iterable

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

_DISTINCTIVE_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*")
_DISTINCTIVE_MIN_LENGTH = 8
_DISTINCTIVE_EXCLUSIONS = frozenset(
    {
        "about",
        "answer",
        "between",
        "corpus",
        "could",
        "document",
        "documents",
        "explain",
        "information",
        "question",
        "selected",
        "should",
        "source",
        "sources",
        "that",
        "these",
        "this",
        "those",
        "together",
        "what",
        "when",
        "where",
        "which",
        "would",
    }
)


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


def coverage_threshold(answer_shape: str | None = None) -> float:
    """Required-atom coverage to answer without the text-help branch. Default
    0.80; clamped to a sane band so a bad .env can't disable the gate.

    P0.4 — sufficiency is calibrated by answer SHAPE, not one universal
    threshold: broad synthesis legitimately draws on partial coverage of many
    concepts and enumerations/comparisons tolerate a missing minor facet,
    while single-fact/definition questions keep the strict default. Shape
    modifiers key on the query's shape, never its content, and the base stays
    env-tunable."""

    base = _clamp(
        getattr(get_settings(), "ANSWERABILITY_COVERAGE_THRESHOLD", 0.80),
        0.40,
        0.95,
        0.80,
    )
    shape = str(answer_shape or "").strip().lower()
    if shape in {"broad_synthesis", "synthesis", "broad"}:
        return max(0.40, round(base - 0.20, 4))
    if shape in {"enumeration", "comparison"}:
        return max(0.40, round(base - 0.10, 4))
    return base


def text_help_threshold() -> float:
    """Coverage floor for the lexical text-help answer branch. Default 0.50."""
    return _clamp(getattr(get_settings(), "ANSWERABILITY_TEXT_HELP_THRESHOLD", 0.50), 0.30, 0.80, 0.50)


def partial_floor() -> float:
    """Coverage boundary between 'partial' (caveat answer) and 'weak'/refuse,
    and the floor for the relationship carve-out. Default 0.50."""
    return _clamp(getattr(get_settings(), "ANSWERABILITY_PARTIAL_FLOOR", 0.50), 0.20, 0.70, 0.50)


def corpus_scope_v2_enabled() -> bool:
    """Whether the chat-only corpus-scope arbiter policy is active.

    This flag is intentionally not consumed by retriever ranking or repair.
    ``_evaluate_sufficiency`` stays strict and unchanged; v2 only constrains
    when the chat arbiter may loosen that upstream result.
    """

    return bool(getattr(get_settings(), "ANSWERABILITY_CORPUS_SCOPE_V2_ENABLED", False))


def answerability_policy_version() -> str:
    """Stable policy identity for traces and A/B receipts."""

    return "corpus_scope.v2" if corpus_scope_v2_enabled() else "baseline_live_v0"


def _scope_term(value: object) -> str:
    text = str(value or "").casefold().replace("-", " ")
    text = re.sub(r"(?:'s|’s)\b", "", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def distinctive_query_terms(query: str | None) -> tuple[str, ...]:
    """Return a conservative, domain-neutral query scope signature.

    A term is distinctive when its surface form is acronym/proper-name-like,
    hyphenated, or long enough to be unlikely question scaffolding. Shared
    request-shape exclusions remove corpus/query scaffolding. This deliberately
    requires no corpus vocabulary and contains no eval- or domain-specific
    terms.
    """

    tokens = list(_DISTINCTIVE_TOKEN_RE.finditer(str(query or "")))
    selected: list[str] = []
    seen: set[str] = set()
    for index, match in enumerate(tokens):
        raw = match.group(0)
        term = _scope_term(raw)
        compact = term.replace(" ", "")
        if not term or compact in _DISTINCTIVE_EXCLUSIONS:
            continue
        letters = re.sub(r"[^A-Za-z]", "", raw)
        acronym_like = len(letters) >= 2 and letters.isupper()
        proper_like = index > 0 and len(compact) >= 3 and raw[0].isupper()
        compound_like = "-" in raw
        long_like = len(compact) >= _DISTINCTIVE_MIN_LENGTH
        if not (acronym_like or proper_like or compound_like or long_like):
            continue
        if term not in seen:
            selected.append(term)
            seen.add(term)
    return tuple(selected)


def corpus_scope_v2_support(
    query: str | None,
    source_texts: Iterable[object],
) -> dict[str, object]:
    """Measure distinctive query coverage in the final retrieved packet.

    The guard is eligible only for a real signature of at least two terms.
    It does not decide answerability by itself; the chat arbiter invokes it
    only when the strict retriever result was already unanswerable and the
    legacy chat policy would otherwise loosen that decision.
    """

    terms = distinctive_query_terms(query)
    try:
        min_terms = max(
            2,
            min(
                8,
                int(
                    getattr(
                        get_settings(),
                        "ANSWERABILITY_CORPUS_SCOPE_V2_MIN_TERMS",
                        2,
                    )
                    or 2
                ),
            ),
        )
    except (TypeError, ValueError):
        min_terms = 2
    min_coverage = _clamp(
        getattr(
            get_settings(),
            "ANSWERABILITY_CORPUS_SCOPE_V2_MIN_COVERAGE",
            0.60,
        ),
        0.25,
        1.0,
        0.60,
    )
    packet = _scope_term(" ".join(str(value or "") for value in source_texts))
    padded = f" {packet} "
    matched = [term for term in terms if f" {term} " in padded]
    coverage = len(matched) / len(terms) if terms else 1.0
    eligible = len(terms) >= min_terms
    return {
        "policy_version": answerability_policy_version(),
        "enabled": corpus_scope_v2_enabled(),
        "eligible": eligible,
        "distinctive_terms": list(terms),
        "matched_terms": matched,
        "missing_terms": [term for term in terms if term not in set(matched)],
        "coverage": round(coverage, 4),
        "min_terms": min_terms,
        "min_coverage": round(min_coverage, 4),
        "supported": (not eligible) or coverage >= min_coverage,
    }


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
