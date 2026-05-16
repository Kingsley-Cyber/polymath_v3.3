"""Phase 3.A5/A6 — ideation prompt structural regression tests.

These verify that the _IDEATION_SYSTEM_PROMPT carries the hard constraints
that prevent ideation from degrading into hallucinated APIs / shell
sentences, and that the synthesis_mode dispatch correctly picks the
right system prompt.

Without these guards, ideation mode would license the LLM to invent
APIs that the user can't grep — which is exactly the failure mode the
research mode was carefully designed to prevent.
"""

from __future__ import annotations

from services.graph.orchestrator import (
    _IDEATION_SYSTEM_PROMPT,
    _NUANCE_SYSTEM_PROMPT,
    _SYNTHESIS_SYSTEM_PROMPT,
    _system_prompt_for_synthesis_mode,
)


# ─── Existence + identity ───────────────────────────────────────────────────


def test_ideation_prompt_exists_and_distinct():
    """Ideation prompt exists, is non-empty, and is NOT the research prompt."""
    assert isinstance(_IDEATION_SYSTEM_PROMPT, str)
    assert len(_IDEATION_SYSTEM_PROMPT) > 500
    assert _IDEATION_SYSTEM_PROMPT != _SYNTHESIS_SYSTEM_PROMPT


def test_nuance_prompt_exists_and_distinct():
    """Nuance prompt exists and is distinct from research and ideation."""
    assert isinstance(_NUANCE_SYSTEM_PROMPT, str)
    assert len(_NUANCE_SYSTEM_PROMPT) > 500
    assert _NUANCE_SYSTEM_PROMPT != _SYNTHESIS_SYSTEM_PROMPT
    assert _NUANCE_SYSTEM_PROMPT != _IDEATION_SYSTEM_PROMPT


def test_ideation_prompt_advertises_build_idea_format():
    """The output format must direct the LLM to emit [BUILD IDEA] blocks."""
    assert "[BUILD IDEA]" in _IDEATION_SYSTEM_PROMPT


def test_ideation_prompt_has_required_sections():
    """The Hook / Mechanic / Corpus Evidence / Build Path / Feasibility /
    Risk sections must be specified so the LLM produces consistent output."""
    required_sections = [
        "The Hook",
        "The Mechanic",
        "Corpus Evidence",
        "Build Path",
        "Feasibility",
        "Risk",
    ]
    for section in required_sections:
        assert section in _IDEATION_SYSTEM_PROMPT, (
            f"Ideation prompt missing required section: {section!r}"
        )


# ─── Hard constraints — preserve research-faithfulness ──────────────────────


def test_ideation_forbids_invented_apis():
    """Ideation mode MUST NOT license API/method/file invention."""
    assert "NEVER invent" in _IDEATION_SYSTEM_PROMPT
    # Must mention what specifically cannot be invented
    forbidden_invention_targets = ["APIs", "method", "file"]
    for target in forbidden_invention_targets:
        assert target in _IDEATION_SYSTEM_PROMPT, (
            f"Ideation prompt should forbid inventing {target!r}"
        )


def test_ideation_demands_greppability():
    """Every bold term in the output must appear in the evidence packet
    so the user can grep for it. This is the single most important rule."""
    assert "grep" in _IDEATION_SYSTEM_PROMPT.lower()
    assert "verbatim" in _IDEATION_SYSTEM_PROMPT.lower()


def test_ideation_proposed_api_escape_hatch():
    """When the LLM thinks a new API is needed, it must label it
    [PROPOSED API] rather than silently inventing a real-sounding name."""
    assert "[PROPOSED API]" in _IDEATION_SYSTEM_PROMPT


def test_ideation_forbids_neo4j_labels():
    """Same RELATES_TO / MENTIONS guard as research mode."""
    assert "RELATES_TO" in _IDEATION_SYSTEM_PROMPT
    assert "NEVER output" in _IDEATION_SYSTEM_PROMPT


def test_ideation_forbids_shell_sentences():
    """Same empty-shell guard as research mode."""
    # Either of these markers proves the guard is present
    assert (
        "empty-shell" in _IDEATION_SYSTEM_PROMPT.lower()
        or "a relationship exists" in _IDEATION_SYSTEM_PROMPT
    )


# ─── Creative license — what distinguishes ideation from research ───────────


def test_ideation_licenses_synthesis_combinations():
    """Unlike research mode, ideation MAY connect evidence items that
    the corpus never explicitly connects — but only when labeled."""
    assert "[SYNTHESIS]" in _IDEATION_SYSTEM_PROMPT


def test_ideation_reframes_gaps_as_opportunities():
    """The whole point of ideation: gap → opportunity, not gap → deficit."""
    assert "opportunit" in _IDEATION_SYSTEM_PROMPT.lower()


def test_ideation_demands_concrete_predictions():
    """Build advisor must predict outcomes (what would happen if A + B),
    not just describe what exists."""
    assert "Predict" in _IDEATION_SYSTEM_PROMPT or "predict" in _IDEATION_SYSTEM_PROMPT


def test_nuance_prompt_preserves_typology_and_inference_labels():
    """Nuance mode must tell the model to use gap types and labeled inference."""
    prompt = _NUANCE_SYSTEM_PROMPT
    for marker in [
        "Explore nuance",
        "Identify gaps",
        "Identify bridges",
        "terminological",
        "analogy",
        "transfer",
        "missing_edge",
        "[INFERENCE]",
        "[BRIDGE]",
    ]:
        assert marker in prompt


def test_system_prompt_helper_dispatches_nuance():
    assert _system_prompt_for_synthesis_mode("research") == _SYNTHESIS_SYSTEM_PROMPT
    assert _system_prompt_for_synthesis_mode("ideation") == _IDEATION_SYSTEM_PROMPT
    assert _system_prompt_for_synthesis_mode("nuance") == _NUANCE_SYSTEM_PROMPT


# ─── Mode dispatch ──────────────────────────────────────────────────────────


def test_call_llm_synthesis_dispatches_on_mode():
    """_call_llm_synthesis must accept synthesis_mode and dispatch to the
    correct prompt. We don't make the LLM call — we just verify the
    function signature and the inline dispatch logic by introspection."""
    import inspect

    from services.graph.orchestrator import _call_llm_synthesis

    sig = inspect.signature(_call_llm_synthesis)
    assert "synthesis_mode" in sig.parameters
    # Default must be "research" to preserve backward compatibility for
    # every existing caller.
    assert sig.parameters["synthesis_mode"].default == "research"


def test_discover_accepts_synthesis_mode():
    """Public discover() entry point must accept the parameter."""
    import inspect

    from services.graph.orchestrator import discover

    sig = inspect.signature(discover)
    assert "synthesis_mode" in sig.parameters
    assert sig.parameters["synthesis_mode"].default == "research"


def test_graph_discover_request_schema_has_synthesis_mode():
    """The /api/graph/discover request body must surface synthesis_mode
    so the frontend can drive the toggle without a separate endpoint."""
    from models.schemas import GraphDiscoverRequest

    fields = GraphDiscoverRequest.model_fields
    assert "synthesis_mode" in fields
    # Default must be "research" so every existing client keeps working.
    assert fields["synthesis_mode"].default == "research"
    req = GraphDiscoverRequest(query="where are the hidden bridges?", synthesis_mode="nuance")
    assert req.synthesis_mode == "nuance"
