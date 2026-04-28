"""Polymath MCP server entrypoint — Phase 8.

Sidecar process. Lifecycle:
    1. Initialize backend service singletons (auth, conversation, ingestion).
       These connect MongoDB / Qdrant / Neo4j the same way the FastAPI lifespan does.
    2. Construct a FastMCP server and register the 4 Phase 8 tools.
    3. Dispatch on MCP_TRANSPORT:
         - 'streamable-http' → uvicorn on MCP_HOST:MCP_PORT (production / compose)
         - 'stdio'           → attach to stdin/stdout (local Claude Desktop proxy)

Run:
    python -m mcp.server                   # inside the backend/ image
    or
    python -m mcp.server --transport stdio  # for stdio
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from config import get_settings

logger = logging.getLogger("polymath.mcp")


async def _bootstrap_services() -> None:
    """Connect the same Mongo / Qdrant / Neo4j clients the FastAPI lifespan owns.

    Tools call into services.* modules whose singletons need to be initialized
    before any request lands. This mirrors backend/main.py's lifespan but lives
    in a separate process for the sidecar.

    ConversationService creates its own Motor client from settings; we then
    hand its `_db` reference to auth_service and ingestion_service so all three
    share the same connection pool.
    """
    from services.auth import auth_service
    from services.conversation import conversation_service
    from services.ingestion_service import ingestion_service

    settings = get_settings()

    await conversation_service.connect()
    db = conversation_service._db
    if db is None:
        raise RuntimeError(
            "ConversationService.connect() did not initialize MongoDB handle"
        )
    await auth_service.connect(db)
    await ingestion_service.connect(db)

    logger.info(
        "MCP services connected: mongo=%s qdrant=%s neo4j=%s",
        settings.MONGODB_URI.split("@")[-1],
        settings.QDRANT_URL,
        settings.NEO4J_URI if settings.NEO4J_ENABLED else "DISABLED",
    )


def _build_mcp_server():
    """Construct the FastMCP server and register the 4 tools."""
    from mcp.server.fastmcp import FastMCP

    from . import tools

    mcp = FastMCP(
        name="polymath",
        instructions=(
            "Polymath RAG tools — search corpora, fetch chunk extractions, "
            "list entities, traverse relations. All tools are corpus-scoped "
            "and respect the authenticated user's allowed corpus set."
        ),
    )

    for fn in tools.ALL_TOOLS:
        mcp.add_tool(fn)

    logger.info(
        "MCP tools registered: %s", ", ".join(fn.__name__ for fn in tools.ALL_TOOLS)
    )
    return mcp


async def _run_streamable_http(mcp) -> None:
    import uvicorn

    from .transport import build_streamable_app

    settings = get_settings()
    app = build_streamable_app(mcp)
    config = uvicorn.Config(
        app,
        host=settings.MCP_HOST,
        port=settings.MCP_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    logger.info(
        "MCP streamable-HTTP listening on %s:%d",
        settings.MCP_HOST,
        settings.MCP_PORT,
    )
    await server.serve()


async def _run_stdio(mcp) -> None:
    from .transport import run_stdio

    logger.info("MCP stdio transport: attached to stdin/stdout")
    await run_stdio(mcp)


async def _amain(transport_override: str | None = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    settings = get_settings()
    transport = transport_override or settings.MCP_TRANSPORT

    await _bootstrap_services()
    mcp = _build_mcp_server()

    if transport == "streamable-http":
        await _run_streamable_http(mcp)
    elif transport == "stdio":
        await _run_stdio(mcp)
    else:
        raise SystemExit(f"Unknown MCP_TRANSPORT={transport!r}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="polymath-mcp")
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "stdio"),
        default=None,
        help="Override MCP_TRANSPORT env var.",
    )
    args = parser.parse_args()
    try:
        asyncio.run(_amain(args.transport))
    except KeyboardInterrupt:
        logger.info("MCP server interrupted; exiting.")
        sys.exit(0)


if __name__ == "__main__":
    main()
