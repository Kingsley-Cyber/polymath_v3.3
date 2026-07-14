#!/usr/bin/env python3
"""Probe one configured route for a tiny forced Tier-3 tool contract."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
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
from scripts.probe_structured_output_capabilities import (  # noqa: E402
    DEFAULT_ROUTES,
    _error_fields,
    _load_routes,
)
from services.settings import settings_service  # noqa: E402


DEFAULT_ROUTE_ID = "longcat-api__longcat-2.0"
REPORT_SCHEMA_VERSION = "polymath.tier3_tool_capability_probe.v1"
TOOL_NAME = "submit_tiny_probe"
TINY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
}


class Tier3ProbeError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_tool_success(response: httpx.Response) -> tuple[bool, str, str]:
    try:
        payload = response.json()
        choices = payload.get("choices") if isinstance(payload, dict) else None
        message = (choices or [{}])[0].get("message") or {}
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or len(calls) != 1:
            raise ValueError("expected one tool call")
        function = calls[0].get("function") or {}
        if function.get("name") != TOOL_NAME:
            raise ValueError("wrong tool name")
        arguments = function.get("arguments")
        parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    except (AttributeError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return False, "invalid_tool_output", "2xx response lacked one forced tool call"
    if (
        not isinstance(parsed, dict)
        or set(parsed) != {"ok"}
        or not isinstance(parsed["ok"], bool)
    ):
        return (
            False,
            "tool_arguments_not_strict",
            "2xx forced-tool arguments did not satisfy the tiny closed schema",
        )
    return True, "", "forced tool accepted and tiny arguments enforced"


async def run(args: argparse.Namespace) -> dict[str, Any]:
    matches = [
        route
        for route in _load_routes(args.routes)
        if route["route_id"] == args.route_id
    ]
    if len(matches) != 1:
        raise Tier3ProbeError(
            f"expected one configured route {args.route_id!r}, found {len(matches)}"
        )
    route = matches[0]
    settings = get_settings()
    mongo = AsyncIOMotorClient(settings.MONGODB_URI)
    try:
        try:
            db = mongo.get_default_database()
        except Exception:
            db = mongo[settings.MONGODB_DATABASE]
        settings_service.attach(db)
        provider_key = await settings_service.get_plaintext_key_any_user(
            route["credential_provider"]
        )
        if not provider_key:
            raise Tier3ProbeError("encrypted provider credential is unavailable")
        body = {
            "model": route["model_id"],
            "messages": [
                {"role": "system", "content": "Call the required tool once."},
                {"role": "user", "content": "Set ok to true."},
            ],
            "stream": False,
            "api_base": route["api_base"],
            "api_key": provider_key,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": "Submit the tiny capability result.",
                        "strict": True,
                        "parameters": TINY_SCHEMA,
                    },
                }
            ],
            "tool_choice": {
                "type": "function",
                "function": {"name": TOOL_NAME},
            },
        }
        started = time.perf_counter()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(args.timeout_seconds, connect=20)
        ) as client:
            response = await client.post(
                settings.LITELLM_URL.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.LITELLM_MASTER_KEY}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        if response.status_code >= 400:
            error_code, message = _error_fields(response, secret=provider_key)
            accepted = False
        else:
            accepted, error_code, message = _validate_tool_success(response)
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": _now(),
            "route_id": route["route_id"],
            "model_id": route["model_id"],
            "api_base": route["api_base"],
            "tier3_forced_tool": accepted,
            "verification_status": "accepted" if accepted else "provider_rejected",
            "http_status": response.status_code,
            "error_code": error_code,
            "message": message,
            "elapsed_ms": elapsed_ms,
            "security": {
                "credential_from_encrypted_settings": True,
                "plaintext_credentials_in_receipt": False,
                "raw_provider_body_in_receipt": False,
            },
        }
    finally:
        mongo.close()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", type=Path, default=DEFAULT_ROUTES)
    parser.add_argument("--route-id", default=DEFAULT_ROUTE_ID)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--out", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = asyncio.run(run(args))
        _write_report(args.out, report)
    except Exception as exc:
        print(f"TIER3 PROBE FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        f"route={report['route_id']} status={report['verification_status']} "
        f"http_status={report['http_status']} error_code={report['error_code']} "
        f"message={report['message']} receipt={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
