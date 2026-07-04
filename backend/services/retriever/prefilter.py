"""Q2/U2 — promoted-payload soft prefilter helpers (§11.2, owner-ratified).

Pure, dependency-free. Two jobs:

1. query_payload_terms(): derive the concept terms + entity_ids a query can
   match against the PROMOTED Qdrant payload (concepts[] = lowercase
   space-joined canonical names from promote's _norm_term; entity_ids =
   hyphen slugs per the ENTITY-ID LAW). Funnel B turns these into a
   should-filter with a DETERMINISTIC unfiltered fallback — recall is never
   stranded on unpromoted corpora.

2. query_operator() + semantic_rank_bonus(): map the query's operator shape
   (definition / comparison / procedure / causal) to preferred
   semantic_chunk_type values as a small RANK-ONLY additive bonus — never a
   multiplier, and the cross-encoder stays the scoring authority downstream.
"""

from __future__ import annotations

import re

# Same tokenizer + stopword stance as graph_payload (kept inline so this
# module stays import-light; both are frozen deterministic sets).
_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be but by for from has have how in is it its of on or "
    "that the this to was what when where which who why will with does do did "
    "can could should would about into over under between across".split()
)
_MAX_NGRAM = 3


def query_payload_terms(query: str, cap: int = 12) -> tuple[list[str], list[str]]:
    """Query → (concept_terms, entity_ids) candidates for the payload filter.

    concepts[] payload values are `_norm_term(canonical_name)` = lowercase,
    whitespace-collapsed (spaces kept); entity_ids are `entity:{hyphen-slug}`.
    Deterministic: n-gram length desc, then query position. Unigrams must be
    non-stopword and >=3 chars; multigrams may not start/end on a stopword.
    """
    tokens = [t for t in _TOKEN_RE.split((query or "").lower()) if t]
    if not tokens:
        return [], []
    terms: list[str] = []
    seen: set[str] = set()
    for n in range(min(_MAX_NGRAM, len(tokens)), 0, -1):
        for i in range(len(tokens) - n + 1):
            gram = tokens[i : i + n]
            if n == 1 and (gram[0] in _STOPWORDS or len(gram[0]) < 3):
                continue
            if n > 1 and (gram[0] in _STOPWORDS or gram[-1] in _STOPWORDS):
                continue
            term = " ".join(gram)
            if term not in seen:
                seen.add(term)
                terms.append(term)
                if len(terms) >= cap:
                    break
        if len(terms) >= cap:
            break
    entity_ids = [f"entity:{t.replace(' ', '-')}" for t in terms]
    return terms, entity_ids


# Operator detection — precedence is fixed and deliberate: an explicit
# comparison marker outranks a "what is" opener ("what is the difference
# between X and Y" is a comparison, not a definition).
_OPERATOR_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("comparison", (
        "compare", " vs ", " vs.", "versus", "difference between",
        "differences between", "differ from", "distinguish",
    )),
    ("definition", (
        "what is ", "what are ", "define ", "definition of", "meaning of",
        "what does", " mean?", " means?",
    )),
    ("procedure", (
        "how do i ", "how to ", "steps to", "steps for", "process of",
        "procedure", "walk me through", "guide to",
    )),
    ("causal", (
        "why ", "cause of", "causes ", "caused by", "lead to", "leads to",
        "effect of", "effects of", "impact of", "result of", "because of",
    )),
)

SEMANTIC_TYPE_PREFERENCE: dict[str, frozenset[str]] = {
    "definition": frozenset({"definition", "principle"}),
    "comparison": frozenset({"comparison", "framework"}),
    "procedure": frozenset({"procedure", "example"}),
    "causal": frozenset({"claim", "principle"}),
}


def query_operator(query: str) -> str | None:
    """Deterministic operator shape of the query, or None."""
    text = " " + " ".join((query or "").lower().split()) + " "
    if not text.strip():
        return None
    for operator, markers in _OPERATOR_MARKERS:
        if any(m in text for m in markers):
            return operator
    return None


def semantic_rank_bonus(
    operator: str | None,
    semantic_chunk_type: str | None,
    bonus: float = 0.03,
) -> float:
    """RANK-ONLY additive bonus when the chunk's semantic type matches the
    query operator's preference. Never a multiplier; 0.0 on any miss."""
    if not operator or not semantic_chunk_type or bonus <= 0:
        return 0.0
    preferred = SEMANTIC_TYPE_PREFERENCE.get(operator)
    if preferred and str(semantic_chunk_type) in preferred:
        return float(bonus)
    return 0.0
