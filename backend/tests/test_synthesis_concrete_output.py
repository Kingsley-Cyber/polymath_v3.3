"""Phase 1.B6 — synthesis prompt regression: assert no shell phrases.

These tests check the PROMPT INPUT to the LLM (deterministic, no LLM call).
They verify that the predicate sanitizer + edge-section rewrite + system
prompt rewrite collectively guarantee that:

  1. No internal Neo4j labels (RELATES_TO, MENTIONS, PART_OF, ...) leak
     into the user prompt edge section.
  2. The system prompt explicitly forbids the shell phrases that the
     old prompt produced ("the graph suggests", "a relationship exists",
     "RELATES_TO").
  3. The edge section uses natural-language predicates and bold subject /
     object names.

If any of these fail, the LLM has higher odds of regressing to shell
output. The fix is to update the sanitizer / prompt — NEVER to update
the assertion to be more permissive.
"""

from __future__ import annotations

from services.graph.orchestrator import (
    _SYNTHESIS_SYSTEM_PROMPT,
    _humanize_predicate,
    _render_packet_user_prompt,
)


# ─── Sanitizer unit tests ───────────────────────────────────────────────────


def test_humanize_predicate_maps_relates_to():
    assert _humanize_predicate("RELATES_TO") == "co-occurs with"


def test_humanize_predicate_maps_mentions():
    assert _humanize_predicate("MENTIONS") == "appears alongside"


def test_humanize_predicate_maps_part_of():
    assert _humanize_predicate("PART_OF") == "is part of"
    # lowercase canonical form should also map
    assert _humanize_predicate("part_of") == "is part of"


def test_humanize_predicate_uses_natural_uses():
    assert _humanize_predicate("USES") == "uses"
    assert _humanize_predicate("uses") == "uses"


def test_humanize_predicate_fallback_for_unknown():
    """Unknown predicates get a soft snake→space conversion, never raw."""
    out = _humanize_predicate("MAGICAL_RELATION")
    assert "MAGICAL_RELATION" not in out
    assert out == "magical relation"


def test_humanize_predicate_handles_none_and_empty():
    assert _humanize_predicate(None) == "is connected to"
    assert _humanize_predicate("") == "is connected to"


# ─── Edge section rendering tests ───────────────────────────────────────────


def _packet_with_edges(predicates: list[str]) -> dict:
    """Build a minimal packet with the given raw predicates on its edges."""
    return {
        "q": "test query",
        "anchors": [],
        "groups": [],
        "documents_in_scope": [],
        "evidence": [],
        "edges": [
            {
                "s": "Humanoid",
                "p": pred,
                "t": "Animation",
                "family": "Operational",
                "conf": 0.85,
                "rationale": "The spider rig uses a Humanoid to drive animation.",
            }
            for pred in predicates
        ],
    }


def test_edge_section_strips_relates_to():
    """RELATES_TO must NEVER appear in the rendered prompt."""
    prompt = _render_packet_user_prompt(_packet_with_edges(["RELATES_TO"]))
    assert "RELATES_TO" not in prompt
    assert "co-occurs with" in prompt


def test_edge_section_strips_mentions():
    prompt = _render_packet_user_prompt(_packet_with_edges(["MENTIONS"]))
    assert "MENTIONS" not in prompt
    assert "appears alongside" in prompt


def test_edge_section_strips_all_caps_underscored_labels():
    """Any all-caps underscored relation label should be humanized."""
    raw = ["RELATES_TO", "MENTIONS", "PART_OF", "DEPENDS_ON", "PRODUCES"]
    prompt = _render_packet_user_prompt(_packet_with_edges(raw))
    for label in raw:
        assert label not in prompt, f"Internal label {label!r} leaked into prompt"


def test_edge_section_bolds_subject_and_object():
    """Subjects and objects render as **bold** so the LLM mirrors that pattern."""
    prompt = _render_packet_user_prompt(_packet_with_edges(["USES"]))
    assert "**Humanoid**" in prompt
    assert "**Animation**" in prompt


def test_edge_section_includes_code_metadata_when_present():
    """When an edge carries code_metadata, file_path + symbols_called surface."""
    packet = _packet_with_edges(["USES"])
    packet["edges"][0]["code_metadata"] = {
        "file_path": "spider.luau",
        "symbols_called": ["Humanoid.MoveTo", "WaitForChild"],
    }
    prompt = _render_packet_user_prompt(packet)
    assert "spider.luau" in prompt
    assert "Humanoid.MoveTo" in prompt


def test_edge_section_uses_structured_mechanism_source_format():
    """B3 follow-up — edge rendering uses structured fields:
       mechanism: "..."
       source: [file:line]
       symbols: a, b, c
    instead of the older single-line italic mash-up."""
    packet = _packet_with_edges(["USES"])
    packet["edges"][0]["code_metadata"] = {
        "file_path": "spider.luau",
        "line_number": 14,
        "symbols_called": ["Humanoid.MoveTo", "WaitForChild"],
    }
    prompt = _render_packet_user_prompt(packet)
    # Structured field labels MUST appear so the LLM mirrors them.
    assert "mechanism:" in prompt
    assert "source:" in prompt
    assert "symbols:" in prompt
    # The file path and line number must be rendered in [path:line] form.
    assert "[spider.luau:14]" in prompt


def test_edge_source_falls_back_when_no_line_number():
    """When code_metadata.line_number is missing, source: still renders
    the file path in brackets (no `:None`, no missing closing bracket)."""
    packet = _packet_with_edges(["USES"])
    packet["edges"][0]["code_metadata"] = {
        "file_path": "spider.luau",
        "symbols_called": ["Humanoid"],
    }
    prompt = _render_packet_user_prompt(packet)
    assert "[spider.luau]" in prompt
    assert "[spider.luau:None]" not in prompt
    assert "[spider.luau:0]" not in prompt


# ─── System prompt regression tests ─────────────────────────────────────────


def test_system_prompt_forbids_relates_to_output():
    """The system prompt must contain an explicit instruction against
    parroting RELATES_TO into the synthesis."""
    assert "RELATES_TO" in _SYNTHESIS_SYSTEM_PROMPT
    # The instruction must be a NEGATION ("NEVER output") — not a citation
    assert "NEVER output" in _SYNTHESIS_SYSTEM_PROMPT or "Never output" in _SYNTHESIS_SYSTEM_PROMPT


def test_system_prompt_forbids_shell_sentences():
    """The system prompt must explicitly forbid the empty-shell phrasing
    that the legacy prompt licensed."""
    assert "empty-shell" in _SYNTHESIS_SYSTEM_PROMPT.lower() or \
           "say nothing" in _SYNTHESIS_SYSTEM_PROMPT.lower()
    # Must mention the bad-pattern examples
    assert "a relationship exists" in _SYNTHESIS_SYSTEM_PROMPT


def test_system_prompt_demands_mechanism_over_metadata():
    """The system prompt must instruct the LLM to explain mechanisms,
    not narrate metadata."""
    assert "mechanism" in _SYNTHESIS_SYSTEM_PROMPT.lower()


def test_system_prompt_demands_greppability():
    """Every bold term should be greppable in the source chunks."""
    assert "grep" in _SYNTHESIS_SYSTEM_PROMPT.lower()


def test_system_prompt_still_forbids_invention():
    """The concrete-claim rewrite must NOT remove the do-not-invent rule.
    Both research-faithfulness AND concrete-claim demands must coexist."""
    invent_terms = ["invent", "hallucinat"]
    found = any(term in _SYNTHESIS_SYSTEM_PROMPT.lower() for term in invent_terms)
    assert found, "Prompt must forbid invention of APIs/entities/files"


# ─── Hard suppression — the actual product requirement ─────────────────────


def test_no_internal_label_in_rendered_packet_ever():
    """End-to-end: even with a worst-case packet (all 5 common Neo4j
    labels), none of them appear in the rendered prompt."""
    bad_labels = ["RELATES_TO", "MENTIONS", "PART_OF", "DEPENDS_ON",
                  "IMPLEMENTS", "PRODUCES", "REFERENCES", "CALLS"]
    packet = _packet_with_edges(bad_labels)
    prompt = _render_packet_user_prompt(packet)
    for label in bad_labels:
        assert label not in prompt, (
            f"REGRESSION: internal label {label!r} leaked into prompt. "
            f"Add it to _PREDICATE_HUMAN_MAP in orchestrator.py."
        )
