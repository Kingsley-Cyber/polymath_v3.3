"""thinking_mapper — provider-agnostic thinking/reasoning-effort dial.

CURRENT STATE: blank slate. No providers configured. `apply_thinking_effort`
is wired into LLMService._build_request_body but is a no-op for every
model until a provider block is added below.

Workflow for adding a provider:

  1. Add a detector predicate (e.g. `_is_<provider>(model, provider)`)
     that returns True for the model patterns the provider exposes a
     thinking dial on. Examples per provider's current docs (you will
     supply these):
       - "all models matching pattern X"
       - "all models from this provider"
       - "specific named models"

  2. Add a per-provider effort mapping. The shape depends on the API:
       - String-enum APIs (e.g. low/medium/high) → dict[ThinkingEffort, str]
       - Token-budget APIs → dict[ThinkingEffort, int]
       - Boolean enable APIs → dict[ThinkingEffort, bool]
       - Anything else — model the dict to whatever the provider accepts.

  3. Add a block inside `apply_thinking_effort` that gates on the
     detector and mutates `body` with the provider-native key/value.

  4. If the provider's thinking mode imposes ordering constraints on
     other body params (e.g. "max_tokens must exceed budget"), apply
     those guards in the same block.

Effort semantics (agnostic, never provider-specific):
  - "auto":   resolve to default_effort, then "medium" if still auto
  - "none":   user explicitly disabled thinking
  - "low" | "medium" | "high": user-chosen budget tier

`effort=None` means the caller didn't opt in; the function is a no-op.
This lets non-thinking-aware call sites pass `None` without any branch
in the caller — the mapper itself handles the "do nothing" path.

Example usage:

    from services.thinking_mapper import apply_thinking_effort
    apply_thinking_effort(body, "openai/o3-mini", "high")
    # body now has whatever the OpenAI o-series block emits.
"""

from __future__ import annotations

import logging
from typing import Literal

logger = logging.getLogger(__name__)

ThinkingEffort = Literal["none", "low", "medium", "high", "auto"]


# ─── Provider-specific maps go here ──────────────────────────────────────
# Add entries when a provider is wired. Each entry should be a dict keyed
# by ThinkingEffort that returns the provider-native value. Keep the
# concrete value type provider-specific (str / int / dict / bool / ...).

# ── DeepSeek V4 (deepseek-v4-flash, deepseek-v4-pro) ─────────────────────
# Source: DeepSeek thinking-mode docs (user-supplied, verified 2026-05-15).
#   - Thinking is DEFAULT ON; toggle via `thinking: {"type": "enabled" |
#     "disabled"}`.
#   - Effort dial via `reasoning_effort: "high" | "max"`.
#   - DeepSeek internally maps "low" and "medium" → "high"; "xhigh" → "max".
#   - In thinking mode, temperature/top_p/presence_penalty/
#     frequency_penalty are silently ignored. The mapper strips them so
#     the body the user sees matches what the provider actually honors.
#
# We don't yet expose "xhigh" in the agnostic ThinkingEffort enum, so
# "max" is unreachable through this mapper today. When/if we add xhigh,
# add `"xhigh": "max"` to _DEEPSEEK_V4_REASONING_EFFORT and update
# ThinkingEffort. For now low/medium/high all collapse to "high",
# matching DeepSeek's own normalization.
_DEEPSEEK_V4_REASONING_EFFORT: dict[ThinkingEffort, str] = {
    "low": "high",
    "medium": "high",
    "high": "high",
}

# Body params that thinking mode silently ignores. Stripping them keeps
# the user's mental model honest: if temperature=0 was set, they'd
# expect deterministic output, but DeepSeek thinking-mode reads it as
# "no effect" — surprising. Better to drop and log.
_DEEPSEEK_V4_THINKING_INCOMPATIBLE_PARAMS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
)


# ─── Provider detectors ──────────────────────────────────────────────────
# Each predicate returns True iff the (model, provider) pair belongs to
# a provider whose thinking-dial we've wired below. Add new predicates
# under their own header comment so the wiring is easy to audit.


def _provider_from_model(model: str) -> str:
    """Extract LiteLLM-style 'provider/' prefix. Empty when the model
    string is bare (e.g. 'gpt-4o' rather than 'openai/gpt-4o')."""
    if not model:
        return ""
    if "/" in model:
        return model.split("/", 1)[0].lower()
    return ""


def _is_deepseek_v4(model: str, provider: str) -> bool:
    """Match DeepSeek-V4-Flash and DeepSeek-V4-Pro. Excludes older
    DeepSeek models (deepseek-chat, deepseek-reasoner, deepseek-r1)
    which either have no dial (chat) or don't follow the v4 toggle
    contract (reasoner / R1 are always-on with no off switch).
    """
    if provider == "deepseek":
        m = model.lower()
        return "v4" in m or "v4-flash" in m or "v4-pro" in m
    # Bare model id without provider/ prefix (some callers strip it).
    m = model.lower()
    return "deepseek-v4-flash" in m or "deepseek-v4-pro" in m


# ─── Public entry point ──────────────────────────────────────────────────


def apply_thinking_effort(
    body: dict,
    model: str,
    effort: ThinkingEffort | None,
    *,
    default_effort: ThinkingEffort = "auto",
) -> None:
    """Mutate ``body`` in place to add the provider-native thinking
    parameter for ``model``, given the agnostic ``effort`` value.

    With no providers configured (current state), this is always a
    no-op. The structural plumbing (caller in LLMService, the schema
    field on ModelOverrides, the UI selector) is in place so adding
    a provider is a localized edit to this file only.
    """
    if effort is None:
        return  # caller didn't opt in

    # Resolve "auto" to the default, then to "medium" if still auto.
    # Keep "auto" out of provider maps — those maps only contain
    # concrete effort levels.
    if effort == "auto":
        effort = default_effort
    if effort == "auto":
        effort = "medium"

    provider = _provider_from_model(model)

    # ── DeepSeek V4 (Flash / Pro) ────────────────────────────────────
    # Wire: `thinking: {type: enabled|disabled}` toggle + optional
    # `reasoning_effort: "high"`. When thinking is enabled, strip
    # body params DeepSeek silently ignores so the wire payload
    # honestly reflects what the provider will honor.
    if _is_deepseek_v4(model, provider):
        if effort == "none":
            # Explicit disable. Per the docs, default is ENABLED, so we
            # must send the disable signal explicitly to opt out.
            body["thinking"] = {"type": "disabled"}
            return
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = _DEEPSEEK_V4_REASONING_EFFORT.get(
            effort, "high"
        )
        # Strip thinking-incompatible params silently — they would be
        # ignored by DeepSeek anyway, but removing them keeps the body
        # honest and avoids user confusion ("I set temperature=0, why
        # is the output non-deterministic?").
        for key in _DEEPSEEK_V4_THINKING_INCOMPATIBLE_PARAMS:
            body.pop(key, None)
        return

    # No matching provider → silent no-op. Body unchanged.
    _ = provider  # keep the local for the next provider's gating
    return
