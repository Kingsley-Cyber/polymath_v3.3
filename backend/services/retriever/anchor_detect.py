"""B5 groundwork — deterministic anchor detection (owner design §5.1).

FLAG-GATED OFF (TWO_LANE_ANCHORING): nothing consumes this at query time yet.

Lexical-FIRST: a named source/author in the query is matched against the M2
metadata (documents.title / author — captured at parse since 3cbc398) with
normalized token containment. No LLM in the loop (the design's determinism
rule); the optional LLM extractor can be layered later for oblique mentions,
cached by normalized query.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

_WORD_RE = re.compile(r"[a-z0-9]+")
# generic tokens that must never anchor on their own ("the", "art", "of"…)
_GENERIC = frozenset({
    "the", "a", "an", "of", "and", "in", "on", "to", "by", "for", "with",
    "book", "books", "doc", "document", "paper", "author",
})


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _distinctive(tokens: Sequence[str]) -> list[str]:
    return [t for t in tokens if t not in _GENERIC and len(t) >= 3]


def detect_anchor_doc_ids(
    query: str,
    docs: Sequence[dict[str, Any]],
    *,
    min_tokens: int = 2,
) -> list[str]:
    """Return doc_ids whose title/author is NAMED in the query.

    Match rule (deterministic): a doc anchors when >= min_tokens of its
    distinctive title tokens appear in the query, or its distinctive author
    tokens all appear (>=1 for single-name authors of >= 5 chars). Output is
    sorted by (match strength desc, doc_id) — stable.
    """
    q_tokens = set(_tokens(query))
    scored: list[tuple[int, str]] = []
    for d in docs:
        doc_id = str(d.get("doc_id") or "")
        if not doc_id:
            continue
        title_toks = _distinctive(_tokens(str(d.get("title") or "")))
        author_toks = _distinctive(_tokens(str(d.get("author") or "")))
        t_hits = sum(1 for t in set(title_toks) if t in q_tokens)
        a_hits = sum(1 for t in set(author_toks) if t in q_tokens)
        anchored = False
        if title_toks and t_hits >= min(min_tokens, len(set(title_toks))):
            anchored = True
        if author_toks and a_hits == len(set(author_toks)) and (
            len(author_toks) >= 2 or len(author_toks[0]) >= 5
        ):
            anchored = True
        if anchored:
            scored.append((t_hits + a_hits, doc_id))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [doc_id for _, doc_id in scored]
