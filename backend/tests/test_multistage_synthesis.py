"""Sprint #2 — multi-stage synthesis structural regression tests.

These verify the critique/revise prompts and dispatch wiring, plus
the JSON-extraction robustness on the critique side. They do NOT
make real LLM calls — they mock `llm_service.complete_sync` and
exercise the surrounding logic.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.graph.orchestrator import (
    _CRITIQUE_SYSTEM_PROMPT,
    _REVISE_SYSTEM_PROMPT,
    _critique_synthesis,
    _revise_synthesis,
)


# ─── Prompt content pins ───────────────────────────────────────────────────


def test_critique_prompt_lists_required_categories():
    """The auditor must know about all five failure categories."""
    required = [
        "fabricated_term",
        "missing_citation",
        "shell_sentence",
        "internal_label_leak",
        "contradicts_evidence",
    ]
    for cat in required:
        assert cat in _CRITIQUE_SYSTEM_PROMPT, f"missing category: {cat}"


def test_critique_prompt_demands_json_only_output():
    """The auditor must be told to emit ONLY JSON."""
    assert "JSON" in _CRITIQUE_SYSTEM_PROMPT
    assert "Output ONLY a JSON" in _CRITIQUE_SYSTEM_PROMPT or "ONLY a JSON" in _CRITIQUE_SYSTEM_PROMPT


def test_critique_prompt_caps_problem_count():
    assert "10 problems" in _CRITIQUE_SYSTEM_PROMPT


def test_revise_prompt_forbids_new_content():
    """The editor must NOT invent replacements for removed sentences."""
    assert "Never invent" in _REVISE_SYSTEM_PROMPT or "Do NOT add new claims" in _REVISE_SYSTEM_PROMPT


def test_revise_prompt_demands_silent_output():
    """The editor must not emit a 'Here is the revised version:' preamble."""
    assert "Do NOT explain" in _REVISE_SYSTEM_PROMPT or "no \"Here is" in _REVISE_SYSTEM_PROMPT


# ─── Critique parsing robustness ───────────────────────────────────────────


def _stub_llm(response_text: str):
    """Stub llm_service.complete_sync to return a fixed string."""
    mock = AsyncMock(return_value=response_text)
    return mock


@pytest.mark.asyncio
async def test_critique_parses_clean_json():
    llm = type(
        "L", (), {"complete_sync": _stub_llm(
            '{"problems": [{"sentence": "a", "category": "shell_sentence", "detail": "x"}]}'
        )}
    )
    problems, err = await _critique_synthesis(
        llm_service=llm,
        draft_markdown="some draft",
        user_prompt="some user prompt",
        creds={"model": "m", "extra_params": {}},
    )
    assert err is None
    assert len(problems) == 1
    assert problems[0]["category"] == "shell_sentence"


@pytest.mark.asyncio
async def test_critique_tolerates_code_fence_wrapper():
    """Many models wrap JSON in ```json ... ``` fences."""
    fenced = "```json\n{\"problems\": []}\n```"
    llm = type("L", (), {"complete_sync": _stub_llm(fenced)})
    problems, err = await _critique_synthesis(
        llm_service=llm,
        draft_markdown="draft",
        user_prompt="user",
        creds={"model": "m", "extra_params": {}},
    )
    assert err is None
    assert problems == []


@pytest.mark.asyncio
async def test_critique_tolerates_chat_wrapper():
    """Some models prepend 'Here is the audit:' before the JSON."""
    wrapped = 'Here is the audit:\n{"problems": [{"sentence": "x", "category": "fabricated_term"}]}\nEnd of audit.'
    llm = type("L", (), {"complete_sync": _stub_llm(wrapped)})
    problems, err = await _critique_synthesis(
        llm_service=llm,
        draft_markdown="draft",
        user_prompt="user",
        creds={"model": "m", "extra_params": {}},
    )
    assert err is None
    assert len(problems) == 1


@pytest.mark.asyncio
async def test_critique_rejects_garbage_response():
    """If the LLM returns prose with no JSON, return error not crash."""
    llm = type("L", (), {"complete_sync": _stub_llm("I think the synthesis is fine.")})
    problems, err = await _critique_synthesis(
        llm_service=llm,
        draft_markdown="d",
        user_prompt="u",
        creds={"model": "m", "extra_params": {}},
    )
    assert err == "critique_no_json"
    assert problems == []


@pytest.mark.asyncio
async def test_critique_caps_problem_count_at_10():
    """Defense against a runaway auditor emitting 50 problems."""
    many = ",".join(
        f'{{"sentence": "s{i}", "category": "shell_sentence", "detail": "d{i}"}}'
        for i in range(50)
    )
    llm = type("L", (), {"complete_sync": _stub_llm(f'{{"problems": [{many}]}}')})
    problems, err = await _critique_synthesis(
        llm_service=llm,
        draft_markdown="draft",
        user_prompt="user",
        creds={"model": "m", "extra_params": {}},
    )
    assert err is None
    assert len(problems) == 10  # capped


@pytest.mark.asyncio
async def test_critique_skips_problems_missing_required_fields():
    """A problem object without `sentence` or `category` is dropped."""
    llm = type("L", (), {"complete_sync": _stub_llm(
        '{"problems": [{"sentence": "valid", "category": "shell_sentence"},'
        ' {"detail": "no sentence"},'
        ' {"sentence": "no category"}]}'
    )})
    problems, err = await _critique_synthesis(
        llm_service=llm,
        draft_markdown="d",
        user_prompt="u",
        creds={"model": "m", "extra_params": {}},
    )
    assert err is None
    assert len(problems) == 1
    assert problems[0]["sentence"] == "valid"


@pytest.mark.asyncio
async def test_critique_handles_llm_exception():
    """LLM transport failure → return error, never raise."""

    class BoomLLM:
        async def complete_sync(self, **kwargs):
            raise RuntimeError("network died")

    problems, err = await _critique_synthesis(
        llm_service=BoomLLM(),
        draft_markdown="d",
        user_prompt="u",
        creds={"model": "m", "extra_params": {}},
    )
    assert err == "critique_llm_failure"
    assert problems == []


@pytest.mark.asyncio
async def test_critique_clamps_sentence_length():
    """A 5000-char sentence gets clipped to 300 chars."""
    huge = "a" * 5000
    llm = type("L", (), {"complete_sync": _stub_llm(
        f'{{"problems": [{{"sentence": "{huge}", "category": "shell_sentence"}}]}}'
    )})
    problems, err = await _critique_synthesis(
        llm_service=llm,
        draft_markdown="d",
        user_prompt="u",
        creds={"model": "m", "extra_params": {}},
    )
    assert err is None
    assert len(problems) == 1
    assert len(problems[0]["sentence"]) <= 300


# ─── Revise stage robustness ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revise_returns_cleaned_markdown():
    llm = type("L", (), {"complete_sync": _stub_llm(
        "# Revised\n*Theme: tag*\nA cleaner synthesis."
    )})
    revised, err = await _revise_synthesis(
        llm_service=llm,
        draft_markdown="# Draft\nbad text.",
        user_prompt="user",
        problems=[
            {"sentence": "bad text.", "category": "shell_sentence", "detail": "empty"}
        ],
        creds={"model": "m", "extra_params": {}},
    )
    assert err is None
    assert revised is not None
    assert "# Revised" in revised


@pytest.mark.asyncio
async def test_revise_empty_response_returns_error():
    llm = type("L", (), {"complete_sync": _stub_llm("")})
    revised, err = await _revise_synthesis(
        llm_service=llm,
        draft_markdown="d",
        user_prompt="u",
        problems=[{"sentence": "x", "category": "shell_sentence"}],
        creds={"model": "m", "extra_params": {}},
    )
    assert err == "revise_empty_response"
    assert revised is None


@pytest.mark.asyncio
async def test_revise_strips_code_fences():
    """Models sometimes wrap markdown output in ```markdown ... ```."""
    llm = type("L", (), {"complete_sync": _stub_llm(
        "```markdown\n# Clean\nbody.\n```"
    )})
    revised, err = await _revise_synthesis(
        llm_service=llm,
        draft_markdown="d",
        user_prompt="u",
        problems=[{"sentence": "x", "category": "shell_sentence"}],
        creds={"model": "m", "extra_params": {}},
    )
    assert err is None
    assert "```" not in (revised or "")


@pytest.mark.asyncio
async def test_revise_llm_exception_returns_error():
    class BoomLLM:
        async def complete_sync(self, **kwargs):
            raise RuntimeError("boom")

    revised, err = await _revise_synthesis(
        llm_service=BoomLLM(),
        draft_markdown="d",
        user_prompt="u",
        problems=[{"sentence": "x", "category": "shell_sentence"}],
        creds={"model": "m", "extra_params": {}},
    )
    assert err == "revise_llm_failure"
    assert revised is None


# ─── Request schema regression ─────────────────────────────────────────────


def test_graph_discover_request_accepts_validate_synthesis():
    """Frontend must be able to pass validate_synthesis through the API."""
    from models.schemas import GraphDiscoverRequest

    fields = GraphDiscoverRequest.model_fields
    assert "validate_synthesis" in fields
    assert fields["validate_synthesis"].default is False
