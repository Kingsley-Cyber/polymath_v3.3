#!/usr/bin/env python3
"""Smoke-test the live Polymath MCP streamable-HTTP endpoint.

This intentionally uses only the standard library so it can run from a fresh
clone after `source .env`. It verifies the same path a remote agent uses:

1. initialize MCP session
2. send initialized notification
3. list tools
4. call polymath_mcp_status
5. call polymath_plan_ingestion

Environment:
  MCP_SMOKE_URL   Optional. Defaults to MCP_PUBLIC_URL or localhost:8765/mcp.
  MCP_API_KEY     Required unless MCP_SMOKE_TOKEN is set.
  MCP_SMOKE_TOKEN Optional bearer token override.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def _default_url() -> str:
    raw = (os.environ.get("MCP_SMOKE_URL") or os.environ.get("MCP_PUBLIC_URL") or "").strip()
    if not raw:
        raw = "http://localhost:8765"
    raw = raw.rstrip("/")
    return raw if raw.endswith("/mcp") else f"{raw}/mcp"


def _parse_sse_json(raw: str) -> dict[str, Any]:
    for line in raw.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    raise RuntimeError(f"no SSE data line in response: {raw[:300]!r}")


def _tool_json(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("result") or {}
    content = payload.get("content") or []
    if not content:
        structured = payload.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        raise RuntimeError(f"tool response had no content: {payload}")
    text = content[0].get("text") if isinstance(content[0], dict) else None
    if not text:
        raise RuntimeError(f"tool response had no text content: {payload}")
    return json.loads(text)


class McpSmokeClient:
    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None
        self.next_id = 1

    def post(self, body: dict[str, Any], *, expect_response: bool = True) -> dict[str, Any] | None:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Polymath-MCP-Smoke/1.0 (+https://github.com)",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                if resp.headers.get("mcp-session-id"):
                    self.session_id = resp.headers["mcp-session-id"]
                raw = resp.read().decode("utf-8")
                if not expect_response:
                    return _parse_sse_json(raw) if raw.strip() else None
                return _parse_sse_json(raw)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"MCP HTTP {exc.code}: {detail[:500]}") from exc

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        msg_id = self.next_id
        self.next_id += 1
        return self.post(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "method": method,
                "params": params or {},
            }
        )

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self.post(
            {"jsonrpc": "2.0", "method": method, "params": params or {}},
            expect_response=False,
        )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return _tool_json(
            self.request(
                "tools/call",
                {"name": name, "arguments": arguments},
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=_default_url())
    parser.add_argument("--token", default=os.environ.get("MCP_SMOKE_TOKEN") or os.environ.get("MCP_API_KEY"))
    parser.add_argument("--json", action="store_true", help="Print machine-readable result JSON.")
    args = parser.parse_args()

    if not args.token:
        print("MCP smoke failed: set MCP_API_KEY or MCP_SMOKE_TOKEN", file=sys.stderr)
        return 2

    client = McpSmokeClient(args.url, args.token)
    init = client.request(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "polymath-mcp-smoke", "version": "1.0"},
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
        "polymath_check_source",
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
        raise RuntimeError(f"ingestion planner returned unexpected plan: {plan}")

    result = {
        "status": "ok",
        "url": args.url,
        "session_id_present": bool(client.session_id),
        "server": (init.get("result") or {}).get("serverInfo"),
        "tool_count": len(tool_names),
        "mcp_status": status.get("status"),
        "ingestion_plan": {
            "profile": plan.get("profile"),
            "summary_required": plan.get("summary_required"),
            "corpus_action": (plan.get("corpus_action") or {}).get("action"),
            "ingest_tool": plan.get("ingest_tool"),
        },
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(
            "MCP smoke ok: "
            f"{result['tool_count']} tools, status={result['mcp_status']}, "
            f"plan={result['ingestion_plan']['profile']} via {result['ingestion_plan']['ingest_tool']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
