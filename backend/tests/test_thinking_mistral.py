"""Per-provider thinking-mapper tests: Mistral Magistral.

Mistral's reasoning_effort surface is BINARY — `"high"` or `"none"`.
Our agnostic 5-level enum collapses heavily:

  agnostic    →  Mistral reasoning_effort
  ─────────────────────────────────────────
  None        →  no-op (caller didn't opt in)
  "auto"      →  "high" (default_effort=medium → mapped to "high")
  "low"       →  "high"
  "medium"    →  "high"
  "high"      →  "high"
  "none"      →  "none"  (explicit opt-out)

Only the Magistral line is wired. Other Mistral models (mistral-small,
mistral-large, codestral, open-mistral-7b, etc.) are NOT reasoning
models and must stay no-op so the UI selector doesn't appear for them.
"""

from __future__ import annotations

import pytest

from services.thinking_mapper import apply_thinking_effort


# ─── Detection — Magistral matches, other Mistral models don't ─────────────


@pytest.mark.parametrize(
    "model",
    [
        "mistral/magistral-small",
        "mistral/magistral-small-latest",
        "mistral/magistral-medium",
        "mistral/magistral-medium-latest",
        "magistral-small",  # bare id without provider/ prefix
        "magistral-medium-latest",
    ],
)
def test_magistral_models_get_reasoning_effort(model):
    body: dict = {"model": model, "messages": []}
    apply_thinking_effort(body, model, "high")
    assert body.get("reasoning_effort") == "high"


@pytest.mark.parametrize(
    "model",
    [
        # Non-Magistral Mistral models — must stay no-op
        "mistral/mistral-small-latest",
        "mistral/mistral-medium-latest",
        "mistral/mistral-large-latest",
        "mistral/open-mistral-7b",
        "mistral/codestral-latest",
        "mistral/mistral-embed",
        # Other providers — still no-op
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-5",
        "deepseek/deepseek-chat",  # DeepSeek but NOT V4 → no-op
    ],
)
def test_non_magistral_models_unchanged(model):
    body: dict = {"model": model, "messages": []}
    snapshot = dict(body)
    apply_thinking_effort(body, model, "high")
    assert body == snapshot, (
        f"mapper mutated body for {model!r} — _is_mistral_magistral "
        f"is over-matching. Should match only models with 'magistral' "
        f"in the name."
    )


# ─── Effort collapse — 4 agnostic tiers → "high" ───────────────────────────


@pytest.mark.parametrize("agnostic_effort", ["low", "medium", "high"])
def test_thinking_efforts_all_map_to_high(agnostic_effort):
    """Mistral has a binary dial. All 'do think' values collapse to 'high'."""
    body: dict = {"model": "mistral/magistral-small-latest", "messages": []}
    apply_thinking_effort(body, "mistral/magistral-small-latest", agnostic_effort)
    assert body.get("reasoning_effort") == "high"


def test_auto_resolves_to_high():
    """auto → default_effort → 'medium' → Mistral 'high'."""
    body: dict = {"model": "mistral/magistral-small-latest", "messages": []}
    apply_thinking_effort(body, "mistral/magistral-small-latest", "auto")
    assert body.get("reasoning_effort") == "high"


# ─── Disable path ──────────────────────────────────────────────────────────


def test_none_sends_explicit_none_value():
    """Unlike DeepSeek (which uses a `thinking: {disabled}` envelope),
    Mistral has an explicit `reasoning_effort: "none"` value. We
    emit that — NOT omit the field — so the model sees the explicit
    opt-out signal rather than treating absence as default."""
    body: dict = {"model": "mistral/magistral-small-latest", "messages": []}
    apply_thinking_effort(body, "mistral/magistral-small-latest", "none")
    assert body.get("reasoning_effort") == "none"


def test_none_does_not_emit_thinking_envelope():
    """Mistral doesn't use a `thinking: {type}` envelope. The reasoning
    dispatch is purely via the top-level `reasoning_effort` field.
    Make sure we don't accidentally borrow DeepSeek's shape."""
    body: dict = {"model": "mistral/magistral-small-latest", "messages": []}
    apply_thinking_effort(body, "mistral/magistral-small-latest", "none")
    assert "thinking" not in body


def test_high_does_not_emit_thinking_envelope():
    body: dict = {"model": "mistral/magistral-small-latest", "messages": []}
    apply_thinking_effort(body, "mistral/magistral-small-latest", "high")
    assert "thinking" not in body


# ─── Param passthrough — Mistral docs don't strip in reasoning mode ────────


def test_enabling_thinking_preserves_temperature():
    """Unlike DeepSeek, Mistral's docs do NOT call out temperature /
    top_p / penalties as incompatible with reasoning mode. Pass them
    through untouched."""
    body: dict = {
        "model": "mistral/magistral-small-latest",
        "messages": [],
        "temperature": 0.5,
        "top_p": 0.9,
        "presence_penalty": 0.1,
        "frequency_penalty": 0.1,
    }
    apply_thinking_effort(body, "mistral/magistral-small-latest", "high")
    assert body["temperature"] == 0.5
    assert body["top_p"] == 0.9
    assert body["presence_penalty"] == 0.1
    assert body["frequency_penalty"] == 0.1


def test_disable_preserves_temperature():
    body: dict = {
        "model": "mistral/magistral-small-latest",
        "messages": [],
        "temperature": 0.5,
    }
    apply_thinking_effort(body, "mistral/magistral-small-latest", "none")
    assert body["temperature"] == 0.5


def test_preserves_max_tokens_and_tools():
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    body: dict = {
        "model": "mistral/magistral-medium-latest",
        "messages": [],
        "max_tokens": 4096,
        "tools": tools,
    }
    apply_thinking_effort(body, "mistral/magistral-medium-latest", "high")
    assert body["max_tokens"] == 4096
    assert body["tools"] == tools


# ─── Body shape sanity ─────────────────────────────────────────────────────


def test_reasoning_effort_at_top_level():
    """`reasoning_effort` is a top-level Mistral chat-completions body
    field. Pin that the mapper writes it at the root, not nested."""
    body: dict = {"model": "mistral/magistral-small-latest", "messages": []}
    apply_thinking_effort(body, "mistral/magistral-small-latest", "high")
    assert "reasoning_effort" in body
    extra = body.get("extra_body")
    if extra is not None:
        assert "reasoning_effort" not in extra


def test_no_effort_no_changes():
    """effort=None means caller didn't opt in — body untouched even for
    a Magistral model."""
    body: dict = {
        "model": "mistral/magistral-small-latest",
        "messages": [],
        "temperature": 0.5,
    }
    snapshot = dict(body)
    apply_thinking_effort(body, "mistral/magistral-small-latest", None)
    assert body == snapshot


# ─── Cross-provider no-bleed ────────────────────────────────────────────────


def test_magistral_wiring_does_not_break_deepseek():
    """Adding Mistral mustn't change DeepSeek behavior. Quick smoke
    check that DeepSeek V4 still routes through its own block."""
    body: dict = {"model": "deepseek/deepseek-v4-pro", "messages": []}
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    # DeepSeek-specific: thinking envelope + reasoning_effort="max"
    # (NOT Mistral's "high" — confirms _is_deepseek_v4 matched first).
    assert body.get("thinking") == {"type": "enabled"}
    assert body.get("reasoning_effort") == "max"
