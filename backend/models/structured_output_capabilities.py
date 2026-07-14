"""Runtime-verified native JSON-Schema capability registry.

Provider/library metadata is advisory only. A route earns Tier 1 exclusively
through a recorded live probe in the versioned registry; an unverified route
must fail closed to Tier 4.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "registries"
    / "structured_output_capabilities.v1.json"
)
SCHEMA_VERSION = "polymath.structured_output_capabilities.v1"
_ROOT_FIELDS = frozenset(
    {"schema_version", "recipe_version", "generated_at", "policy", "routes"}
)
_POLICY_FIELDS = frozenset({"metadata_can_grant_tier1", "unverified_default"})
_ROUTE_FIELDS = frozenset(
    {
        "route_id",
        "model_id",
        "api_base",
        "native_json_schema",
        "verification_status",
        "verified_at",
        "verified_digest_path",
        "tier3_digest_status",
        "tier4_digest_status",
        "digest_evidence_receipts",
        "tier4_prompt_requirement",
        "probe_receipt",
    }
)
_RECEIPT_FIELDS = frozenset(
    {
        "http_status",
        "outcome",
        "error_code",
        "message",
        "response_format_type",
    }
)


class StructuredOutputCapabilityRegistryError(ValueError):
    """The runtime capability registry is malformed or ambiguous."""


def normalize_api_base(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StructuredOutputCapabilityRegistryError(
            "api_base must be null or a non-empty string"
        )
    normalized = value.strip().rstrip("/")
    if not normalized.startswith(("https://", "http://")):
        raise StructuredOutputCapabilityRegistryError(
            "api_base must be an absolute http(s) URL"
        )
    return normalized


@dataclass(frozen=True)
class RuntimeCapabilityRoute:
    route_id: str
    model_id: str
    api_base: str | None
    native_json_schema: bool
    verification_status: str
    verified_at: str
    verified_digest_path: str | None
    tier3_digest_status: str
    tier4_digest_status: str
    digest_evidence_receipts: tuple[str, ...]
    tier4_prompt_requirement: str
    probe_receipt: dict[str, Any]


@dataclass(frozen=True)
class StructuredOutputCapabilityRegistry:
    schema_version: str
    recipe_version: str
    generated_at: str
    routes: tuple[RuntimeCapabilityRoute, ...]

    def resolve(
        self,
        *,
        model_id: str,
        api_base: str | None,
    ) -> RuntimeCapabilityRoute | None:
        normalized_base = normalize_api_base(api_base)
        for route in self.routes:
            if route.model_id == model_id and route.api_base == normalized_base:
                return route
        return None


def _required_string(row: dict[str, Any], field: str, *, location: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.{field} must be a non-empty string"
        )
    return value.strip()


def _parse_route(row: Any, index: int) -> RuntimeCapabilityRoute:
    location = f"routes[{index}]"
    if not isinstance(row, dict) or set(row) != _ROUTE_FIELDS:
        raise StructuredOutputCapabilityRegistryError(
            f"{location} fields must equal {sorted(_ROUTE_FIELDS)}"
        )
    native = row.get("native_json_schema")
    if not isinstance(native, bool):
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.native_json_schema must be bool"
        )
    status = _required_string(row, "verification_status", location=location)
    expected_status = "accepted" if native else "provider_rejected"
    if status != expected_status:
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.verification_status must be {expected_status!r}"
        )
    receipt = row.get("probe_receipt")
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.probe_receipt fields must equal {sorted(_RECEIPT_FIELDS)}"
        )
    http_status = receipt.get("http_status")
    if not isinstance(http_status, int) or isinstance(http_status, bool):
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.probe_receipt.http_status must be int"
        )
    if receipt.get("response_format_type") != "json_schema":
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.probe_receipt.response_format_type must be 'json_schema'"
        )
    if receipt.get("outcome") != status:
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.probe_receipt.outcome must match verification_status"
        )
    for field in ("error_code", "message"):
        if not isinstance(receipt.get(field), str):
            raise StructuredOutputCapabilityRegistryError(
                f"{location}.probe_receipt.{field} must be string"
            )
    verified_digest_path = row.get("verified_digest_path")
    if verified_digest_path is not None and verified_digest_path not in {
        "tier1",
        "tier2",
        "tier3",
        "tier4",
    }:
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.verified_digest_path must be null or a tier"
        )
    evidence = row.get("digest_evidence_receipts")
    if not isinstance(evidence, list) or any(
        not isinstance(value, str) or not value.strip() for value in evidence
    ):
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.digest_evidence_receipts must be a string list"
        )
    if len(evidence) != len(set(evidence)):
        raise StructuredOutputCapabilityRegistryError(
            f"{location}.digest_evidence_receipts must be unique"
        )
    return RuntimeCapabilityRoute(
        route_id=_required_string(row, "route_id", location=location),
        model_id=_required_string(row, "model_id", location=location),
        api_base=normalize_api_base(row.get("api_base")),
        native_json_schema=native,
        verification_status=status,
        verified_at=_required_string(row, "verified_at", location=location),
        verified_digest_path=verified_digest_path,
        tier3_digest_status=_required_string(
            row, "tier3_digest_status", location=location
        ),
        tier4_digest_status=_required_string(
            row, "tier4_digest_status", location=location
        ),
        digest_evidence_receipts=tuple(evidence),
        tier4_prompt_requirement=_required_string(
            row, "tier4_prompt_requirement", location=location
        ),
        probe_receipt=dict(receipt),
    )


def load_structured_output_capabilities(
    path: Path | None = None,
) -> StructuredOutputCapabilityRegistry:
    registry_path = path or REGISTRY_PATH
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StructuredOutputCapabilityRegistryError(
            f"cannot load capability registry {registry_path.name}: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS:
        raise StructuredOutputCapabilityRegistryError(
            f"root fields must equal {sorted(_ROOT_FIELDS)}"
        )
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise StructuredOutputCapabilityRegistryError(
            f"schema_version must be {SCHEMA_VERSION!r}"
        )
    recipe_version = _required_string(payload, "recipe_version", location="root")
    generated_at = _required_string(payload, "generated_at", location="root")
    policy = payload.get("policy")
    if not isinstance(policy, dict) or set(policy) != _POLICY_FIELDS:
        raise StructuredOutputCapabilityRegistryError(
            f"policy fields must equal {sorted(_POLICY_FIELDS)}"
        )
    if policy.get("metadata_can_grant_tier1") is not False:
        raise StructuredOutputCapabilityRegistryError(
            "policy.metadata_can_grant_tier1 must be false"
        )
    if policy.get("unverified_default") != "tier4":
        raise StructuredOutputCapabilityRegistryError(
            "policy.unverified_default must be 'tier4'"
        )
    raw_routes = payload.get("routes")
    if not isinstance(raw_routes, list):
        raise StructuredOutputCapabilityRegistryError("routes must be a list")
    routes = tuple(_parse_route(row, index) for index, row in enumerate(raw_routes))
    route_ids = [route.route_id for route in routes]
    route_keys = [(route.model_id, route.api_base) for route in routes]
    if len(route_ids) != len(set(route_ids)):
        raise StructuredOutputCapabilityRegistryError("route_id values must be unique")
    if len(route_keys) != len(set(route_keys)):
        raise StructuredOutputCapabilityRegistryError(
            "(model_id, api_base) route keys must be unique"
        )
    return StructuredOutputCapabilityRegistry(
        schema_version=SCHEMA_VERSION,
        recipe_version=recipe_version,
        generated_at=generated_at,
        routes=routes,
    )
