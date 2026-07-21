from __future__ import annotations

from pydantic import BaseModel, Field

from services.extraction_provider_cards import resolve_extraction_provider_card
from services.schema_control import (
    extract_provider_json_payload,
    provider_native_response_format,
    validate_pydantic_projection,
    xml_json_contract_prompt,
)


class _TinyContract(BaseModel):
    summary: str = Field(min_length=1)
    count: int = Field(ge=0)


def test_extract_provider_json_payload_unwraps_xml_and_prose() -> None:
    raw = (
        "Reasoning omitted.\n"
        '<json_payload>{"summary":"valid","count":1}</json_payload>\n'
        "Done."
    )

    assert extract_provider_json_payload(raw) == '{"summary":"valid","count":1}'


def test_extract_provider_json_payload_rejects_ambiguous_double_object() -> None:
    assert extract_provider_json_payload('{"a":1} then {"b":2}') is None


def test_validate_pydantic_projection_accepts_owned_fields_only() -> None:
    receipt = validate_pydantic_projection(
        {"summary": "valid", "count": 2, "caller_context": "kept outside"},
        _TinyContract,
    )

    assert receipt.valid is True
    assert receipt.normalized == {"summary": "valid", "count": 2}


def test_validate_pydantic_projection_reports_first_failure() -> None:
    receipt = validate_pydantic_projection(
        {"summary": "", "count": -1},
        _TinyContract,
    )

    assert receipt.valid is False
    assert receipt.model_name == "_TinyContract"
    assert receipt.error


def test_xml_json_contract_prompt_wraps_prompt_only_providers() -> None:
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "longcat",
            "model": "openai/LongCat-2.0",
            "base_url": "https://api.longcat.chat/openai/v1",
        }
    )

    prompt = xml_json_contract_prompt(
        card,
        contract_name="parent_summary.v1",
        prompt="Return the summary JSON.",
    )

    assert '<schema_control contract="parent_summary.v1"' in prompt
    assert "<json_payload>" in prompt
    assert "Return the summary JSON." in prompt


def test_xml_json_contract_prompt_leaves_native_schema_providers_alone() -> None:
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "openai",
            "model": "gpt-4.1-mini",
            "base_url": "https://api.openai.com/v1",
        }
    )

    assert (
        xml_json_contract_prompt(
            card,
            contract_name="parent_summary.v1",
            prompt="unchanged",
        )
        == "unchanged"
    )


def test_provider_native_response_format_uses_json_object_without_task_schema() -> None:
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "deepseek",
            "model": "deepseek/deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
        }
    )

    assert provider_native_response_format(card) == {"type": "json_object"}


def test_provider_native_response_format_skips_prompt_only_longcat() -> None:
    card = resolve_extraction_provider_card(
        {
            "provider_preset": "longcat",
            "model": "openai/LongCat-2.0",
            "base_url": "https://api.longcat.chat/openai/v1",
        }
    )

    assert provider_native_response_format(card) is None
