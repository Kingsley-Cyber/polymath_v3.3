from __future__ import annotations

from services.extraction_provider_cards import (
    extraction_lane_uses_private_vllm,
    provider_payload_defaults,
    resolve_extraction_provider_card,
    resolve_extraction_routing_policy,
    safe_extraction_pool_contract,
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
    assert card.context_window_tokens == 8192
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
    assert "pydantic_extraction_response" in card.promotion_gate
    assert "required_evidence_phrase" in card.promotion_gate
    assert "allowed_predicate" in card.promotion_gate
    assert provider_payload_defaults(card) == {"thinking": {"type": "disabled"}}
    assert card.context_window_tokens == 1_000_000


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
    assert "pydantic_extraction_response" in card.promotion_gate
    assert "required_evidence_phrase" in card.promotion_gate
    assert "allowed_predicate" in card.promotion_gate


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


def test_local_private_vllm_card_keeps_provider_native_schema_gate():
    card = resolve_extraction_provider_card(
        {
            "provider": "openai",
            "model": "polymath-extract",
            "base_url": "http://10.0.0.12:8000/v1",
            "extra_params": {"supports_json_schema": True},
        }
    )

    assert card.provider == "local_private_vllm"
    assert card.schema_mode == "json_schema"
    assert card.json_repair_mode == "provider_native"
    assert card.concurrency_policy == "adaptive_vram_85"
    assert "pydantic_extraction_response" in card.promotion_gate
    assert "required_evidence_phrase" in card.promotion_gate
    assert "allowed_predicate" in card.promotion_gate


def test_private_vllm_detector_uses_provider_card():
    assert extraction_lane_uses_private_vllm(
        {
            "model": "openai/polymath-extract",
            "base_url": "http://192.168.1.83:8000/v1",
        }
    )


def test_mixed_private_and_cloud_pool_defaults_balanced_with_safe_contract():
    pool = [
        {
            "provider_preset": "vllm-rtx",
            "model": "polymath-extract",
            "base_url": "http://192.168.1.83:8000/v1",
            "max_concurrent": 60,
            "api_key": "never-emit",
        },
        {
            "provider_preset": "siliconflow",
            "model": "tencent/Hy3",
            "base_url": "https://api.siliconflow.com/v1",
            "max_concurrent": 8,
        },
    ]

    contract = safe_extraction_pool_contract(
        pool_source="extraction_models",
        pool=pool,
    )

    assert resolve_extraction_routing_policy(pool) == "balanced"
    assert contract["routing_policy"] == "balanced"
    assert contract["lanes"][0]["schema_mode"] == "json_schema"
    assert contract["lanes"][1]["schema_mode"] == "json_object_prompt"
    assert contract["lane_capacities"][0]["concurrency_policy"] == "adaptive_vram_85"
    assert contract["lane_capacities"][1]["concurrency_policy"] == "static_lane_cap"
    assert "never-emit" not in str(contract)
