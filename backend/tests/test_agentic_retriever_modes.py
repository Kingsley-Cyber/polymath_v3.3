import pytest

from services.graph import agentic_retriever
from services.graph.agentic_retriever import (
    _SELECT_DEEPEN_PROMPT,
    _selection_appears_in_packet,
    _select_prompt_for_mode,
    _split_deepen_selection,
    run_agentic_loop,
)


def test_agentic_mode_prompt_selection_preserves_nuance_default():
    prompt, show_structural = _select_prompt_for_mode("nuance")
    assert prompt == _SELECT_DEEPEN_PROMPT
    assert show_structural is False


def test_agentic_ideation_prompt_shows_structural_material():
    prompt, show_structural = _select_prompt_for_mode("ideation")
    assert show_structural is True
    assert "bridges, gaps, analogies, transfers" in prompt
    assert "A + B" in prompt


def test_agentic_research_prompt_deepens_proof_not_structure():
    prompt, show_structural = _select_prompt_for_mode("research")
    assert show_structural is False
    assert "MORE EVIDENCE" in prompt
    assert "proof gap" in prompt


def test_ideation_hallucination_guard_accepts_bridge_compound():
    packet = {
        "bridges": [
            {
                "source_name": "Identity Map",
                "target_name": "User Profile",
                "bridge_type": "cross_domain",
            }
        ]
    }

    assert _selection_appears_in_packet("Identity Map + User Profile", packet)
    assert not _selection_appears_in_packet("Identity Map + Missing Concept", packet)


def test_split_deepen_selection_supports_compound_bridge_names():
    assert _split_deepen_selection("Identity Map + User Profile") == [
        "Identity Map",
        "User Profile",
    ]
    assert _split_deepen_selection("A <-> B") == ["A", "B"]


@pytest.mark.asyncio
async def test_agentic_loop_retrieves_both_sides_of_ideation_compound(monkeypatch):
    async def fake_select(**kwargs):
        assert kwargs["synthesis_mode"] == "ideation"
        return ["Identity Map + User Profile"], "bridge needs both sides"

    calls = []

    async def fake_retrieve(entity_name: str):
        calls.append(entity_name)
        return [
            {
                "chunk_id": f"chunk-{entity_name}",
                "text": entity_name,
            }
        ]

    monkeypatch.setattr(agentic_retriever, "select_entities_to_deepen", fake_select)

    packet = await run_agentic_loop(
        base_packet={"evidence": []},
        user_query="find an idea",
        creds={},
        llm_service=None,
        retrieve_for_entity=fake_retrieve,
        synthesis_mode="ideation",
        max_rounds=1,
    )

    assert calls == ["Identity Map", "User Profile"]
    assert len(packet["evidence"]) == 2
