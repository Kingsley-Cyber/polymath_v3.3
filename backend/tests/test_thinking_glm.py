"""Per-provider thinking-mapper tests: GLM (Z.AI).

GLM has the simplest dial we've wired so far — pure binary, no effort
levels at all. `thinking.type` is either `enabled` (default) or
`disabled`. All of our agnostic thinking tiers (auto/low/medium/high)
collapse to `enabled`; only "none" maps to `disabled`.

Models with thinking support (per Z.AI docs, 2026-05-15):
  - glm-5, glm-5.1, glm-5-turbo, glm-5v-turbo
  - glm-4.5, glm-4.6, glm-4.7

Models WITHOUT thinking support (must stay no-op):
  - glm-4, glm-4-plus, glm-4-0520
  - glm-3-turbo
  - Anything not in the GLM family
"""

from __future__ import annotations

import pytest

from services.thinking_mapper import apply_thinking_effort


# ─── Detection — GLM thinking models match ─────────────────────────────────


@pytest.mark.parametrize(
    "model",
    [
        # Bare ids
        "glm-5",
        "glm-5.1",
        "glm-5-turbo",
        "glm-5v-turbo",
        "glm-4.5",
        "glm-4.6",
        "glm-4.7",
        # With LiteLLM-style provider prefix
        "zai/glm-5",
        "zai/glm-5.1",
        "zai/glm-4.7",
        "z-ai/glm-5",
    ],
)
def test_glm_thinking_models_get_enabled_block(model):
    body: dict = {"model": model, "messages": []}
    apply_thinking_effort(body, model, "high")
    assert body.get("thinking") == {"type": "enabled"}


@pytest.mark.parametrize(
    "model",
    [
        # Older GLM models — non-thinking, must stay no-op
        "glm-4",
        "glm-4-plus",
        "glm-4-0520",
        "glm-3-turbo",
        # Other providers — still no-op
        "openai/gpt-4o",
        "anthropic/claude-sonnet-4-5",
        "mistral/mistral-large-latest",  # non-Magistral
        "deepseek/deepseek-chat",
    ],
)
def test_non_glm_thinking_models_unchanged(model):
    body: dict = {"model": model, "messages": []}
    snapshot = dict(body)
    apply_thinking_effort(body, model, "high")
    assert body == snapshot, (
        f"mapper mutated body for {model!r} — _is_glm_thinking is "
        f"over-matching. Should match only models in _GLM_THINKING_MODELS."
    )


# ─── Binary collapse — all "do think" levels → enabled ─────────────────────


@pytest.mark.parametrize(
    "agnostic_effort", ["low", "medium", "high", "auto"]
)
def test_all_do_think_efforts_emit_enabled(agnostic_effort):
    """GLM has no effort gradient. low/medium/high/auto all flatten to
    {type: enabled}. The model itself auto-decides depth per the docs."""
    body: dict = {"model": "glm-5.1", "messages": []}
    apply_thinking_effort(body, "glm-5.1", agnostic_effort)
    assert body.get("thinking") == {"type": "enabled"}


def test_none_emits_disabled():
    body: dict = {"model": "glm-5.1", "messages": []}
    apply_thinking_effort(body, "glm-5.1", "none")
    assert body.get("thinking") == {"type": "disabled"}


def test_no_effort_no_change():
    """effort=None → body untouched even on a thinking-capable model."""
    body: dict = {"model": "glm-5.1", "messages": [], "temperature": 0.5}
    snapshot = dict(body)
    apply_thinking_effort(body, "glm-5.1", None)
    assert body == snapshot


def test_llm_request_body_auto_enables_glm_by_default(monkeypatch):
    """The chat UI omits auto from the wire, so LLMService must still
    resolve provider-default thinking for GLM. Otherwise [THINK: AUTO]
    becomes an accidental no-op and GLM streams no reasoning."""
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test")
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret-key-for-unit-tests")
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "test-password")
    from services.llm import LLMService

    body = LLMService()._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/GLM-5.1",
    )
    assert body.get("thinking") == {"type": "enabled"}


def test_explicit_auto_wins_over_pool_extra_disabled(monkeypatch):
    """Saved model-pool extras may carry `thinking: disabled` from older
    configs. When the UI sends AUTO explicitly, the per-turn selector must
    re-enable GLM thinking after extras are merged.
    """
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test")
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret-key-for-unit-tests")
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "test-password")
    from models.schemas import ModelOverrides
    from services.llm import LLMService

    overrides = ModelOverrides(thinking_effort="auto")
    body = LLMService()._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/GLM-5.1",
        overrides=overrides,
    )
    body["thinking"] = {"type": "disabled"}

    LLMService()._reapply_explicit_thinking_effort(
        body,
        "openai/GLM-5.1",
        overrides,
    )

    assert body.get("thinking") == {"type": "enabled"}


def test_explicit_none_wins_over_pool_extra_enabled(monkeypatch):
    """The same precedence must preserve the user's explicit off switch."""
    monkeypatch.setenv("LITELLM_MASTER_KEY", "test")
    monkeypatch.setenv("AUTH_SECRET_KEY", "test-secret-key-for-unit-tests")
    monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "test-password")
    from models.schemas import ModelOverrides
    from services.llm import LLMService

    overrides = ModelOverrides(thinking_effort="none")
    body = LLMService()._build_request_body(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/GLM-5.1",
        overrides=overrides,
    )
    body["thinking"] = {"type": "enabled"}

    LLMService()._reapply_explicit_thinking_effort(
        body,
        "openai/GLM-5.1",
        overrides,
    )

    assert body.get("thinking") == {"type": "disabled"}


# ─── No reasoning_effort emitted ───────────────────────────────────────────


def test_no_reasoning_effort_field_emitted():
    """GLM's thinking control is purely via `thinking.type`. The
    `reasoning_effort` field is NOT part of GLM's API — make sure
    we don't borrow DeepSeek's or Mistral's shape."""
    body: dict = {"model": "glm-4.7", "messages": []}
    apply_thinking_effort(body, "glm-4.7", "high")
    assert "reasoning_effort" not in body


def test_disabled_path_also_no_reasoning_effort():
    body: dict = {"model": "glm-4.7", "messages": []}
    apply_thinking_effort(body, "glm-4.7", "none")
    assert "reasoning_effort" not in body


# ─── Param passthrough — GLM doesn't strip in thinking mode ────────────────


def test_thinking_preserves_temperature_and_top_p():
    """GLM's docs don't list any thinking-incompatible body params.
    Temperature, top_p, max_tokens, tools all pass through."""
    body: dict = {
        "model": "glm-5.1",
        "messages": [],
        "temperature": 0.7,
        "top_p": 0.9,
        "max_tokens": 4096,
    }
    apply_thinking_effort(body, "glm-5.1", "high")
    assert body["temperature"] == 0.7
    assert body["top_p"] == 0.9
    assert body["max_tokens"] == 4096


def test_thinking_preserves_tools():
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    body: dict = {"model": "glm-4.7", "messages": [], "tools": tools}
    apply_thinking_effort(body, "glm-4.7", "high")
    assert body["tools"] == tools


def test_disable_preserves_temperature():
    body: dict = {"model": "glm-5.1", "messages": [], "temperature": 0.5}
    apply_thinking_effort(body, "glm-5.1", "none")
    assert body["temperature"] == 0.5


# ─── Body shape sanity ─────────────────────────────────────────────────────


def test_thinking_is_dict_not_string():
    body: dict = {"model": "glm-5.1", "messages": []}
    apply_thinking_effort(body, "glm-5.1", "high")
    thinking = body.get("thinking")
    assert isinstance(thinking, dict)
    assert thinking.get("type") in ("enabled", "disabled")


def test_thinking_at_top_level_not_extra_body():
    """GLM's REST API accepts `thinking` at the JSON root (per the
    cURL examples in the docs). The Python SDK examples nest it in
    extra_body to bypass SDK strips — that's an SDK-side concern,
    not the API contract."""
    body: dict = {"model": "glm-5.1", "messages": []}
    apply_thinking_effort(body, "glm-5.1", "high")
    assert "thinking" in body
    extra = body.get("extra_body")
    if extra is not None:
        assert "thinking" not in extra


# ─── Cross-provider no-bleed ────────────────────────────────────────────────


def test_glm_wiring_does_not_break_deepseek():
    """DeepSeek V4 still emits its own thinking-envelope + reasoning_effort."""
    body: dict = {"model": "deepseek/deepseek-v4-pro", "messages": []}
    apply_thinking_effort(body, "deepseek/deepseek-v4-pro", "high")
    assert body.get("thinking") == {"type": "enabled"}
    assert body.get("reasoning_effort") == "max"


def test_glm_wiring_does_not_break_mistral_magistral():
    """Mistral Magistral still emits a flat reasoning_effort and NO
    thinking envelope."""
    body: dict = {"model": "mistral/magistral-small-latest", "messages": []}
    apply_thinking_effort(body, "mistral/magistral-small-latest", "high")
    assert body.get("reasoning_effort") == "high"
    assert "thinking" not in body
