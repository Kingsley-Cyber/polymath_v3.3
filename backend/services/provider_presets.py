"""
Backend mirror of the frontend provider registry in
`frontend/src/types/queryModelPool.ts` → POOL_PROVIDER_PRESETS.

Narrow scope: the migration only needs `id → litellm_provider` to rewrite
bare model names into provider-prefixed ones (e.g. "deepseek-chat" →
"deepseek/deepseek-chat"). Base URLs, example models, and kwargs live
on the frontend — they're UX defaults, not runtime routing state.

Keep this dict in lockstep with the TS registry. When adding a provider,
update both files in one edit.
"""

from __future__ import annotations

# id (UI preset key) → litellm_provider (wildcard-router prefix).
# The "custom" entry maps to "openai" because that's the generic
# OpenAI-compatible passthrough — but a custom entry without a known
# preset id will not trigger migration (see `litellm_provider_for`).
PROVIDER_PRESET_PREFIX: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "deepseek": "deepseek",
    "google": "gemini",
    "mistral": "mistral",
    "groq": "groq",
    "moonshot": "openai",
    "together": "together_ai",
    "xai": "xai",
    "mimo": "openai",
    "siliconflow": "openai",
    "zai": "openai",
    "openrouter": "openrouter",
    "ollama": "ollama",
    "custom": "openai",
}


def litellm_provider_for(preset_id: str | None) -> str | None:
    """Return the LiteLLM prefix for a preset id, or None if unknown.

    The migration treats an unknown preset as "do not rewrite" — this guards
    user-authored entries with hand-crafted model strings against being
    clobbered.
    """
    if not preset_id:
        return None
    return PROVIDER_PRESET_PREFIX.get(preset_id)
