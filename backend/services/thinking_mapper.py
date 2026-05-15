"""
Phase 28 — Thinking-effort normalizer.

Maps a provider-agnostic thinking_effort enum to provider-native body
params. LiteLLM handles most passthrough natively; this module knows the
shape each provider expects.

Usage:
    from services.thinking_mapper import apply_thinking_effort
    body = {"model": "anthropic/claude-sonnet-4-6", "messages": [...]}
    apply_thinking_effort(body, "anthropic/claude-sonnet-4-6", "medium")
    # body now contains {"thinking": {"type": "enabled", "budget_tokens": 8192}}

Synthesis (Ghost A/B) always calls with thinking_effort="none" unless the
corpus config explicitly overrides it — reasoning tokens waste the output
budget on structured extraction/summarization.
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

ThinkingEffort = Literal["none", "low", "medium", "high", "auto"]

# ── Provider-specific token budgets (Anthropic-style) ────────────────────
_ANTHROPIC_BUDGETS: dict[ThinkingEffort, int] = {
    "none": 0,       # disabled via type=disabled
    "low": 2048,
    "medium": 8192,
    "high": 32000,
}

# Gemini thinking_budget (experimental, gemini-2.5-flash+)
_GEMINI_BUDGETS: dict[ThinkingEffort, int] = {
    "none": 0,
    "low": 1024,
    "medium": 4096,
    "high": 24576,
}

# OpenAI o-series reasoning_effort is a string enum
_OPENAI_REASONING_EFFORT: dict[ThinkingEffort, str] = {
    "none": "low",    # o-series has no "none"; low is minimal
    "low": "low",
    "medium": "medium",
    "high": "high",
}


def _provider_from_model(model: str) -> str:
    """Extract provider prefix from a LiteLLM model string."""
    if not model:
        return ""
    if "/" in model:
        return model.split("/", 1)[0].lower()
    return ""


def _is_anthropic(model: str, provider: str) -> bool:
    return provider == "anthropic" or "claude" in model.lower()


def _is_openai_reasoning(model: str, provider: str) -> bool:
    m = model.lower()
    return provider == "openai" and ("o1" in m or "o3" in m or "o4" in m)


def _is_deepseek(model: str, provider: str) -> bool:
    return provider == "deepseek" or "deepseek" in model.lower()


def _is_gemini(model: str, provider: str) -> bool:
    return provider in ("gemini", "google") or "gemini" in model.lower()


def apply_thinking_effort(
    body: dict,
    model: str,
    effort: ThinkingEffort | None,
    *,
    default_effort: ThinkingEffort = "auto",
) -> None:
    """Mutate ``body`` in place to add the provider-native thinking
    parameter for ``model``, given the agnostic ``effort`` value.

    - ``effort=None``: caller didn't ask for thinking; this is a no-op.
      Callers who want a global default should pass an explicit value.
    - ``effort="auto"``: resolves to ``default_effort``. For most models
      this means "medium"; for Anthropic it means enabled-with-no-budget
      (let the model self-budget); see provider blocks below.
    - ``effort="none"``: explicit disable. For OpenAI we map to "low"
      (the o-series has no off switch); for Anthropic we emit
      ``thinking: {type: "disabled"}``; for Gemini we set budget to 0.
    - All other values: routed through the per-provider budget map.

    No-ops cleanly for non-reasoning models (gpt-4o, chat-shaped Claude
    Haiku, gemini-1.5, etc.) — gating is by ``_is_*`` helpers above.
    """
    if effort is None:
        return  # caller didn't opt in

    # Resolve "auto" to the default. Keep "auto" out of provider maps
    # so the maps only contain concrete values.
    if effort == "auto":
        effort = default_effort
    if effort == "auto":
        # default_effort was also "auto" — treat as medium.
        effort = "medium"

    provider = _provider_from_model(model)

    # ── Anthropic Claude ──────────────────────────────────────────────
    # API shape: thinking: {type: "enabled", budget_tokens: N}
    # or         thinking: {type: "disabled"} for explicit off.
    if _is_anthropic(model, provider):
        if effort == "none":
            body["thinking"] = {"type": "disabled"}
            return
        budget = _ANTHROPIC_BUDGETS.get(effort, _ANTHROPIC_BUDGETS["medium"])
        body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Claude requires max_tokens > thinking budget; pad if the caller
        # didn't set one or set one too small.
        existing_max = body.get("max_tokens")
        floor = budget + 1024
        if not isinstance(existing_max, int) or existing_max < floor:
            body["max_tokens"] = floor
        return

    # ── OpenAI o-series ───────────────────────────────────────────────
    # API shape: reasoning_effort: "low" | "medium" | "high"
    # The o-series doesn't have an off switch; "none" maps to "low".
    if _is_openai_reasoning(model, provider):
        body["reasoning_effort"] = _OPENAI_REASONING_EFFORT.get(
            effort, _OPENAI_REASONING_EFFORT["medium"]
        )
        return

    # ── Gemini 2.5+ ───────────────────────────────────────────────────
    # API shape: thinking_budget: N (camelCase thinkingBudget also works
    # at the Google SDK level; LiteLLM normalizes either).
    if _is_gemini(model, provider):
        # Gate on 2.5+ — pre-2.5 Gemini has no thinking dial. Be lenient
        # and apply the budget anyway; LiteLLM will drop_params if the
        # model doesn't accept it. Better to over-emit than miss.
        if effort == "none":
            body["thinking_budget"] = 0
            return
        body["thinking_budget"] = _GEMINI_BUDGETS.get(
            effort, _GEMINI_BUDGETS["medium"]
        )
        return

    # ── DeepSeek R1 / reasoner ────────────────────────────────────────
    # DeepSeek-R1 has no effort dial — it always reasons and returns
    # `reasoning_content` alongside `content`. We don't emit anything;
    # the UI can still SHOW the selector to inform the user, but the
    # backend correctly no-ops.
    if _is_deepseek(model, provider):
        return

    # ── Anything else ─────────────────────────────────────────────────
    # Non-reasoning model. No-op — the caller's body is unchanged.
    return
