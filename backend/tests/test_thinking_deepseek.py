"""Per-provider thinking-mapper tests: DeepSeek V4 Flash / Pro.

These tests pin DeepSeek's specific thinking-mode contract:
  - Both models support thinking, default ON
  - Toggle via `thinking: {type: "enabled" | "disabled"}`
  - Effort via `reasoning_effort: "high"` (low/medium/high all collapse
    to "high" per DeepSeek's own normalization)
  - Thinking mode silently ignores temperature / top_p /
    presence_penalty / frequency_penalty — the mapper strips them
    so the wire payload matches what the provider honors

Stays in lockstep with services/thinking_mapper.py's
`_DEEPSEEK_V4_REASONING_EFFORT` / `_DEEPSEEK_V4_THINKING_INCOMPATIBLE_PARAMS`
constants. If you change the mapping, update these tests.
"""

from __future__ import annotations

import pytest

from services.thinking_mapper import apply_thinking_effort


# ─── Detection ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-pro",
        "deepseek-v4-flash",  # bare id (no provider prefix)
        "deepseek-v4-pro",
    ],
)
def test_deepseek_v4_models_get_thinking_block(model):
    """Both Flash and Pro support thinking. Agnostic 'high' reaches
    DeepSeek's top tier 'max' so the user's strongest selection
    actually maps to the strongest provider effort."""
    body: dict = {"model": model, "messages": []}
    apply_thinking_effort(body, model, "high")
    assert body.get("thinking") == {"type": "enabled"}
    assert body.get("reasoning_effort") == "max"


@pytest.mark.parametrize(
    "model",
    [
        # Older DeepSeek models — NOT wired (per user spec — only v4
        # flash/pro). These should fall through to the no-op path.
        "deepseek/deepseek-chat",
        "deepseek/deepseek-reasoner",
        "deepseek/deepseek-r1",
        "deepseek/deepseek-v3",
        # Other providers — must stay no-op too.
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-5",
        "gemini/gemini-2.5-flash",
    ],
)
def test_other_models_unchanged(model):
    body: dict = {"model": model, "messages": []}
    snapshot = dict(body)
    apply_thinking_effort(body, model, "high")
    assert body == snapshot, (
        f"mapper mutated body for {model!r} — this provider isn't wired "
        f"yet. Check _is_deepseek_v4 doesn't over-match."
    )


# ─── Effort → reasoning_effort mapping ─────────────────────────────────────


@pytest.mark.parametrize(
    "agnostic_effort,expected_value",
    [
        # DeepSeek has two real tiers: "high" (lower) and "max" (top).
        # Our agnostic 5-level enum maps so the user's top selection
        # ("high") reaches DeepSeek's top tier ("max"). low/medium
        # both map to DeepSeek's lower tier — they're effectively
        # aliases of each other within DeepSeek's compat normalization.
        ("low", "high"),     # lower DeepSeek tier
        ("medium", "high"),  # lower DeepSeek tier (same effective effort)
        ("high", "max"),     # top DeepSeek tier
    ],
)
def test_effort_levels_use_both_deepseek_tiers(agnostic_effort, expected_value):
    body: dict = {"model": "deepseek/deepseek-v4-pro", "messages": []}
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", agnostic_effort)
    assert body.get("reasoning_effort") == expected_value


def test_auto_resolves_to_lower_tier():
    """auto → default_effort → 'medium' → DeepSeek 'high' (lower tier).

    'auto' is the safe default — users who haven't picked a level
    shouldn't be burning the top DeepSeek tier on every query. They
    have to explicitly pick 'high' to reach 'max'."""
    body: dict = {"model": "deepseek/deepseek-v4-pro", "messages": []}
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "auto")
    assert body.get("thinking") == {"type": "enabled"}
    assert body.get("reasoning_effort") == "high"


# ─── Disable path ──────────────────────────────────────────────────────────


def test_none_explicitly_disables_thinking():
    """The DeepSeek default is enabled, so 'none' must explicitly emit
    the disable signal — otherwise thinking would silently stay on."""
    body: dict = {"model": "deepseek/deepseek-v4-pro", "messages": []}
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "none")
    assert body.get("thinking") == {"type": "disabled"}
    # When disabling, no reasoning_effort should be sent — it's
    # meaningless without thinking enabled.
    assert "reasoning_effort" not in body


def test_disable_does_not_strip_temperature():
    """Only thinking-mode strips temp/top_p/etc. When thinking is
    DISABLED, those params should pass through untouched (non-thinking
    mode supports them all per the docs)."""
    body: dict = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [],
        "temperature": 0.5,
        "top_p": 0.9,
    }
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "none")
    assert body.get("thinking") == {"type": "disabled"}
    assert body["temperature"] == 0.5
    assert body["top_p"] == 0.9


# ─── Incompatible-param stripping ──────────────────────────────────────────


def test_enabling_thinking_strips_temperature():
    body: dict = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [],
        "temperature": 0.7,
    }
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    assert "temperature" not in body


def test_enabling_thinking_strips_top_p():
    body: dict = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [],
        "top_p": 0.9,
    }
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    assert "top_p" not in body


def test_enabling_thinking_strips_presence_and_frequency_penalty():
    body: dict = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [],
        "presence_penalty": 0.5,
        "frequency_penalty": 0.5,
    }
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    assert "presence_penalty" not in body
    assert "frequency_penalty" not in body


def test_enabling_thinking_preserves_max_tokens():
    """max_tokens is NOT in the incompatible list and must pass through.
    DeepSeek-V4 max output is 384K so users frequently want to cap it."""
    body: dict = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [],
        "max_tokens": 16384,
    }
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    assert body.get("max_tokens") == 16384


def test_enabling_thinking_preserves_tools():
    """Tool calls are supported in thinking mode per the docs.
    They must survive the mapper's strip pass."""
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    body: dict = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [],
        "tools": tools,
    }
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    assert body.get("tools") == tools


# ─── Body shape sanity ─────────────────────────────────────────────────────


def test_thinking_value_is_dict_not_string():
    """LiteLLM and DeepSeek both expect a dict shape `{"type": ...}`,
    not a bare string. Pinning this so a refactor doesn't accidentally
    flatten the structure."""
    body: dict = {"model": "deepseek/deepseek-v4-pro", "messages": []}
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    thinking = body.get("thinking")
    assert isinstance(thinking, dict)
    assert thinking.get("type") == "enabled"


def test_reasoning_effort_at_top_level_not_in_extra_body():
    """We send `reasoning_effort` at the top level of the JSON body
    (the LiteLLM REST contract), NOT nested in `extra_body`. The
    OpenAI SDK `extra_body` trick is for SDK callers, not for our
    proxy-style body construction."""
    body: dict = {"model": "deepseek/deepseek-v4-pro", "messages": []}
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    assert "reasoning_effort" in body
    extra = body.get("extra_body")
    if extra is not None:
        # If extra_body exists for unrelated reasons, our key must
        # NOT have been nested inside it.
        assert "reasoning_effort" not in extra


def test_no_effort_no_thinking_block():
    """effort=None means caller didn't opt in — no thinking block,
    body untouched. This is the OPPOSITE of DeepSeek's API default
    (which is enabled), but our agnostic contract is opt-in
    everywhere. Callers wanting DeepSeek's default behavior should
    pass effort='auto' or 'high' explicitly."""
    body: dict = {
        "model": "deepseek/deepseek-v4-pro",
        "messages": [],
        "temperature": 0.5,
    }
    snapshot = dict(body)
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", None)
    assert body == snapshot
