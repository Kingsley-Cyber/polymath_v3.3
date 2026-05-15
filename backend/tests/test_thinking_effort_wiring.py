"""Phase 28 — thinking-effort wiring tests (blank-slate state).

The mapper has no providers configured yet — these tests pin the
EMPTY-STATE behavior:

  - The schema field exists and accepts the 5 effort values
  - The mapper is a no-op for every model
  - The LLM service call site doesn't crash when effort is set
  - The auto / none / explicit-level paths all flow through without
    raising

When you wire a provider, ADD a test file (e.g.
`test_thinking_anthropic.py`) that pins the provider-specific shape.
Don't modify these blank-slate tests — they should stay green even
as providers come online.
"""

from __future__ import annotations

import pytest

from models.schemas import ModelOverrides
from services.llm import LLMService
from services.thinking_mapper import apply_thinking_effort


@pytest.fixture
def llm():
    return LLMService()


# ─── Schema field is exposed ───────────────────────────────────────────────


def test_model_overrides_has_thinking_effort():
    """The field MUST exist on ModelOverrides so the router accepts it."""
    fields = ModelOverrides.model_fields
    assert "thinking_effort" in fields
    # Default must be None — opt-in per-request.
    assert fields["thinking_effort"].default is None


@pytest.mark.parametrize(
    "value", ["auto", "none", "low", "medium", "high"]
)
def test_model_overrides_accepts_known_efforts(value):
    """All 5 effort values parse without validation error."""
    overrides = ModelOverrides(thinking_effort=value)
    assert overrides.thinking_effort == value


# ─── Mapper no-op behavior with no providers wired ─────────────────────────


def test_mapper_noop_when_effort_is_none():
    """effort=None means caller didn't opt in → body unchanged."""
    body = {"model": "openai/gpt-4o", "messages": []}
    snapshot = dict(body)
    apply_thinking_effort(body, "openai/gpt-4o", None)
    assert body == snapshot


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-4o",
        "openai/o3-mini",
        "anthropic/claude-sonnet-4-5",
        "gemini/gemini-2.5-flash",
        "deepseek/deepseek-reasoner",
        "groq/llama-3.3-70b",
        "ollama/qwen2.5:7b",
        "bare-model-id-with-no-provider",
    ],
)
@pytest.mark.parametrize(
    "effort", ["auto", "low", "medium", "high", "none"]
)
def test_mapper_is_noop_for_every_model_and_effort(model, effort):
    """Blank-slate: no providers configured → every (model, effort)
    combination leaves the body untouched. The function should NEVER
    raise. When you wire a provider, this test will continue to pass
    for OTHER models but will be superseded by a provider-specific
    test for the wired provider."""
    body = {"model": model, "messages": []}
    snapshot = dict(body)
    apply_thinking_effort(body, model, effort)
    assert body == snapshot, (
        f"mapper mutated body for ({model!r}, {effort!r}) — "
        f"if a provider was added, write a provider-specific test."
    )


# ─── LLM call-site integration ─────────────────────────────────────────────


def test_build_request_body_does_not_crash_with_effort(llm):
    """The LLM service must invoke the mapper without crashing even
    when no providers are wired. Body should be unchanged from the
    no-mapper case."""
    body_with = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/o3-mini",
        overrides=ModelOverrides(thinking_effort="high"),
    )
    body_without = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/o3-mini",
        overrides=ModelOverrides(),
    )
    # The two bodies must be identical (mapper is a no-op until a
    # provider block is added). The only intentional difference would
    # be a `model` or `messages` change — neither happens here.
    assert body_with == body_without


def test_build_request_body_with_effort_none(llm):
    """effort=None should produce identical output to no overrides at all."""
    body_with = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/o3-mini",
        overrides=ModelOverrides(thinking_effort=None),
    )
    body_without = llm._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/o3-mini",
        overrides=ModelOverrides(),
    )
    assert body_with == body_without
