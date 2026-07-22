#!/usr/bin/env bash
set -euo pipefail

container="${MCP_CONTAINER:-polymath-mcp}"
required_tools_csv="${MCP_REQUIRED_TOOLS:-search,fetch,polymath_search,polymath_chat_query,polymath_graph_query}"

docker exec -i "$container" env MCP_REQUIRED_TOOLS="$required_tools_csv" python - <<'PY'
import json
import os
import sys

import httpx

url = os.environ.get("MCP_URL", "http://127.0.0.1:8765/mcp")
token = os.environ.get("MCP_API_KEY")
required = [item.strip() for item in os.environ.get("MCP_REQUIRED_TOOLS", "").split(",") if item.strip()]

if not token:
    print(json.dumps({"ok": False, "error": "MCP_API_KEY not configured inside MCP container"}, sort_keys=True))
    sys.exit(2)

headers = {
    "Authorization": f"Bearer {token}",
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def decode_response(resp: httpx.Response) -> dict:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        payloads = []
        for line in resp.text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line.split(":", 1)[1].strip()
            if data:
                payloads.append(json.loads(data))
        return payloads[-1] if payloads else {"raw": resp.text[:500]}
    try:
        return resp.json()
    except Exception:
        return {"raw": resp.text[:500]}


with httpx.Client(timeout=15.0) as client:
    init = client.post(
        url,
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "polymath-mcp-live-smoke", "version": "1"},
            },
        },
    )
    init_body = decode_response(init)
    session_id = init.headers.get("mcp-session-id") or init.headers.get("Mcp-Session-Id")
    if init.status_code >= 400 or not session_id:
        print(json.dumps({"ok": False, "stage": "initialize", "status": init.status_code, "body": init_body}, sort_keys=True))
        sys.exit(1)

    session_headers = {**headers, "Mcp-Session-Id": session_id}
    client.post(
        url,
        headers=session_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    )
    listed = client.post(
        url,
        headers=session_headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    body = decode_response(listed)
    tools = ((body.get("result") or {}).get("tools") or []) if isinstance(body, dict) else []
    names = sorted(str(tool.get("name") or "") for tool in tools)
    missing = [name for name in required if name not in names]
    result = {
        "ok": listed.status_code < 400 and not missing,
        "status": listed.status_code,
        "tool_count": len(names),
        "required_tools": required,
        "missing_tools": missing,
        "first_tools": names[:8],
        "last_tools": names[-8:],
    }
    print(json.dumps(result, sort_keys=True))
    if missing or listed.status_code >= 400:
        sys.exit(1)
PY
