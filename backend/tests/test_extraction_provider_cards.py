from __future__ import annotations

from services.extraction_provider_cards import (
    extraction_lane_uses_private_vllm,
    provider_payload_defaults,
    resolve_extraction_provider_card,
)


def test_local_private_vllm_card_is_adaptive_schema_lane():
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "vllm-rtx",
            "model": "openai/polymath-extract",
            "base_url": "http://192.168.1.83:8000/v1",
            "lifecycle_base_url": "http://192.168.1.83:8085",
            "extra_params": {"managed_vllm": True, "resource_class": "rtx"},
        }
    )

    assert card.provider == "local_private_vllm"
    assert card.schema_mode == "json_schema"
    assert card.supports_json_schema is True
    assert card.concurrency_policy == "adaptive_vram_85"
    assert card.local_private is True
    assert card.managed_vllm is True
    assert "semantic_direction_check" in card.promotion_gate


def test_openrouter_mistral_nemo_uses_native_json_schema():
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "openrouter",
            "model": "openrouter/mistralai/mistral-nemo",
            "base_url": "https://openrouter.ai/api/v1",
        }
    )

    assert card.provider == "openrouter"
    assert card.schema_mode == "json_schema"
    assert card.supports_json_schema is True


def test_longcat_is_compiler_gated_and_disables_thinking():
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "longcat",
            "model": "openai/LongCat-2.0",
            "base_url": "https://api.longcat.chat/openai/v1",
        }
    )

    assert card.schema_mode == "json_object_prompt"
    assert card.supports_json_schema is False
    assert card.json_repair_mode == "deterministic_compiler"
    assert provider_payload_defaults(card) == {"thinking": {"type": "disabled"}}


def test_siliconflow_hy3_is_prompt_json_not_native_schema():
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "siliconflow",
            "model": "openai/tencent/Hy3-preview",
            "base_url": "https://api.siliconflow.com/v1",
        }
    )

    assert card.provider == "siliconflow"
    assert card.schema_mode == "json_object_prompt"
    assert card.supports_json_schema is False
    assert card.json_repair_mode == "deterministic_compiler"


def test_explicit_schema_flag_can_promote_unknown_provider():
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "custom",
            "model": "openai/custom-extract",
            "base_url": "https://example.test/v1",
            "extra_params": {"supports_json_schema": True},
        }
    )

    assert card.schema_mode == "json_schema"
    assert card.supports_json_schema is True


def test_private_vllm_detector_uses_provider_card():
    assert extraction_lane_uses_private_vllm(
        {
            "model": "openai/polymath-extract",
            "base_url": "http://192.168.1.83:8000/v1",
        }
    )
