"""Sprint #3 — agentic retrieval loop structural tests.

Validates:
  - The selection prompt has the required constraints
  - JSON parsing handles fences, prose wrappers, garbage
  - Hallucinated entities (not in packet) are dropped
  - The bounded loop exits on empty entity list, exits on no-new-evidence
  - Hard cap on rounds is respected
  - Per-round entity cap (3) is respected
  - No-loop-pumping on the same entity twice
  - Exceptions in retrieve_for_entity are swallowed
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.graph.agentic_retriever import (
    MAX_ENTITIES_PER_ROUND,
    MAX_ROUNDS,
    _SELECT_DEEPEN_PROMPT,
    _entity_appears_in_packet,
    run_agentic_loop,
    select_entities_to_deepen,
)


# ─── Prompt content pins ───────────────────────────────────────────────────


def test_select_prompt_demands_packet_grounded_entities():
    assert "appears verbatim" in _SELECT_DEEPEN_PROMPT


def test_select_prompt_caps_at_three_entities():
    assert "Maximum 3 entities" in _SELECT_DEEPEN_PROMPT


def test_select_prompt_allows_quitting():
    """Empty list must be a valid output — the loop has to be able to stop."""
    assert "empty list" in _SELECT_DEEPEN_PROMPT


def test_select_prompt_forbids_invention():
    assert "Do NOT invent" in _SELECT_DEEPEN_PROMPT


# ─── Packet-grounding check ────────────────────────────────────────────────


def test_entity_appears_in_packet_evidence_text():
    packet = {
        "evidence": [{"text": "the Humanoid drives locomotion"}],
        "edges": [],
        "anchors": [],
    }
    assert _entity_appears_in_packet("Humanoid", packet)


def test_entity_appears_in_packet_edge_field():
    packet = {
        "evidence": [],
        "edges": [{"source_name": "TweenService", "target": "Animation"}],
        "anchors": [],
    }
    assert _entity_appears_in_packet("TweenService", packet)


def test_entity_appears_in_packet_anchor():
    packet = {"evidence": [], "edges": [], "anchors": ["MoveTo"]}
    assert _entity_appears_in_packet("moveto", packet)  # case-insensitive


def test_entity_not_in_packet_returns_false():
    packet = {
        "evidence": [{"text": "talks about spiders"}],
        "edges": [],
        "anchors": [],
    }
    assert not _entity_appears_in_packet("Humanoid", packet)


# ─── Selection LLM call: parsing + hallucination drop ──────────────────────


def _stub_llm(response: str):
    return type("L", (), {"complete_sync": AsyncMock(return_value=response)})


def _packet(evidence_texts: list[str], anchors=None):
    return {
        "evidence": [{"text": t, "source": {"label": "src"}} for t in evidence_texts],
        "edges": [],
        "anchors": anchors or [],
    }


@pytest.mark.asyncio
async def test_select_returns_valid_entities():
    llm = _stub_llm('{"entities": ["Humanoid", "TweenService"], "reason": "load-bearing"}')
    entities, reason = await select_entities_to_deepen(
        llm_service=llm,
        packet=_packet(["Humanoid drives locomotion via TweenService"]),
        user_query="how does X work",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert entities == ["Humanoid", "TweenService"]
    assert "load-bearing" in reason


@pytest.mark.asyncio
async def test_select_drops_hallucinated_entities():
    """Entity not in packet → dropped silently."""
    llm = _stub_llm('{"entities": ["Humanoid", "AntiGravityNode"], "reason": "y"}')
    entities, _ = await select_entities_to_deepen(
        llm_service=llm,
        packet=_packet(["only Humanoid is in here"]),
        user_query="q",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert entities == ["Humanoid"]
    assert "AntiGravityNode" not in entities


@pytest.mark.asyncio
async def test_select_caps_at_max_entities_per_round():
    llm = _stub_llm(
        '{"entities": ["a", "b", "c", "d", "e"], "reason": "many"}'
    )
    entities, _ = await select_entities_to_deepen(
        llm_service=llm,
        packet=_packet(["a b c d e"]),
        user_query="q",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert len(entities) <= MAX_ENTITIES_PER_ROUND


@pytest.mark.asyncio
async def test_select_quits_cleanly_on_empty_list():
    llm = _stub_llm('{"entities": [], "reason": "evidence sufficient"}')
    entities, reason = await select_entities_to_deepen(
        llm_service=llm,
        packet=_packet(["full coverage"]),
        user_query="q",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert entities == []
    assert reason == "evidence sufficient"


@pytest.mark.asyncio
async def test_select_handles_prose_wrapped_json():
    llm = _stub_llm(
        'Here is my analysis:\n{"entities": ["Humanoid"], "reason": "x"}\nDone.'
    )
    entities, _ = await select_entities_to_deepen(
        llm_service=llm,
        packet=_packet(["Humanoid is mentioned"]),
        user_query="q",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert entities == ["Humanoid"]


@pytest.mark.asyncio
async def test_select_handles_code_fence_wrapper():
    llm = _stub_llm('```json\n{"entities": ["Humanoid"], "reason": "x"}\n```')
    entities, _ = await select_entities_to_deepen(
        llm_service=llm,
        packet=_packet(["Humanoid mention"]),
        user_query="q",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert entities == ["Humanoid"]


@pytest.mark.asyncio
async def test_select_returns_empty_on_garbage():
    llm = _stub_llm("I think you should look at humanoid stuff.")
    entities, _ = await select_entities_to_deepen(
        llm_service=llm,
        packet=_packet(["Humanoid here"]),
        user_query="q",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert entities == []


@pytest.mark.asyncio
async def test_select_returns_empty_on_llm_exception():
    class BoomLLM:
        async def complete_sync(self, **kwargs):
            raise RuntimeError("boom")

    entities, _ = await select_entities_to_deepen(
        llm_service=BoomLLM(),
        packet=_packet(["x"]),
        user_query="q",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert entities == []


@pytest.mark.asyncio
async def test_select_drops_duplicate_entities():
    """LLM returns ['Humanoid', 'humanoid', 'HUMANOID'] → just one."""
    llm = _stub_llm(
        '{"entities": ["Humanoid", "humanoid", "HUMANOID"], "reason": "x"}'
    )
    entities, _ = await select_entities_to_deepen(
        llm_service=llm,
        packet=_packet(["Humanoid x"]),
        user_query="q",
        round_index=1,
        creds={"model": "m", "extra_params": {}},
    )
    assert len(entities) == 1


# ─── Full loop: round budget, evidence merge, exit conditions ──────────────


@pytest.mark.asyncio
async def test_loop_exits_on_empty_selection():
    """First selection returns []  →  loop exits round 1, no retrieval."""
    llm = _stub_llm('{"entities": [], "reason": "done"}')
    retrieve_called = []

    async def _retrieve(entity: str) -> list[dict[str, Any]]:
        retrieve_called.append(entity)
        return []

    merged = await run_agentic_loop(
        base_packet=_packet(["start"]),
        user_query="q",
        creds={"model": "m", "extra_params": {}},
        llm_service=llm,
        retrieve_for_entity=_retrieve,
    )
    assert retrieve_called == []
    assert merged.get("agentic_rounds_run") == 1
    assert merged["agentic_trace"][0]["exit"] == "no_entities"


@pytest.mark.asyncio
async def test_loop_merges_new_evidence_then_quits_when_no_more():
    """Round 1: picks Humanoid → retrieves 2 chunks. Round 2: picks []. Done."""

    class CountingLLM:
        def __init__(self):
            self.call_count = 0

        async def complete_sync(self, **kwargs):
            self.call_count += 1
            if self.call_count == 1:
                return '{"entities": ["Humanoid"], "reason": "core"}'
            return '{"entities": [], "reason": "covered"}'

    llm = CountingLLM()

    async def _retrieve(entity: str) -> list[dict[str, Any]]:
        return [
            {"text": f"new chunk about {entity} (1)"},
            {"text": f"new chunk about {entity} (2)"},
        ]

    base = _packet(["initial chunk mentions Humanoid"])
    merged = await run_agentic_loop(
        base_packet=base,
        user_query="q",
        creds={"model": "m", "extra_params": {}},
        llm_service=llm,
        retrieve_for_entity=_retrieve,
    )
    # Started with 1, added 2 in round 1 → 3 total.
    assert len(merged["evidence"]) == 3
    # Round 1 added evidence, round 2 quit on empty entities.
    assert merged["agentic_rounds_run"] == 2


@pytest.mark.asyncio
async def test_loop_respects_max_rounds_cap():
    """If the LLM keeps suggesting new entities, the loop still stops at MAX_ROUNDS."""

    class GreedyLLM:
        def __init__(self):
            self.count = 0

        async def complete_sync(self, **kwargs):
            self.count += 1
            return f'{{"entities": ["E{self.count}"], "reason": "x"}}'

    llm = GreedyLLM()

    async def _retrieve(entity: str) -> list[dict[str, Any]]:
        # Echo the entity into evidence so it's grounded for future rounds.
        return [{"text": f"chunk about {entity}"}]

    # Seed packet has E1, E2, ... E5 mentioned so the entity-grounding
    # check accepts the LLM's suggestions.
    base = _packet([f"E{i}" for i in range(1, MAX_ROUNDS + 5)])

    merged = await run_agentic_loop(
        base_packet=base,
        user_query="q",
        creds={"model": "m", "extra_params": {}},
        llm_service=llm,
        retrieve_for_entity=_retrieve,
    )
    assert merged["agentic_rounds_run"] <= MAX_ROUNDS


@pytest.mark.asyncio
async def test_loop_does_not_deepen_same_entity_twice():
    """If the LLM picks Humanoid in round 1 AND round 2, only round 1 retrieves."""

    class StubbornLLM:
        async def complete_sync(self, **kwargs):
            return '{"entities": ["Humanoid"], "reason": "again"}'

    llm = StubbornLLM()
    retrieve_calls: list[str] = []

    async def _retrieve(entity: str) -> list[dict[str, Any]]:
        retrieve_calls.append(entity)
        return [{"text": f"new chunk about {entity}"}]

    merged = await run_agentic_loop(
        base_packet=_packet(["Humanoid in seed"]),
        user_query="q",
        creds={"model": "m", "extra_params": {}},
        llm_service=llm,
        retrieve_for_entity=_retrieve,
    )
    # Humanoid retrieved exactly once even though LLM picked it every round.
    assert retrieve_calls.count("Humanoid") == 1


@pytest.mark.asyncio
async def test_loop_dedupes_agentic_evidence_against_base():
    """Sprint #3 follow-up — if the agentic retriever returns a chunk
    that's already in the base packet (high-similarity to the original
    query), the loop must drop it instead of duplicating."""

    class OneRoundLLM:
        def __init__(self):
            self.n = 0

        async def complete_sync(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return '{"entities": ["Humanoid"], "reason": "x"}'
            return '{"entities": [], "reason": "done"}'

    async def _retrieve(entity: str):
        # Return two chunks — one duplicates the base, one is new.
        return [
            {"chunk_id": "already_in_base", "text": "duplicate"},
            {"chunk_id": "new_chunk", "text": "fresh evidence"},
        ]

    base = {
        "evidence": [
            {"chunk_id": "already_in_base", "text": "originally retrieved"},
        ],
        "edges": [],
        "anchors": ["Humanoid"],
    }
    merged = await run_agentic_loop(
        base_packet=base,
        user_query="q",
        creds={"model": "m", "extra_params": {}},
        llm_service=OneRoundLLM(),
        retrieve_for_entity=_retrieve,
    )
    # Started with 1, added only the non-duplicate → 2 total.
    assert len(merged["evidence"]) == 2
    assert merged["agentic_trace"][0]["added_evidence"] == 1
    assert merged["agentic_trace"][0]["deduped"] == 1


@pytest.mark.asyncio
async def test_loop_dedupes_across_rounds():
    """If round 1 retrieves chunk_X and round 2 returns chunk_X again
    (different entity → same chunk), round 2 drops it as a dupe."""

    class TwoRoundLLM:
        def __init__(self):
            self.n = 0

        async def complete_sync(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return '{"entities": ["A"], "reason": "first"}'
            if self.n == 2:
                return '{"entities": ["B"], "reason": "second"}'
            return '{"entities": [], "reason": "done"}'

    async def _retrieve(entity: str):
        # Both entities return the same chunk_X — only round 1 should add it.
        return [{"chunk_id": "chunk_X", "text": f"about {entity}"}]

    merged = await run_agentic_loop(
        base_packet={"evidence": [], "edges": [], "anchors": ["A", "B"]},
        user_query="q",
        creds={"model": "m", "extra_params": {}},
        llm_service=TwoRoundLLM(),
        retrieve_for_entity=_retrieve,
    )
    assert len(merged["evidence"]) == 1
    assert merged["agentic_trace"][1]["deduped"] == 1


@pytest.mark.asyncio
async def test_loop_allows_dedup_when_chunk_id_missing():
    """Some retrievers may omit chunk_id. The dedup tracker should
    accept those rather than collapsing them all together."""

    class OneRoundLLM:
        def __init__(self):
            self.n = 0

        async def complete_sync(self, **kwargs):
            self.n += 1
            return (
                '{"entities": ["A"], "reason": "first"}'
                if self.n == 1
                else '{"entities": [], "reason": "done"}'
            )

    async def _retrieve(entity: str):
        # No chunk_ids — both must pass through.
        return [
            {"text": "first agentic snippet"},
            {"text": "second agentic snippet"},
        ]

    merged = await run_agentic_loop(
        base_packet={"evidence": [], "edges": [], "anchors": ["A"]},
        user_query="q",
        creds={"model": "m", "extra_params": {}},
        llm_service=OneRoundLLM(),
        retrieve_for_entity=_retrieve,
    )
    assert len(merged["evidence"]) == 2
    assert merged["agentic_trace"][0]["deduped"] == 0


@pytest.mark.asyncio
async def test_loop_survives_retrieve_exception():
    """retrieve_for_entity raises → loop logs and continues, doesn't crash."""

    class OneshotLLM:
        def __init__(self):
            self.count = 0

        async def complete_sync(self, **kwargs):
            self.count += 1
            if self.count == 1:
                return '{"entities": ["Humanoid"], "reason": "x"}'
            return '{"entities": [], "reason": "done"}'

    async def _broken_retrieve(entity: str) -> list[dict[str, Any]]:
        raise RuntimeError("retrieval broke")

    merged = await run_agentic_loop(
        base_packet=_packet(["Humanoid"]),
        user_query="q",
        creds={"model": "m", "extra_params": {}},
        llm_service=OneshotLLM(),
        retrieve_for_entity=_broken_retrieve,
    )
    # No crash; trace captures the round.
    assert "agentic_trace" in merged
