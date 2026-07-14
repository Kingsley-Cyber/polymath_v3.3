import json
from pathlib import Path

import httpx
import pytest

from models.structured_output_capabilities import (
    REGISTRY_PATH,
    StructuredOutputCapabilityRegistryError,
    load_structured_output_capabilities,
    normalize_api_base,
)
from scripts.probe_structured_output_capabilities import (
    DEFAULT_ROUTES,
    _error_fields,
    _load_routes,
    _safe_text,
    _validate_success,
)


def test_checked_in_capability_registry_records_all_runtime_rejections():
    registry = load_structured_output_capabilities()
    route = registry.resolve(
        model_id="deepseek/deepseek-v4-flash",
        api_base="https://api.deepseek.com/v1/",
    )

    assert registry.recipe_version == "structured-output-capability.runtime-probe.v1"
    assert len(registry.routes) == 5
    assert all(item.native_json_schema is False for item in registry.routes)
    assert all(
        item.verification_status == "provider_rejected" for item in registry.routes
    )
    assert route is not None
    assert route.native_json_schema is False
    assert route.verification_status == "provider_rejected"
    assert route.probe_receipt["http_status"] == 400
    assert route.probe_receipt["error_code"] == "400"
    assert (
        "This response_format type is unavailable now" in route.probe_receipt["message"]
    )
    assert route.probe_receipt["response_format_type"] == "json_schema"
    assert (
        route.tier4_prompt_requirement == "json_object_prompt_must_contain_literal_json"
    )
    assert route.verified_digest_path is None
    assert route.tier3_digest_status == "partial_acceptance_repair_budget_exhausted"
    assert route.tier4_digest_status == "structurally_unreliable"
    assert len(route.digest_evidence_receipts) == 4

    longcat = registry.resolve(
        model_id="openai/LongCat-2.0",
        api_base="https://api.longcat.chat/openai/v1",
    )
    assert longcat is not None
    assert longcat.probe_receipt["http_status"] == 200
    assert longcat.probe_receipt["error_code"] == "invalid_structured_output"
    assert longcat.tier4_prompt_requirement == "not_runtime_verified"
    assert longcat.verified_digest_path is None
    assert longcat.tier3_digest_status == "tiny_tool_accepted_digest_unverified"
    assert longcat.tier4_digest_status == "not_tested"
    assert len(longcat.digest_evidence_receipts) == 1


def test_capability_registry_does_not_match_model_without_exact_route():
    registry = load_structured_output_capabilities()

    assert (
        registry.resolve(
            model_id="deepseek/deepseek-v4-flash",
            api_base=None,
        )
        is None
    )
    assert (
        registry.resolve(
            model_id="deepseek/deepseek-v4-flash",
            api_base="https://another.invalid/v1",
        )
        is None
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://api.deepseek.com/v1/", "https://api.deepseek.com/v1"),
        ("http://localhost:8000/v1", "http://localhost:8000/v1"),
        (None, None),
    ],
)
def test_api_base_normalization(value, expected):
    assert normalize_api_base(value) == expected


@pytest.mark.parametrize("value", ["", "api.deepseek.com/v1", 42])
def test_api_base_normalization_rejects_ambiguous_values(value):
    with pytest.raises(StructuredOutputCapabilityRegistryError):
        normalize_api_base(value)


def test_registry_rejects_metadata_permission_drift(tmp_path: Path):
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    payload["policy"]["metadata_can_grant_tier1"] = True
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        StructuredOutputCapabilityRegistryError,
        match="metadata_can_grant_tier1",
    ):
        load_structured_output_capabilities(path)


def test_registry_rejects_duplicate_route_key(tmp_path: Path):
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    duplicate = dict(payload["routes"][0])
    duplicate["route_id"] = "duplicate"
    payload["routes"].append(duplicate)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StructuredOutputCapabilityRegistryError, match="route keys"):
        load_structured_output_capabilities(path)


def test_checked_in_probe_routes_are_unique_and_secret_free():
    routes = _load_routes(DEFAULT_ROUTES)

    assert len(routes) == 5
    assert len({route["route_id"] for route in routes}) == 5
    assert {route["credential_provider"] for route in routes} == {
        "deepseek",
        "longcat",
    }
    raw = DEFAULT_ROUTES.read_text(encoding="utf-8")
    assert "api_key" not in raw
    assert "ciphertext" not in raw


def test_probe_error_receipt_extracts_only_safe_provider_fields():
    response = httpx.Response(
        400,
        json={
            "error": {
                "code": "invalid_request_error",
                "message": "This response_format type is unavailable now",
                "private": "ignored",
            },
            "raw": "ignored",
        },
    )

    assert _error_fields(response, secret="never-print-me") == (
        "invalid_request_error",
        "This response_format type is unavailable now",
    )


def test_probe_success_requires_closed_schema_content():
    valid = httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps({"ok": True})}}]},
    )
    extra = httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": json.dumps({"ok": True, "extra": 1})}}]
        },
    )

    assert _validate_success(valid, secret="x")[0] is True
    assert _validate_success(extra, secret="x") == (
        False,
        "schema_not_enforced",
        "2xx response did not enforce the tiny closed schema",
    )


def test_probe_text_sanitizer_removes_explicit_secret_and_bearer_token():
    safe = _safe_text(
        "failure api_key=never-print-me Bearer token-value",
        secret="never-print-me",
    )

    assert "never-print-me" not in safe
    assert "token-value" not in safe
    assert safe.count("[REDACTED]") == 2
