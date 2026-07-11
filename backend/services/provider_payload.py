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
        "provider_canary_passed",
    }
)

_RESERVED_PAYLOAD_KEYS = frozenset({"model", "messages", "response_format"})


def provider_payload_extras(extra_params: dict[str, Any] | None) -> dict[str, Any]:
    """Return only fields that are safe to send to a model provider."""
    return {
        key: value
        for key, value in (extra_params or {}).items()
        if key not in INTERNAL_MODEL_FLAGS and key not in _RESERVED_PAYLOAD_KEYS
    }
