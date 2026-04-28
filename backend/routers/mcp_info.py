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
from fastapi import APIRouter, Depends
from polymath_mcp.tools import ALL_TOOLS
from routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/mcp", tags=["mcp"])


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
    # Prefer explicit public URL if the deployer configured one; else fall
    # back to localhost+port (Docker Desktop default).
    public_url = (getattr(s, "MCP_PUBLIC_URL", "") or "").strip()
    if not public_url:
        public_url = f"http://localhost:{s.MCP_PORT}"

    return {
        "transport": s.MCP_TRANSPORT,
        "url": public_url,
        "port": s.MCP_PORT,
        "host": s.MCP_HOST,
        "require_auth": s.MCP_REQUIRE_AUTH,
        "has_api_key": bool(s.MCP_API_KEY),
        "default_top_k": s.MCP_DEFAULT_TOP_K,
        "tools": [
            {"name": fn.__name__, "description": (fn.__doc__ or "").split("\n")[0]}
            for fn in ALL_TOOLS
        ],
    }
