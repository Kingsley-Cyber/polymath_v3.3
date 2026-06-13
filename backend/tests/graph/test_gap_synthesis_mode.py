"""Contract tests for the "gap" synthesis mode.

Gap mode reuses the same retrieval + packet builder as research/nuance/
ideation; only the packet caps, token budget, system prompt, and deepening
selector differ. These tests lock that wiring so an accidental edit to the
caps or prompt is caught rather than silently shipping a degraded mode.
"""
from models.schemas import GraphDiscoverRequest
from services.graph.agentic_retriever import (
    _SELECT_DEEPEN_GAP_PROMPT,
    _select_prompt_for_mode,
)
from services.graph.orchestrator import (
    _GAP_SYSTEM_PROMPT,
    _IDEATION_SYSTEM_PROMPT,
    _NUANCE_SYSTEM_PROMPT,
    _SYNTHESIS_SYSTEM_PROMPT,
    _packet_caps_for_mode,
    _render_packet_user_prompt,
    _synthesis_max_tokens_for_mode,
    _system_prompt_for_synthesis_mode,
)


def test_gap_caps_foreground_absence_signals():
    """Gap mode must surface far more gap/fragile/weak material than the
    defaults (3 each) — that material IS the answer."""
    caps = _packet_caps_for_mode("gap")
    assert caps.gaps == 12
    assert caps.fragile_bridges == 8
    assert caps.weak_links == 8
    # Lateral bridging material kept; dense edge list and evidence trimmed.
    assert caps.bridges == 5
    assert caps.transfers == 5
    assert caps.analogies == 4
    assert caps.edges == 6
    assert caps.evidence == 8


def test_gap_token_budget():
    assert _synthesis_max_tokens_for_mode("gap") == 2200


def test_gap_system_prompt_selected_and_distinct():
    prompt = _system_prompt_for_synthesis_mode("gap")
    assert prompt is _GAP_SYSTEM_PROMPT
    assert prompt is not _NUANCE_SYSTEM_PROMPT
    assert prompt is not _IDEATION_SYSTEM_PROMPT
    assert prompt is not _SYNTHESIS_SYSTEM_PROMPT


def test_gap_prompt_contract():
    """The gap prompt must ask for a structural, metric-grounded, honest map."""
    p = _GAP_SYSTEM_PROMPT
    assert "gap analyst" in p.lower()
    assert "Gap ledger" in p  # the required output table
    assert "HYPOTHESIS" in p  # gaps are hypotheses, not proven absences
    assert "missing_edge" in p and "fragile_bridge" in p and "terminological" in p
    assert "Close this first" in p  # actionable closing section


def test_gap_deepen_selector_allows_cross_domain():
    prompt, show_structural = _select_prompt_for_mode("gap")
    assert prompt is _SELECT_DEEPEN_GAP_PROMPT
    # Gaps are often cross-domain, so the deepening loop must see the
    # structural items (bridges/gaps/analogies/transfers).
    assert show_structural is True


def test_schema_accepts_gap_mode():
    req = GraphDiscoverRequest(query="what should connect but doesn't?", synthesis_mode="gap")
    assert req.synthesis_mode == "gap"


def test_gap_renderer_foregrounds_gap_material():
    """A packet carrying gaps/fragile_bridges/weak_links must render the gap
    curation lane and the candidate-gaps section with the bridging question."""
    packet = {
        "query": "how do A and B relate?",
        "evidence": [],
        "gaps": [
            {
                "gap_id": "g1",
                "gap_type": "missing_edge",
                "cluster_a_label": "A",
                "cluster_b_label": "B",
                "question": "Does A relate to B, and how?",
                "topology_sim": 0.71,
                "coherence": {"shared_terms": ["t1"], "shared_neighbors": ["n1"]},
                "support_status": "unsupported",
            }
        ],
        "fragile_bridges": [
            {
                "source_name": "A",
                "target_name": "C",
                "path_entities": ["A", "x", "C"],
                "evidence": "single articulation path",
            }
        ],
        "weak_links": [{"rationale": "thin provenance on the A->D claim"}],
        "bridges": [],
        "analogies": [],
        "transfers": [],
        "tensions": [],
        "communities": [],
        "signals": [],
    }
    out = _render_packet_user_prompt(packet, synthesis_mode="gap")
    assert "GAP MATERIAL" in out  # the gap-specific curation lane
    assert "Candidate gaps" in out
    assert "Does A relate to B, and how?" in out


def test_existing_modes_unchanged():
    """Adding gap must not perturb the other three modes."""
    assert _synthesis_max_tokens_for_mode("research") == 1900
    assert _synthesis_max_tokens_for_mode("nuance") == 1800
    assert _synthesis_max_tokens_for_mode("ideation") == 2400
    assert _system_prompt_for_synthesis_mode("nuance") is _NUANCE_SYSTEM_PROMPT
    assert _system_prompt_for_synthesis_mode("ideation") is _IDEATION_SYSTEM_PROMPT
    assert _system_prompt_for_synthesis_mode("research") is _SYNTHESIS_SYSTEM_PROMPT
    assert _packet_caps_for_mode("research").gaps == 2
