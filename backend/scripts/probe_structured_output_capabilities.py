#!/usr/bin/env python3
"""Probe owner-configured routes for real native JSON-Schema support.

Each route receives one tiny strict-schema request through the deployed
LiteLLM proxy. Credentials are resolved from encrypted settings and are never
printed or written. The output is a redacted route receipt suitable for the
versioned structured-output capability registry.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorClient


HERE = Path(__file__).resolve()
BACKEND_ROOT = HERE.parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from config import get_settings  # noqa: E402
from models.structured_output_capabilities import normalize_api_base  # noqa: E402
from services.settings import settings_service  # noqa: E402


DEFAULT_ROUTES = BACKEND_ROOT / "registries" / "structured_output_probe_routes.v1.json"
ROUTE_SCHEMA_VERSION = "polymath.structured_output_probe_routes.v1"
REPORT_SCHEMA_VERSION = "polymath.structured_output_capability_probe.v1"
_ROUTE_FIELDS = frozenset({"route_id", "credential_provider", "model_id", "api_base"})
_SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+|api[_-]?key[\s\"':=]+)([A-Za-z0-9._-]+)"
)

TINY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


class CapabilityProbeError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, *, secret: str | None = None) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    if secret:
        text = text.replace(secret, "[REDACTED]")
    text = _SECRET_PATTERN.sub(
        lambda match: match.group(0).replace(match.group(1), "[REDACTED]"), text
    )
    return text[:500]


def _load_routes(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapabilityProbeError(
            f"cannot load probe routes: {type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "routes"}:
        raise CapabilityProbeError("probe route root fields are invalid")
    if payload.get("schema_version") != ROUTE_SCHEMA_VERSION:
        raise CapabilityProbeError("probe route schema_version is invalid")
    raw_routes = payload.get("routes")
    if not isinstance(raw_routes, list) or not raw_routes:
        raise CapabilityProbeError("probe routes must be a non-empty list")
    routes: list[dict[str, str]] = []
    for index, row in enumerate(raw_routes):
        if not isinstance(row, dict) or set(row) != _ROUTE_FIELDS:
            raise CapabilityProbeError(f"routes[{index}] fields are invalid")
        clean: dict[str, str] = {}
        for field in ("route_id", "credential_provider", "model_id"):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise CapabilityProbeError(f"routes[{index}].{field} is invalid")
            clean[field] = value.strip()
        clean["api_base"] = str(normalize_api_base(row.get("api_base")))
        routes.append(clean)
    ids = [row["route_id"] for row in routes]
    keys = [(row["model_id"], row["api_base"]) for row in routes]
    if len(ids) != len(set(ids)) or len(keys) != len(set(keys)):
        raise CapabilityProbeError("probe route IDs and keys must be unique")
    return routes


def _error_fields(response: httpx.Response, *, secret: str) -> tuple[str, str]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        return "http_error", _safe_text(response.text, secret=secret)
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or "provider_error"
        message = error.get("message") or error
    else:
        code = "provider_error"
        message = payload
    return _safe_text(code, secret=secret), _safe_text(message, secret=secret)


def _validate_success(
    response: httpx.Response, *, secret: str
) -> tuple[bool, str, str]:
    try:
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        message = (choices or [{}])[0].get("message") or {}
        content = message.get("content") or ""
        parsed = json.loads(content)
    except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return (
            False,
            "invalid_structured_output",
            "2xx response did not contain valid JSON content",
        )
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"ok"}
        or not isinstance(parsed["ok"], bool)
    ):
        return (
            False,
            "schema_not_enforced",
            "2xx response did not enforce the tiny closed schema",
        )
    return True, "", "native json_schema accepted and tiny closed schema enforced"


def _response_telemetry(response: httpx.Response) -> dict[str, Any]:
    """Extract only numeric usage and LiteLLM's routed response cost."""

    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = {}
    raw_usage = payload.get("usage") if isinstance(payload, dict) else None
    usage: dict[str, int] = {}
    if isinstance(raw_usage, dict):
        for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = raw_usage.get(field)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                usage[field] = value

    raw_cost = response.headers.get("x-litellm-response-cost")
    cost: float | None = None
    if raw_cost not in (None, "", "None"):
        try:
            candidate = float(raw_cost)
        except (TypeError, ValueError):
            candidate = float("nan")
        if math.isfinite(candidate) and candidate >= 0:
            cost = candidate
    if cost is not None:
        source = "litellm.x-litellm-response-cost"
    elif response.status_code >= 400 and not usage:
        cost = 0.0
        source = "provider_rejected_before_generation"
    else:
        source = None
    return {
        "usage": usage,
        "actual_cost_usd": cost,
        "cost_source": source,
    }


async def _probe_route(
    client: httpx.AsyncClient,
    *,
    proxy_url: str,
    proxy_key: str,
    route: dict[str, str],
    provider_key: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": route["model_id"],
        "messages": [
            {"role": "system", "content": "Return one tiny schema object."},
            {"role": "user", "content": "Set ok to true."},
        ],
        "temperature": 0,
        "max_tokens": 32,
        "stream": False,
        "api_base": route["api_base"],
        "api_key": provider_key,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "capability_probe_v1",
                "strict": True,
                "schema": TINY_SCHEMA,
            },
        },
    }
    if "deepseek-v4-" in route["model_id"]:
        body["thinking"] = {"type": "disabled"}
    started = time.perf_counter()
    try:
        response = await client.post(
            proxy_url.rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {proxy_key}",
                "Content-Type": "application/json",
            },
            json=body,
        )
    except Exception as exc:
        raise CapabilityProbeError(
            f"route {route['route_id']} transport failed: {type(exc).__name__}"
        ) from exc
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    if response.status_code >= 400:
        error_code, message = _error_fields(response, secret=provider_key)
        accepted = False
    else:
        accepted, error_code, message = _validate_success(
            response,
            secret=provider_key,
        )
    outcome = "accepted" if accepted else "provider_rejected"
    telemetry = _response_telemetry(response)
    return {
        "route_id": route["route_id"],
        "credential_provider": route["credential_provider"],
        "model_id": route["model_id"],
        "api_base": route["api_base"],
        "native_json_schema": accepted,
        "verification_status": outcome,
        "verified_at": _now(),
        "probe_receipt": {
            "http_status": response.status_code,
            "outcome": outcome,
            "error_code": error_code,
            "message": message,
            "response_format_type": "json_schema",
        },
        "elapsed_ms": elapsed_ms,
        "provider_telemetry": telemetry,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    routes = _load_routes(args.routes)
    if args.route_id:
        routes = [route for route in routes if route["route_id"] == args.route_id]
        if len(routes) != 1:
            raise CapabilityProbeError(
                f"route selector {args.route_id!r} did not resolve exactly once"
            )
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = mongo.get_default_database()
        except Exception:
            db = mongo[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        key_cache: dict[str, str] = {}
        for provider in sorted({route["credential_provider"] for route in routes}):
            key = await settings_service.get_plaintext_key_any_user(provider)
            if not key:
                raise CapabilityProbeError(
                    f"encrypted credential is missing for provider {provider}"
                )
            key_cache[provider] = key
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(args.timeout_seconds, connect=20)
        ) as client:
            receipts = []
            for route in routes:
                receipt = await _probe_route(
                    client,
                    proxy_url=settings.LITELLM_URL,
                    proxy_key=settings.LITELLM_MASTER_KEY,
                    route=route,
                    provider_key=key_cache[route["credential_provider"]],
                )
                receipts.append(receipt)
                cost = receipt["provider_telemetry"]["actual_cost_usd"]
                if cost is None:
                    raise CapabilityProbeError(
                        f"route {route['route_id']} returned no numeric cost telemetry"
                    )
                if (
                    sum(
                        row["provider_telemetry"]["actual_cost_usd"] for row in receipts
                    )
                    > args.max_provider_cost_usd
                ):
                    raise CapabilityProbeError("provider cost ceiling exceeded")
        actual_cost = sum(
            row["provider_telemetry"]["actual_cost_usd"] for row in receipts
        )
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": _now(),
            "route_count": len(receipts),
            "accepted_count": sum(bool(row["native_json_schema"]) for row in receipts),
            "rejected_count": sum(
                not bool(row["native_json_schema"]) for row in receipts
            ),
            "routes": receipts,
            "cost_accounting": {
                "actual_cost_usd": actual_cost,
                "max_provider_cost_usd": args.max_provider_cost_usd,
                "cost_complete": True,
                "within_ceiling": actual_cost <= args.max_provider_cost_usd,
            },
            "security": {
                "credentials_from_encrypted_settings": True,
                "plaintext_credentials_in_receipt": False,
                "raw_provider_bodies_in_receipt": False,
            },
        }
    finally:
        mongo.close()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", type=Path, default=DEFAULT_ROUTES)
    parser.add_argument("--route-id")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--max-provider-cost-usd", type=float, default=2.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(run(args))
        _write_report(args.out, report)
    except Exception as exc:
        print(f"CAPABILITY PROBE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for route in report["routes"]:
        print(
            f"{route['route_id']} outcome={route['verification_status']} "
            f"http_status={route['probe_receipt']['http_status']} "
            f"error_code={route['probe_receipt']['error_code']} "
            f"message={route['probe_receipt']['message']}"
        )
    print(
        f"accepted={report['accepted_count']} rejected={report['rejected_count']} "
        f"receipt={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
