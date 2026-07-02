"""Query-guided excerpt selection (roadmap B2 — CPU, no GPU cost).

When a chunk is longer than a consumer's character budget, taking the
LEADING characters often hands the consumer a window that never contains
the passage that matched the query — live probe (2026-07-01): the
Le Guin sentence-rhythm query retrieved the right book but the reranker
scored its leading 1000 chars, missing her actual rhythm passage
entirely (doc-hit / passage-miss).

``query_guided_excerpt`` slides a sentence-aligned window over the text
and picks the window that best covers the query: distinct-term coverage,
adjacent-term (phrase) hits, and multi-term proximity within a sentence.
Zero matches fall back to the legacy leading window, so behaviour is
unchanged for texts the query never touches. Pure CPU string work — the
GPU alternative (support-pass reranking) was A/B-measured at +19s p50
and rejected (see RERANK_EVIDENCE_SUPPORT).
"""

from __future__ import annotations

import re

from services.retriever.query_semantics import LEXICAL_STOP_WORDS, query_tokens

# Sentence boundaries: end punctuation + whitespace, or paragraph breaks.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")

# Window scoring weights. Distinct-term coverage dominates (an excerpt
# containing more of the query's vocabulary beats one repeating a single
# term); phrase adjacency and same-sentence proximity break ties toward
# passages that USE the terms together rather than merely mentioning them.
_TERM_WEIGHT = 2.0
_PHRASE_WEIGHT = 3.0
_PROXIMITY_WEIGHT = 1.0


def _sentences_with_offsets(text: str) -> list[tuple[int, int]]:
    """Return (start, end) offsets of sentences in ``text``."""
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _SENTENCE_SPLIT.finditer(text):
        end = match.start()
        if end > pos:
            spans.append((pos, end))
        pos = match.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return spans


def query_guided_excerpt(text: str, query: str, *, max_chars: int) -> str:
    """Best ``max_chars`` window of ``text`` for ``query``.

    Falls back to the leading window when the text fits the budget, the
    query yields no lexical terms, or no sentence matches any term.
    """
    if not text or len(text) <= max_chars:
        return text
    terms = query_tokens(query or "", stop_words=LEXICAL_STOP_WORDS)
    if not terms:
        return text[:max_chars]
    term_set = set(terms)
    # Adjacent query-term pairs, in query order, as phrase signals.
    bigrams = {
        f"{a} {b}" for a, b in zip(terms, terms[1:]) if a != b
    }

    spans = _sentences_with_offsets(text)
    if not spans:
        return text[:max_chars]

    # Pre-score each sentence: which terms it contains, bigram hits, and
    # whether multiple distinct terms co-occur in it (proximity).
    sent_terms: list[set[str]] = []
    sent_bigrams: list[int] = []
    lowered = text.lower()
    for start, end in spans:
        sentence = lowered[start:end]
        present = {t for t in term_set if t in sentence}
        sent_terms.append(present)
        sent_bigrams.append(sum(1 for bg in bigrams if bg in sentence))

    best_score = 0.0
    best_range: tuple[int, int] | None = None
    n = len(spans)
    for i in range(n):
        window_terms: set[str] = set()
        bigram_hits = 0
        proximity = 0
        j = i
        while j < n and spans[j][1] - spans[i][0] <= max_chars:
            window_terms |= sent_terms[j]
            bigram_hits += sent_bigrams[j]
            if len(sent_terms[j]) >= 2:
                proximity += 1
            j += 1
        if j == i:
            # Single sentence longer than the budget — score it alone.
            window_terms = set(sent_terms[i])
            bigram_hits = sent_bigrams[i]
            proximity = 1 if len(sent_terms[i]) >= 2 else 0
            j = i + 1
        score = (
            _TERM_WEIGHT * len(window_terms)
            + _PHRASE_WEIGHT * bigram_hits
            + _PROXIMITY_WEIGHT * proximity
        )
        if score > best_score:
            best_score = score
            best_range = (spans[i][0], min(spans[j - 1][1], spans[i][0] + max_chars))

    if best_range is None or best_score <= 0.0:
        return text[:max_chars]
    start, end = best_range
    return text[start:end]
