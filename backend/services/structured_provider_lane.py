"""Shared structured-provider lane helpers.

The provider lane decides how to ask. Schema control decides what is accepted.
This module keeps those two concerns connected without making DeepSeek,
LongCat, Hy3, private vLLM, or future providers own the artifact contract.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel

from services.schema_control import (
    provider_native_response_format,
    xml_json_contract_prompt,
)


@dataclass(frozen=True)
class StructuredProviderAttempt:
    """Secret-free receipt for one structured provider attempt."""

    contract_name: str
    provider: str
    model: str
    schema_mode: str
    output_mode: str
    json_repair_mode: str
    prompt_wrapped: bool
    response_format_type: str | None

    def to_safe_dict(self) -> dict[str, Any]:
        return asdict(self)


def prepare_structured_provider_attempt(
    card: Any,
    *,
    output_mode: str,
    contract_name: str,
    prompt: str,
    schema_hint: str | None = None,
    model_cls: type[BaseModel] | None = None,
    schema_name: str = "structured_artifact",
) -> tuple[str, dict[str, Any] | None, StructuredProviderAttempt]:
    """Return the prompt, native response_format, and safe attempt receipt.

    ``output_mode`` is the actual route selected for this attempt. This matters
    when a native lane has degraded to ``json_object_prompt`` after a provider
    rejects response_format: the card still describes native capability, but
    this specific attempt must be compiler-gated.
    """

    output_mode = str(output_mode or "").strip()
    force_prompt_wrapper = output_mode == "json_object_prompt"
    controlled_prompt = xml_json_contract_prompt(
        card,
        contract_name=contract_name,
        prompt=prompt,
        schema_hint=schema_hint,
        force=force_prompt_wrapper,
    )
    response_format: dict[str, Any] | None = None
    if output_mode == "json_schema" and bool(getattr(card, "supports_json_schema", False)):
        response_format = provider_native_response_format(
            card,
            model_cls=model_cls,
            schema_name=schema_name,
        )
    elif output_mode == "json_object" and bool(getattr(card, "supports_json_object", False)):
        response_format = provider_native_response_format(card)

    response_format_type = (
        str(response_format.get("type"))
        if isinstance(response_format, dict) and response_format.get("type")
        else None
    )
    attempt = StructuredProviderAttempt(
        contract_name=contract_name,
        provider=str(getattr(card, "provider", "") or ""),
        model=str(getattr(card, "model", "") or ""),
        schema_mode=str(getattr(card, "schema_mode", "") or ""),
        output_mode=output_mode,
        json_repair_mode=str(getattr(card, "json_repair_mode", "") or ""),
        prompt_wrapped=controlled_prompt != prompt,
        response_format_type=response_format_type,
    )
    return controlled_prompt, response_format, attempt
