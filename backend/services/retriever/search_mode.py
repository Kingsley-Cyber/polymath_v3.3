"""Search mode dispatch — local vs global vs auto.

Three modes:
  - `local`: existing default. Funnel A (summaries) + Funnel B (children)
    + lexical, merge, rerank, hydrate to full text. The LLM sees verbatim
    chunk content. Best for specific questions, debugging, citations.
  - `global`: Funnel A ONLY (summaries), hydrate canonical summary text from
    Mongo, then feed summaries to the LLM without hydrating full parent text. ~50 summaries fit in
    one context window where ~5 full chunks would, so this powers
    "what are the main themes?" type corpus-level queries that the
    local path can't answer at all (it returns evidence, never overview).
  - `auto`: backend infers from the query shape. Default bias toward
    `local` (safer — wrong-direction global on a specific question
    returns vague overviews; wrong-direction local on a thematic
    question returns a few chunks instead of overview but is still
    grounded). Auto fires `infer_search_mode()`.

The shape of `infer_search_mode` is heuristic, not ML. Tuning the
marker lists is the maintenance cost. When tuning, prefer false-negatives
on global (i.e., return local more often) — see "default bias toward
local" above.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

logger = logging.getLogger(__name__)

SearchMode = Literal["auto", "local", "global"]


# Phrases that signal the user wants a broad, thematic, corpus-level
# answer. The LLM should read N summaries, not M full chunks. Most
# entries are full phrases (multi-token), not single words, to reduce
# false positives — "the main idea" is global; bare "main" is not.
_GLOBAL_MARKERS: tuple[str, ...] = (
    "summarize",
    "summary of",
    "overview",
    "overall",
    "main themes",
    "main ideas",
    "key themes",
    "key ideas",
    "what do i have",
    "what's in my",
    "what is in my",
    "what are the main",
    "what are the key",
    "what are the major",
    "high level",
    "high-level",
    "big picture",
    "at a glance",
    "corpus map",
    "across all",
    "patterns across",
    "compare all",
    "common themes",
    "recurring",
    "tldr",
    "tl;dr",
    "synthesize",
    "what topics",
    "what subjects",
)


# Phrases that signal the user wants specific evidence, exact code,
# verbatim citation, or targeted debugging. These OVERRIDE global
# markers — a query like "summarize how Humanoid:MoveTo works" goes
# local because the entity reference + "how" is more specific than
# "summarize" is broad.
_LOCAL_MARKERS: tuple[str, ...] = (
    "how does",
    "how do i",
    "how can i",
    "how to",
    "find where",
    "find the",
    "where is",
    "where does",
    "which file",
    "which line",
    "show me the",
    "show me where",
    "exact",
    "exactly",
    "verbatim",
    "quote",
    "cite",
    "citation",
    "source of",
    "bug",
    "error",
    "fix",
    "debug",
    "specific",
    "implement",
    "what does this",
    "what is the call",
    "line number",
)


# Code-entity regex — a word starting with uppercase followed by more
# alphanumerics, optionally with a dot. Catches "Humanoid", "TweenService",
# "Humanoid.MoveTo", "Motor6D". Strong signal the user is asking about
# a specific symbol, not a broad theme.
#
# Stop-list: words that match the regex but aren't really code entities
# in our domain — "Auto" / "Roblox" / "Luau" / etc. could appear in
# global-shaped queries (e.g. "summarize the Roblox corpus"). Subtracting
# them from the entity-detection signal keeps auto-detect honest.
_CODE_ENTITY_RE = re.compile(r"\b[A-Z][A-Za-z0-9]+(?:\.[A-Za-z][A-Za-z0-9]*)*\b")
_CODE_ENTITY_STOPLIST: frozenset[str] = frozenset({
    "Auto", "Roblox", "Luau", "Lua", "Polymath", "Github", "OK",
    "Mongo", "Neo4j", "Qdrant", "FastAPI", "Cypher", "TLDR", "API",
    "RAG", "LLM", "AI", "URL", "JSON", "YAML", "HTML", "CSS",
    "I", "Im", "Id", "Ive", "Ill",
})


def _count_phrase_hits(text: str, markers: tuple[str, ...]) -> int:
    """Count how many marker phrases appear in the lowercased text.
    Each marker counts at most once even if it appears multiple times
    — we want signal presence, not signal density."""
    return sum(1 for m in markers if m in text)


def _has_code_entity(query: str) -> bool:
    """True iff the query contains at least one capitalized-identifier
    token that isn't on the stoplist."""
    for match in _CODE_ENTITY_RE.finditer(query):
        token = match.group(0)
        if token in _CODE_ENTITY_STOPLIST:
            continue
        # Single-letter "I" / "A" — common pronouns, skip
        if len(token) <= 1:
            continue
        # All-uppercase 2-3 char acronyms in stoplist already; everything
        # else (Humanoid, MoveTo, TweenService) is a real code entity.
        return True
    return False


def infer_search_mode(query: str) -> SearchMode:
    """Pick local vs global from query shape. Never returns "auto" —
    "auto" is the upstream flag value that triggers this function.

    Algorithm:
      1. Count global markers, count local markers
      2. Code-entity mention adds +2 to local score (strong signal)
      3. If global beats local strictly, return "global"
      4. Otherwise (tie or local wins), return "local" — safer default
    """
    if not query:
        return "local"
    q = query.lower().strip()
    if not q:
        return "local"

    global_score = _count_phrase_hits(q, _GLOBAL_MARKERS)
    local_score = _count_phrase_hits(q, _LOCAL_MARKERS)

    if _has_code_entity(query):  # case-sensitive check on raw query
        local_score += 2

    chosen: SearchMode = "global" if global_score > local_score else "local"
    logger.debug(
        "infer_search_mode: query=%r global=%d local=%d chosen=%s",
        q[:80], global_score, local_score, chosen,
    )
    return chosen


def resolve_search_mode(requested: SearchMode | str | None, query: str) -> SearchMode:
    """Apply the user-facing dispatch.

    Policy (tier-authoritative): the user's retrieval tier drives the work, and
    GLOBAL is an explicit, user-selected mode (the 50-summary overview) — never
    auto-inferred. "global" is honored only when explicitly requested; every
    other value (the "Default", legacy "auto", or anything unknown) resolves to
    "local" so a query is never silently inflated into the heavier global path.

    `infer_search_mode` is retained for callers that opt into heuristic
    inference, but it is no longer triggered by the default mode.
    """
    val = (requested or "local").lower().strip()
    if val == "global":
        return "global"
    # "local", "default", legacy "auto", and unknowns all → local.
    return "local"
