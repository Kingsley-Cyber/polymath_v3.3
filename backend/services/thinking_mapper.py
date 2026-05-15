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
# Our agnostic ThinkingEffort enum has 4 thinking levels
# (low/medium/high — "none" is the disable signal, handled separately).
# DeepSeek has 2 real tiers (high and max; their `low` / `medium` /
# `xhigh` are aliases). We map so the user's top selection actually
# reaches DeepSeek's top tier:
#
#   agnostic "low"    → DeepSeek "high"  (lower tier, lighter budget)
#   agnostic "medium" → DeepSeek "high"  (lower tier, lighter budget)
#   agnostic "high"   → DeepSeek "max"   (top tier, max budget)
#
# Rationale: if a user picks "high" expecting maximum reasoning, they
# should NOT silently get DeepSeek's lower thinking tier. The two
# real DeepSeek levels are exposed as low/medium (lower budget) vs
# high (maximum budget). "auto" resolves to medium → high (lower
# DeepSeek tier), which is also the sane default for autonomous use.
_DEEPSEEK_V4_REASONING_EFFORT: dict[ThinkingEffort, str] = {
    "low": "high",
    "medium": "high",
    "high": "max",
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


# ── GLM (Z.AI: glm-4.5 / 4.6 / 4.7 / 5 / 5.1 / 5-turbo / 5v-turbo) ───────
# Source: Z.AI GLM thinking-mode docs (user-supplied, 2026-05-15).
#   - Pure BINARY toggle. No effort dial at all — `thinking.type` is
#     either "enabled" (default — model auto-decides depth) or
#     "disabled".
#   - Default is ENABLED. We send the toggle explicitly anyway so the
#     wire payload is self-describing.
#   - GLM exposes additional thinking-mode features
#     (clear_thinking: False for preserved context, interleaved
#     thinking for tool calls) but those are content-flow concerns
#     not effort knobs — they stay opt-in via direct extra_body
#     manipulation, not through the agnostic ThinkingEffort enum.
#   - Body field is `thinking: {type}` at the JSON root (per the
#     cURL examples). The Python SDK examples nest it in extra_body
#     to bypass the SDK's unknown-param strip; we hit the LiteLLM
#     REST endpoint directly so top-level is the right placement.
#
# Models with thinking support (per the docs' enumeration of
# "GLM-5.1 GLM-5 GLM-5-Turbo GLM-5V-Turbo GLM-4.5 GLM-4.6 GLM-4.7"):
#   - glm-5*, glm-5.x, glm-5-turbo, glm-5v-turbo (all glm-5-line)
#   - glm-4.5, glm-4.6, glm-4.7
# Models EXCLUDED:
#   - glm-4, glm-4-plus, glm-4-0520, glm-3-* — non-thinking chat
#     models; selector hidden.
#
# No effort map needed (binary toggle). Effort collapse is handled
# inline in the provider block below.
_GLM_THINKING_MODELS: tuple[str, ...] = (
    "glm-5.1",
    "glm-5-turbo",
    "glm-5v-turbo",
    "glm-5",       # bare glm-5 (substring covers future minor variants)
    "glm-4.5",
    "glm-4.6",
    "glm-4.7",
)


# ── Mistral Magistral (magistral-small, magistral-medium, …) ─────────────
# Source: Mistral chat-completions API docs (user-supplied, 2026-05-15).
#   - Dial is BINARY: `reasoning_effort: "high" | "none"`. No low/medium
#     tiers exposed on the API surface.
#   - The dial is only meaningful for "reasoning models" per the docs.
#     We gate on the Magistral line (Mistral's named reasoning family)
#     rather than all mistral-* models so the UI selector only appears
#     for models where it actually does something.
#   - Mistral's docs do NOT call out any thinking-incompatible body
#     params (unlike DeepSeek). temperature / top_p / penalties pass
#     through untouched.
#
# Effort collapse: our 4 agnostic thinking tiers (low/medium/high all
# meaning "do think") map to Mistral "high". Only "none" maps to "none".
# This is the tightest possible mapping given Mistral's binary surface.
_MISTRAL_REASONING_EFFORT: dict[ThinkingEffort, str] = {
    "low": "high",
    "medium": "high",
    "high": "high",
}


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


def _is_glm_thinking(model: str, provider: str) -> bool:
    """Match GLM models that support thinking mode (GLM-4.5+ / 5+).

    Substring check against the curated _GLM_THINKING_MODELS tuple
    so future minor variants (e.g. glm-5.2, glm-5-turbo-preview) are
    matched without needing an explicit entry — as long as the name
    contains a recognized base id.

    Excluded by design: glm-4, glm-4-plus, glm-4-0520, glm-3-* —
    these are non-thinking chat models per the docs.
    """
    m = model.lower()
    return any(name in m for name in _GLM_THINKING_MODELS)


def _is_mistral_magistral(model: str, provider: str) -> bool:
    """Match Mistral's Magistral reasoning line (magistral-small,
    magistral-medium, and future variants under the same name).

    Other Mistral models (mistral-small, mistral-large, mistral-medium,
    open-mistral-7b, codestral-*, ...) are NOT reasoning models and
    don't have a meaningful effort dial — exclude them so the UI
    selector doesn't appear for them.
    """
    m = model.lower()
    if provider == "mistral":
        return "magistral" in m
    return "magistral" in m  # bare id, no provider/ prefix


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

    # ── GLM (Z.AI: glm-4.5+ and glm-5 line) ──────────────────────────
    # Pure binary toggle. ALL "do think" effort levels (low/medium/
    # high/auto) collapse to {type: "enabled"}; "none" → {type:
    # "disabled"}. GLM defaults to enabled — we send it explicitly
    # anyway so the wire payload is self-describing and a debug log
    # makes the user's intent legible.
    if _is_glm_thinking(model, provider):
        if effort == "none":
            body["thinking"] = {"type": "disabled"}
            return
        body["thinking"] = {"type": "enabled"}
        return

    # ── Mistral Magistral (small / medium / future variants) ────────
    # Binary dial: reasoning_effort: "high" | "none". All four agnostic
    # thinking tiers (low/medium/high) collapse to "high"; "none"
    # maps to Mistral's explicit "none" value (NOT omitted — Mistral
    # treats absence vs "none" differently, and we want the explicit
    # opt-out signal).
    if _is_mistral_magistral(model, provider):
        if effort == "none":
            body["reasoning_effort"] = "none"
            return
        body["reasoning_effort"] = _MISTRAL_REASONING_EFFORT.get(effort, "high")
        return

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
