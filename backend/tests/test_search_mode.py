"""Phase 27 — search_mode dispatch unit tests.

Pins the auto-detect heuristic so future tuning is intentional.
Edge cases: code entity overrides, marker phrase counting, the
default-toward-local safety bias, the resolve_search_mode wrapper.
"""

from __future__ import annotations

import pytest

from services.retriever.search_mode import (
    _CODE_ENTITY_STOPLIST,
    _GLOBAL_MARKERS,
    _LOCAL_MARKERS,
    _has_code_entity,
    infer_search_mode,
    resolve_search_mode,
)


# ─── Global queries ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "summarize my corpus",
        "what are the main themes",
        "give me an overview of the codebase",
        "what's in my library",
        "patterns across the documents",
        "compare all the design docs",
        "TLDR on the spider rigging system",
    ],
)
def test_global_queries_route_to_global(query):
    assert infer_search_mode(query) == "global"


# ─── Local queries ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query",
    [
        "how does the locomotion code work",
        "find where TweenService is used",
        "which file contains the rigging logic",
        "show me the exact line for MoveTo",
        "cite the source for joint constraints",
        "debug the network sync bug",
    ],
)
def test_local_queries_route_to_local(query):
    assert infer_search_mode(query) == "local"


# ─── Code entity overrides ─────────────────────────────────────────────────


def test_code_entity_forces_local_over_summarize():
    """A query that mixes 'summarize' (global) with a code entity
    should route LOCAL — the entity is a stronger specific signal."""
    assert infer_search_mode("summarize how Humanoid:MoveTo works") == "local"


def test_code_entity_alone_routes_local():
    """A bare entity-name query with no markers either way."""
    assert infer_search_mode("Humanoid") == "local"


def test_stoplist_words_dont_force_local():
    """A capitalized word on the stoplist (Roblox, Auto, Polymath) is
    NOT a code entity and shouldn't force local routing."""
    # "summarize my Roblox library" should still go global because
    # "Roblox" is stop-listed.
    assert infer_search_mode("summarize my Roblox library") == "global"
    assert not _has_code_entity("summarize my Roblox library")


def test_camelcase_method_call_counts_as_entity():
    assert _has_code_entity("how does humanoid:MoveTo work")
    assert _has_code_entity("call Animation.Play")


def test_short_pronouns_dont_count_as_entity():
    """Single-letter capitalized tokens (I, A) shouldn't trigger
    the code-entity heuristic."""
    assert not _has_code_entity("I want a summary")


# ─── Default bias toward local ─────────────────────────────────────────────


def test_empty_query_returns_local():
    assert infer_search_mode("") == "local"


def test_whitespace_query_returns_local():
    assert infer_search_mode("   \n\t  ") == "local"


def test_tied_markers_break_toward_local():
    """1 global marker + 1 local marker → local wins the tie.
    Using "summarize" (global) + "how does" (local) to construct an
    actual 1-vs-1 tie."""
    q = "summarize how does this work"  # "summarize" + "how does"
    # local wins ties (safer fallback per the docstring)
    assert infer_search_mode(q) == "local"


def test_no_markers_at_all_returns_local():
    """A short ambiguous query with no markers should fall to local."""
    assert infer_search_mode("the spider rig") == "local"


# ─── resolve_search_mode wrapper ───────────────────────────────────────────


def test_resolve_local_passes_through():
    assert resolve_search_mode("local", "any query") == "local"


def test_resolve_global_passes_through():
    assert resolve_search_mode("global", "any query") == "global"


def test_resolve_auto_resolves_to_local_not_inferred():
    """'auto' no longer infers global — it resolves to local so the tier drives
    the work. (infer_search_mode is still unit-tested directly above for callers
    that opt into heuristic inference; it just isn't wired to the default mode.)"""
    assert resolve_search_mode("auto", "summarize all my docs") == "local"
    assert resolve_search_mode("auto", "find Humanoid:MoveTo") == "local"


def test_resolve_none_defaults_to_local():
    """Tier-authoritative policy: None / empty / legacy 'auto' all resolve to
    local. Global is NEVER auto-inferred — it is an explicit user choice only,
    so an overview-shaped query stays local unless 'global' is requested."""
    assert resolve_search_mode(None, "summarize all themes") == "local"
    assert resolve_search_mode("", "how does X work") == "local"
    assert resolve_search_mode("auto", "summarize all themes") == "local"


def test_resolve_unknown_mode_falls_back_to_local():
    """Defensive — bad client input doesn't accidentally enable global."""
    assert resolve_search_mode("magical_mode", "summarize all") == "local"


def test_resolve_case_insensitive():
    assert resolve_search_mode("GLOBAL", "any") == "global"
    assert resolve_search_mode("Local", "any") == "local"
    # 'Auto' is no longer inferred to global — it resolves to local now.
    assert resolve_search_mode("Auto", "summarize all themes") == "local"


# ─── Marker list sanity ────────────────────────────────────────────────────


def test_marker_lists_are_non_overlapping():
    """A phrase shouldn't appear in BOTH global and local markers —
    that would make scoring ambiguous and tuning impossible."""
    global_set = set(_GLOBAL_MARKERS)
    local_set = set(_LOCAL_MARKERS)
    overlap = global_set & local_set
    assert overlap == set(), f"marker overlap: {overlap}"


def test_stoplist_has_common_false_positives():
    """The capitalized-token stoplist must include words that look like
    code entities but aren't (Roblox, Auto, Polymath, etc.)."""
    for word in ("Roblox", "Luau", "Polymath", "Mongo", "Neo4j", "Qdrant"):
        assert word in _CODE_ENTITY_STOPLIST
