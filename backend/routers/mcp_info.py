"""
Phase 24 — MCP info endpoint. Surfaces the Polymath MCP server's connection
details (URL, transport, registered tool names, auth requirement) for the
Settings → MCP tab to render copy-paste configs for Claude Desktop / Cursor /
custom MCP clients.

NEVER returns the actual MCP_API_KEY. The frontend gets a `has_api_key`
boolean only — the user already configured the secret in `.env`, the UI just
confirms it's present.
"""

import logging

from config import get_settings
from fastapi import APIRouter, Depends, HTTPException
from polymath_mcp.app_guide import get_app_guide
from polymath_mcp.tools import ALL_TOOLS
from routers.auth import get_current_user
from services.conversation import conversation_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["mcp"])


async def _user_mcp_keys(user_id: str) -> list[dict]:
    db = conversation_service._db
    if db is None:
        return []
    from polymath_mcp.key_store import list_mcp_keys

    return await list_mcp_keys(db, user_id=user_id)


@router.get("/info")
async def get_mcp_info(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Return MCP server config for client connection.

    The host-side URL Polymath emits is the docker-compose-published port
    (default 8765). For non-localhost setups (Cloudflare tunnel, LAN), the
    user overrides via MCP_PUBLIC_URL env or just edits the snippet manually.
    """
    s = get_settings()
    user_keys = await _user_mcp_keys(current_user["user_id"])
    # Prefer explicit public URL if the deployer configured one; else fall
    # back to localhost+port (Docker Desktop default).
    public_url = (getattr(s, "MCP_PUBLIC_URL", "") or "").strip()
    if not public_url:
        public_url = f"http://localhost:{s.MCP_PORT}"
    public_url = public_url.rstrip("/")
    mcp_endpoint = public_url if public_url.endswith("/mcp") else f"{public_url}/mcp"
    app_guide = get_app_guide(detail="summary")

    return {
        "transport": s.MCP_TRANSPORT,
        "url": public_url,
        "mcp_endpoint": mcp_endpoint,
        "port": s.MCP_PORT,
        "host": s.MCP_HOST,
        "require_auth": s.MCP_REQUIRE_AUTH,
        "auth_header_name": "Authorization",
        "auth_header_scheme": "Bearer",
        "has_api_key": bool(s.MCP_API_KEY) or bool(user_keys),
        "has_static_api_key": bool(s.MCP_API_KEY),
        "has_user_api_key": bool(user_keys),
        "user_api_key_count": len(user_keys),
        "supports_user_api_keys": True,
        "default_top_k": s.MCP_DEFAULT_TOP_K,
        "agent_guide": app_guide,
        "app_capabilities": app_guide["app_capabilities"],
        "agent_workflows": app_guide["agent_workflows"],
        "mcp_toolsets": app_guide["mcp_toolsets"],
        "remote_agent_setup": app_guide["remote_agent_setup"],
        "retrieval_routes": app_guide["retrieval_routes"],
        "graph_modes": app_guide["graph_modes"],
        "write_safety": app_guide["write_safety"],
        "tool_playbook": app_guide["tool_playbook"],
        "tools": [
            {"name": fn.__name__, "description": (fn.__doc__ or "").split("\n")[0]}
            for fn in ALL_TOOLS
        ],
    }


@router.get("/api-keys")
async def list_mcp_api_keys(
    current_user: dict = Depends(get_current_user),
) -> dict:
    """List the caller's user-scoped MCP API keys without secrets."""
    return {"keys": await _user_mcp_keys(current_user["user_id"])}


@router.post("/api-keys")
async def create_mcp_api_key(
    body: dict | None = None,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Generate a user-scoped MCP API key.

    The plaintext key is returned once. It is stored hashed in MongoDB and can
    be used immediately by the MCP sidecar; no `.env` edit or container restart
    is required.
    """
    db = conversation_service._db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    from polymath_mcp.key_store import create_mcp_key

    name = None
    scopes = None
    if isinstance(body, dict):
        name = body.get("name")
        scopes = body.get("scopes")
    key = await create_mcp_key(
        db,
        user_id=current_user["user_id"],
        name=name,
        scopes=scopes if isinstance(scopes, list) else None,
    )
    return {"key": key}


@router.delete("/api-keys/{key_id}")
async def revoke_mcp_api_key(
    key_id: str,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Revoke a user-scoped MCP API key."""
    db = conversation_service._db
    if db is None:
        raise HTTPException(status_code=503, detail="Database not connected")
    from polymath_mcp.key_store import revoke_mcp_key

    revoked = await revoke_mcp_key(db, user_id=current_user["user_id"], key_id=key_id)
    return {"key_id": key_id, "revoked": revoked}
