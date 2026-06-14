"""
Backend mirror of the frontend provider registry in
`frontend/src/types/queryModelPool.ts` → POOL_PROVIDER_PRESETS.

Runtime scope: user-facing model setup stores provider-native model ids
(`glm-5.1`, `deepseek-chat`, `meta-llama/...`) while LiteLLM still needs a
route prefix (`openai/glm-5.1`, `deepseek/deepseek-chat`). Keep that routing
detail here so the UI never has to ask users for LiteLLM-specific formats.

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
    "glm-coding": "openai",
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


PROVIDER_PREFIX_ALIASES: dict[str, set[str]] = {
    "zai": {"zai", "z.ai"},
    "glm-coding": {"glm-coding", "zai", "z.ai"},
    "opencode-go": {"opencode-go"},
    "opencode-go-anthropic": {"opencode-go-anthropic"},
}


def normalize_model_for_litellm(
    preset_id: str | None,
    model_name: str | None,
) -> str:
    """Return the concrete LiteLLM route model for a provider preset.

    The setup UI should accept provider-native model ids without prefixes:
    `deepseek-chat`, `glm-5.1`, `anthropic/claude-sonnet-4.5`, etc. Slashes
    are common inside provider model ids, so slash presence alone does not
    mean the model is already LiteLLM-prefixed.
    """

    model = (model_name or "").strip()
    if not model:
        return model

    route_prefix = litellm_provider_for(preset_id)
    if not route_prefix:
        return model

    if "/" in model:
        head, tail = model.split("/", 1)
        head_key = head.strip().lower()
        route_key = route_prefix.lower()
        if head_key == route_key:
            return model

        preset_key = (preset_id or "").strip().lower()
        aliases = {preset_key, *PROVIDER_PREFIX_ALIASES.get(preset_key, set())}
        if head_key in aliases and tail.strip():
            model = tail.strip()

    return f"{route_prefix}/{model}"
