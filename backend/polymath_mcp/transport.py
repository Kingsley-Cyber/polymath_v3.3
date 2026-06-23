"""MCP transport selector + Starlette auth middleware.

Streamable-HTTP path:
    transport.py exposes `build_streamable_app(mcp)` which wraps the FastMCP
    server's streamable_http_app() with a custom Starlette middleware that
    extracts the Bearer JWT, validates it, and stashes user_id into the
    contextvar consumed by tool functions (mcp.auth).

Stdio path:
    Used only for local Claude Desktop integration via a host-side proxy.
    No auth middleware — stdio is per-process and trusted.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from config import get_settings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .auth import (
    extract_bearer_token,
    set_current_user_id,
    validate_token_async,
)

logger = logging.getLogger(__name__)


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """Extract Authorization header, validate JWT, stash user_id into contextvar.

    Per Phase 8.4: missing/invalid JWT returns MCP-shaped 401 BEFORE the tool
    dispatcher runs. Health endpoints bypass auth.
    """

    PUBLIC_PATHS: tuple[str, ...] = ("/health", "/healthz", "/")

    async def dispatch(self, request: Request, call_next) -> Response:
        settings = get_settings()

        # Bypass auth on health probes — used by docker-compose healthcheck.
        if request.url.path in self.PUBLIC_PATHS:
            return await call_next(request)

        token = extract_bearer_token(request.headers.get("authorization"))
        user_id = await validate_token_async(token)

        if user_id is None and settings.MCP_REQUIRE_AUTH:
            return JSONResponse(
                status_code=401,
                content={
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32001,
                        "message": "auth.unauthorized",
                        "data": "Missing or invalid Bearer token.",
                    },
                    "id": None,
                },
                headers={"WWW-Authenticate": 'Bearer realm="polymath-mcp"'},
            )

        set_current_user_id(user_id)
        return await call_next(request)


async def _health(request: Request) -> Response:
    """Minimal liveness probe. Compose healthcheck hits this."""
    return JSONResponse({"status": "ok", "service": "polymath-mcp"})


def build_streamable_app(mcp_server) -> Starlette:
    """Wrap a FastMCP server's streamable_http_app() with auth middleware.

    Args:
        mcp_server: A FastMCP instance (mcp.server.fastmcp.FastMCP).

    Returns:
        A Starlette app ready for uvicorn. Mounted: /mcp (the SDK's mount
        point), /health (liveness probe).

    The lifespan enters mcp_server.session_manager.run() — required by the
    streamable-HTTP SDK so per-session task groups are initialized. Without
    it, every tool call raises "Task group is not initialized."
    """
    from starlette.routing import Mount, Route

    inner = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with mcp_server.session_manager.run():
            logger.info("MCP session_manager task group running")
            yield

    app = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Mount("/", app=inner),
        ],
        middleware=[Middleware(JWTAuthMiddleware)],
        lifespan=lifespan,
    )
    return app


async def run_stdio(mcp_server) -> None:
    """Run the MCP server over stdio. Trusted single-process path."""
    await mcp_server.run_stdio_async()
