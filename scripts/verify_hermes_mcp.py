#!/usr/bin/env python3
"""Verify Hermes points at the same Polymath MCP endpoint the repo publishes.

This script is intentionally stdlib-only. It checks the real Hermes config,
confirms the Polymath MCP URL is exactly MCP_PUBLIC_URL + /mcp with no trailing
slash, and optionally runs the live streamable-HTTP MCP smoke test using the
same bearer token Hermes will send.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_HERMES_CONFIG = Path.home() / ".hermes" / "config.yaml"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "mcp_e2e_smoke.py"


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _normalize_mcp_endpoint(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    if not url:
        return ""
    return url if url.endswith("/mcp") else f"{url}/mcp"


def _find_indented_block(lines: list[str], key: str, min_indent: int = 0) -> tuple[int, int, int] | None:
    needle = f"{key}:"
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped != needle:
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent < min_indent:
            continue
        end = len(lines)
        for j in range(idx + 1, len(lines)):
            next_line = lines[j]
            if not next_line.strip() or next_line.lstrip().startswith("#"):
                continue
            next_indent = len(next_line) - len(next_line.lstrip(" "))
            if next_indent <= indent:
                end = j
                break
        return idx, end, indent
    return None


def _extract_scalar(block: list[str], key: str) -> str:
    prefix = f"{key}:"
    for line in block:
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip().strip('"').strip("'")
    return ""


def _extract_hermes_polymath(config_path: Path) -> dict[str, str]:
    if not config_path.exists():
        raise FileNotFoundError(f"Hermes config not found: {config_path}")
    lines = config_path.read_text(encoding="utf-8", errors="replace").splitlines()

    servers = _find_indented_block(lines, "mcp_servers")
    if servers is None:
        raise RuntimeError("Hermes config has no mcp_servers block")
    _, servers_end, servers_indent = servers
    server_lines = lines[servers[0] + 1:servers_end]

    polymath = _find_indented_block(server_lines, "polymath", servers_indent + 1)
    if polymath is None:
        raise RuntimeError("Hermes config has no mcp_servers.polymath block")
    poly_start, poly_end, _ = polymath
    poly_lines = server_lines[poly_start + 1:poly_end]

    headers = _find_indented_block(poly_lines, "headers")
    header_lines = poly_lines[headers[0] + 1:headers[1]] if headers else []
    auth = _extract_scalar(header_lines, "Authorization")
    token = ""
    if auth.startswith("Bearer "):
        token = auth[len("Bearer "):].strip()

    return {
        "url": _extract_scalar(poly_lines, "url"),
        "authorization": auth,
        "token": token,
    }


def _run_smoke(url: str, token: str) -> dict[str, Any]:
    if not SMOKE_SCRIPT.exists():
        raise FileNotFoundError(f"MCP smoke script not found: {SMOKE_SCRIPT}")
    spec = importlib.util.spec_from_file_location("polymath_mcp_e2e_smoke", SMOKE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load smoke script: {SMOKE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    client = module.McpSmokeClient(url, token)
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "polymath-hermes-mcp-verify", "version": "1.0"},
        },
    )
    client.notify("notifications/initialized")
    tools = client.request("tools/list")
    tool_names = {
        tool["name"]
        for tool in ((tools.get("result") or {}).get("tools") or [])
    }
    required = {
        "polymath_mcp_status",
        "polymath_plan_ingestion",
        "polymath_backfill_summaries",
    }
    missing = sorted(required - tool_names)
    if missing:
        raise RuntimeError(f"missing expected MCP tools: {missing}")

    status = client.call_tool("polymath_mcp_status", {"detail": "summary"})
    plan = client.call_tool(
        "polymath_plan_ingestion",
        {
            "filename": "shopify_video_transcript.txt",
            "source_url": "https://example.com/shopify_video_transcript.txt",
            "content_type": "text/plain",
            "summary_required": "auto",
        },
    )
    if plan.get("profile") != "transcript" or plan.get("summary_required") is not True:
        raise RuntimeError(f"unexpected ingestion plan: {plan}")

    return {
        "status": "ok",
        "server": (init.get("result") or {}).get("serverInfo"),
        "tool_count": len(tool_names),
        "mcp_status": status.get("status"),
        "connection_endpoint": (status.get("connection") or {}).get("endpoint"),
        "ingestion_plan": {
            "profile": plan.get("profile"),
            "summary_required": plan.get("summary_required"),
            "corpus_action": (plan.get("corpus_action") or {}).get("action"),
            "ingest_tool": plan.get("ingest_tool"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--hermes-config", type=Path, default=DEFAULT_HERMES_CONFIG)
    parser.add_argument("--expected-url", default="", help="Override MCP_PUBLIC_URL/MCP endpoint expectation.")
    parser.add_argument("--no-smoke", action="store_true", help="Only compare config; skip live MCP call.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    env = _read_env(args.env_file)
    expected_source = args.expected_url or env.get("MCP_PUBLIC_URL") or os.environ.get("MCP_PUBLIC_URL", "")
    expected = _normalize_mcp_endpoint(expected_source)
    hermes = _extract_hermes_polymath(args.hermes_config)
    actual = _normalize_mcp_endpoint(hermes["url"])

    errors: list[str] = []
    if not expected:
        errors.append(f"expected MCP endpoint is unknown; set MCP_PUBLIC_URL in {args.env_file}")
    if actual != hermes["url"].strip():
        errors.append("Hermes Polymath URL must not have trailing slashes or whitespace")
    if expected and actual != expected:
        errors.append(f"Hermes Polymath URL mismatch: expected {expected}, got {hermes['url']}")
    if not hermes["authorization"].startswith("Bearer "):
        errors.append("Hermes Polymath Authorization header must be 'Bearer <Polymath MCP key>'")
    if not hermes["token"]:
        errors.append("Hermes Polymath bearer token is missing")

    smoke: dict[str, Any] | None = None
    if not errors and not args.no_smoke:
        smoke = _run_smoke(actual, hermes["token"])
        reported_endpoint = _normalize_mcp_endpoint(smoke.get("connection_endpoint") or "")
        if reported_endpoint and reported_endpoint != expected:
            errors.append(
                f"MCP server reports endpoint {reported_endpoint}, expected {expected}"
            )

    result = {
        "status": "ok" if not errors else "failed",
        "expected_endpoint": expected,
        "hermes_endpoint": hermes["url"],
        "has_bearer_token": bool(hermes["token"]),
        "smoke": smoke,
        "errors": errors,
    }

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif errors:
        print("Hermes MCP verification failed:")
        for err in errors:
            print(f"- {err}")
    else:
        msg = f"Hermes MCP verified: {actual}"
        if smoke:
            msg += (
                f" ({smoke['tool_count']} tools, status={smoke['mcp_status']}, "
                f"plan={smoke['ingestion_plan']['profile']})"
            )
        print(msg)
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
