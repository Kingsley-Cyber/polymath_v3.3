"""Dependency-free normalization for provider model-card payloads."""

from __future__ import annotations

from typing import Any


# Provider-card metadata consumed by Polymath itself. None of these keys may
# leak into an OpenAI-compatible request body.
INTERNAL_MODEL_FLAGS = frozenset(
    {
        "supports_json_object",
        "supports_json_schema",
        "skip_model_validation",
        "managed_vllm",
        "resource_class",
        "schema_mode",
        "json_repair_mode",
        "semantic_verifier_mode",
        "concurrency_policy",
        "failure_backfill_policy",
        "disable_thinking",
        "local_private",
        "adaptive_vram",
        "vram_safety_ratio",
        "lifecycle_base_url",
        "routing_policy",
        "route_policy",
        "lane_role",
        "route_weight",
        "context_window_tokens",
        "max_context_tokens",
        "canary_max_concurrent",
        "auto_max_concurrent",
        "auto_initial_concurrent",
        "auto_max_concurrent_ceiling",
        "max_concurrent_ceiling",
        "provider_max_concurrent_ceiling",
        "max_concurrent_mode",
        "provider_canary_passed",
        "summary_canary_passed_rows",
    }
)

_RESERVED_PAYLOAD_KEYS = frozenset({"model", "messages", "response_format"})

# Ingestion LLM calls are bounded artifact compilers, not open-ended chat turns.
# Reasoning/thinking controls are intentionally owned by provider cards so a
# corpus/user extra_params blob cannot re-enable hidden reasoning and strand
# summaries/extractions at the output limit.
INGESTION_THINKING_FLAGS = frozenset(
    {
        "thinking",
        "enable_thinking",
        "reasoning",
        "reasoning_effort",
        "thinking_effort",
        "reasoning_budget",
        "thinking_budget",
    }
)


def provider_payload_extras(extra_params: dict[str, Any] | None) -> dict[str, Any]:
    """Return only fields that are safe to send to a model provider."""
    return {
        key: value
        for key, value in (extra_params or {}).items()
        if key not in INTERNAL_MODEL_FLAGS and key not in _RESERVED_PAYLOAD_KEYS
    }


def ingestion_provider_payload_extras(
    extra_params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return provider extras safe for ingestion artifact LLM calls."""
    return {
        key: value
        for key, value in provider_payload_extras(extra_params).items()
        if key not in INGESTION_THINKING_FLAGS
    }
