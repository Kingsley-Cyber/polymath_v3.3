"""Shared schema-control helpers for provider model calls.

Providers are treated as generators, not validators. Native schema controls are
still selected by the provider card where available, but prompt-only providers
such as LongCat must be wrapped, compiled, and validated locally before any
artifact reaches durable storage.
"""

from __future__ import annotations

import json
import re
import copy
from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel, ValidationError


_JSON_PAYLOAD_RE = re.compile(
    r"<json_payload\b[^>]*>(.*?)</json_payload>",
    flags=re.IGNORECASE | re.DOTALL,
)
_THINK_RE = re.compile(r"<think\b[^>]*>.*?</think>", flags=re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class SchemaControlValidation:
    """Secret-free local validation receipt for one provider artifact."""

    valid: bool
    model_name: str
    error: str | None = None
    normalized: dict[str, Any] | None = None


def _escape_cdata(text: str) -> str:
    return str(text or "").replace("]]>", "]]]]><![CDATA[>")


def xml_json_contract_prompt(
    card: Any,
    *,
    contract_name: str,
    prompt: str,
    schema_hint: str | None = None,
) -> str:
    """Wrap prompt-only JSON providers in an XML-delimited contract.

    This is guidance only. Enforcement remains the local compiler plus
    Pydantic validation. Native-schema providers keep their original prompt so
    we do not perturb already-token-masked lanes.
    """

    if getattr(card, "schema_mode", None) != "json_object_prompt":
        return prompt
    hint = f"\n<schema_hint><![CDATA[{_escape_cdata(schema_hint)}]]></schema_hint>" if schema_hint else ""
    return (
        f'<schema_control contract="{contract_name}" mode="prompt_json">\n'
        "<rules>\n"
        "Return exactly one JSON object inside exactly one <json_payload> element.\n"
        "Do not output markdown fences, prose, hidden reasoning, or a second JSON object.\n"
        "Use only the supplied source text and evidence identifiers.\n"
        "</rules>"
        f"{hint}\n"
        f"<task><![CDATA[{_escape_cdata(prompt)}]]></task>\n"
        "</schema_control>"
    )


def provider_native_response_format(
    card: Any,
    *,
    model_cls: type[BaseModel] | None = None,
    schema_name: str = "structured_artifact",
) -> dict[str, Any] | None:
    """Return the provider-native response_format allowed by a card.

    If a task has a provider-facing Pydantic model, json_schema mode can use it
    directly. If not, capable native lanes still get the weaker but valuable
    JSON-object guarantee. Prompt-only/compiler-gated providers get ``None`` so
    callers do not send unsupported OpenAI response_format payloads to LongCat,
    Hy3, Mimo, or similar routes.
    """

    schema_mode = getattr(card, "schema_mode", None)
    if schema_mode == "json_schema" and model_cls is not None:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "schema": _pin_all_required(model_cls.model_json_schema()),
                "strict": True,
            },
        }
    if schema_mode in {"json_schema", "json_object"} and getattr(
        card, "supports_json_object", False
    ):
        return {"type": "json_object"}
    return None


def _pin_all_required(schema: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Pydantic JSON Schema for strict provider decoders."""

    def _walk(node: Any) -> Any:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties.keys())
                node.setdefault("additionalProperties", False)
                for child in properties.values():
                    _walk(child)
            for key in ("$defs", "definitions"):
                children = node.get(key)
                if isinstance(children, dict):
                    for child in children.values():
                        _walk(child)
            for key in ("items", "anyOf", "oneOf", "allOf"):
                child = node.get(key)
                if isinstance(child, list):
                    for item in child:
                        _walk(item)
                elif isinstance(child, dict):
                    _walk(child)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        return node

    return _walk(copy.deepcopy(schema))


def extract_provider_json_payload(raw: str) -> str | None:
    """Extract one complete JSON object/array from prose, XML, or fences.

    The compiler is intentionally conservative: if it sees another object after
    the first balanced payload, it returns ``None`` instead of silently choosing
    one. Callers may then trigger their existing rescue/quarantine path.
    """

    text = _THINK_RE.sub("", str(raw or "")).lstrip("\ufeff").strip()
    payload_match = _JSON_PAYLOAD_RE.search(text)
    if payload_match:
        text = payload_match.group(1).strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text).strip()

    starts = [(text.find("{"), "{", "}"), (text.find("["), "[", "]")]
    starts = [item for item in starts if item[0] >= 0]
    if not starts:
        return None
    start, opener, closer = min(starts, key=lambda item: item[0])

    depth = 0
    in_string = False
    escape = False
    end: int | None = None
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                end = index + 1
                break
            if depth < 0:
                return None
    if end is None:
        return None

    candidate = text[start:end]
    remainder = text[end:].strip()
    remainder = re.sub(r"^(?:```|</json_payload>)\s*", "", remainder, flags=re.I)
    if "{" in remainder or "[" in remainder:
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return candidate if isinstance(parsed, (dict, list)) else None


def validate_pydantic_projection(
    artifact: Mapping[str, Any],
    model_cls: type[BaseModel],
) -> SchemaControlValidation:
    """Validate only fields owned by ``model_cls`` from a larger artifact.

    Summary artifacts carry identity fields such as parent_id/doc_id that do
    not belong to the durable Pydantic record. This keeps local acceptance
    aligned with the Mongo writer boundary without rejecting caller context.
    """

    payload = {
        name: artifact[name]
        for name in model_cls.model_fields
        if name in artifact
    }
    try:
        model = model_cls.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(part) for part in first.get("loc", ())) or "root"
        msg = str(first.get("msg") or type(exc).__name__)
        return SchemaControlValidation(
            valid=False,
            model_name=model_cls.__name__,
            error=f"{loc}:{msg}",
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a validation receipt
        return SchemaControlValidation(
            valid=False,
            model_name=model_cls.__name__,
            error=f"{type(exc).__name__}:{str(exc)[:160]}",
        )
    return SchemaControlValidation(
        valid=True,
        model_name=model_cls.__name__,
        normalized=model.model_dump(mode="python", exclude_none=True),
    )
