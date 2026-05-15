"""Phase 28 — thinking-effort wiring tests.

Verifies that the ModelOverrides.thinking_effort field flows through
LLMService._build_request_body into the provider-native body params
via services.thinking_mapper.apply_thinking_effort.

These tests don't make real LLM calls. They build the request body
in isolation and assert that the expected param appears (or doesn't,
for non-reasoning models).
"""

from __future__ import annotations

import pytest

from models.schemas import ModelOverrides
from services.llm import LLMService


@pytest.fixture
def llm():
    return LLMService()


# ─── Schema field is exposed ───────────────────────────────────────────────


def test_model_overrides_has_thinking_effort():
    """The field MUST exist on ModelOverrides so the router can accept it."""
    fields = ModelOverrides.model_fields
    assert "thinking_effort" in fields
    # Default must be None — the field is opt-in per-request.
    assert fields["thinking_effort"].default is None


def test_model_overrides_accepts_known_efforts():
    """Auto/none/low/medium/high all parse without error."""
    for value in ("auto", "none", "low", "medium", "high"):
        overrides = ModelOverrides(thinking_effort=value)
        assert overrides.thinking_effort == value


# ─── _build_request_body wiring ────────────────────────────────────────────


def test_no_thinking_effort_omits_provider_params(llm):
    """When thinking_effort is None, no reasoning_effort / thinking /
    thinking_budget appears in the body."""
    body = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/o3-mini",
        overrides=ModelOverrides(),  # thinking_effort=None by default
    )
    assert "reasoning_effort" not in body
    assert "thinking" not in body
    assert "thinking_budget" not in body


def test_openai_o3_high_maps_to_reasoning_effort_high(llm):
    """o3 + high → body.reasoning_effort = 'high'."""
    body = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/o3-mini",
        overrides=ModelOverrides(thinking_effort="high"),
    )
    assert body.get("reasoning_effort") == "high"


def test_openai_o3_low_maps_to_reasoning_effort_low(llm):
    body = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/o3-mini",
        overrides=ModelOverrides(thinking_effort="low"),
    )
    assert body.get("reasoning_effort") == "low"


def test_anthropic_high_emits_thinking_dict(llm):
    """Claude with effort=high → body.thinking = {type: enabled, budget_tokens: N}."""
    body = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="anthropic/claude-sonnet-4-5",
        overrides=ModelOverrides(thinking_effort="high"),
    )
    thinking = body.get("thinking")
    assert isinstance(thinking, dict)
    assert thinking.get("type") == "enabled"
    assert isinstance(thinking.get("budget_tokens"), int)
    assert thinking["budget_tokens"] > 0


def test_gemini_25_emits_thinking_budget(llm):
    """Gemini 2.5 with medium → body.thinking_budget = N (some non-zero int)."""
    body = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="gemini/gemini-2.5-flash",
        overrides=ModelOverrides(thinking_effort="medium"),
    )
    # Gemini-specific param (services.thinking_mapper._GEMINI_BUDGETS).
    # Either thinking_budget or thinkingBudget depending on mapper —
    # accept either shape.
    has_budget = (
        "thinking_budget" in body
        or "thinkingBudget" in body
        or isinstance(body.get("thinking"), dict)
    )
    assert has_budget, f"expected Gemini thinking param in body, got: {body}"


def test_non_reasoning_model_no_thinking_param(llm):
    """gpt-4o is NOT a reasoning model — even with effort=high, no
    reasoning_effort should appear (mapper gates on model)."""
    body = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",
        overrides=ModelOverrides(thinking_effort="high"),
    )
    assert "reasoning_effort" not in body


def test_effort_none_disables_thinking(llm):
    """Even for a reasoning model, effort=none should produce no
    thinking param (or an explicit-disabled signal — accept either)."""
    body = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="anthropic/claude-sonnet-4-5",
        overrides=ModelOverrides(thinking_effort="none"),
    )
    # Either thinking is absent, or it's an explicit "disabled" structure.
    if "thinking" in body:
        thinking = body["thinking"]
        if isinstance(thinking, dict):
            assert thinking.get("type") != "enabled" or thinking.get("budget_tokens") in (None, 0)


def test_overrides_model_takes_precedence_over_arg(llm):
    """If overrides.model is set, the thinking mapper should use THAT
    model name (not the positional `model` arg) when picking provider."""
    body = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",  # non-reasoning
        overrides=ModelOverrides(
            model="anthropic/claude-sonnet-4-5",  # reasoning
            thinking_effort="medium",
        ),
    )
    assert body["model"] == "anthropic/claude-sonnet-4-5"
    # And thinking-mapper should have stamped a Claude-shaped thinking dict.
    thinking = body.get("thinking")
    assert isinstance(thinking, dict)
    assert thinking.get("type") == "enabled"
