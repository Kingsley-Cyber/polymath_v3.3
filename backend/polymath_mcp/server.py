"""Polymath MCP server entrypoint — Phase 8.

Sidecar process. Lifecycle:
    1. Initialize backend service singletons (auth, conversation, ingestion).
       These connect MongoDB / Qdrant / Neo4j the same way the FastAPI lifespan does.
    2. Construct a FastMCP server and register the Polymath MCP tools.
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
    from services.model_pool import model_pool_service
    from services.model_profiles import model_profiles_service
    from services.query_prefs import query_prefs_service
    from services.settings import settings_service

    settings = get_settings()

    await conversation_service.connect()
    db = conversation_service._db
    if db is None:
        raise RuntimeError(
            "ConversationService.connect() did not initialize MongoDB handle"
        )
    await auth_service.connect(db)
    await ingestion_service.connect(db)

    # Attach the DB-backed singletons the chat/retrieval path reads — the SAME
    # set the FastAPI lifespan attaches (backend/main.py:182-194). Without these,
    # polymath_chat_query raises "<Service> not attached to a DB": the in-process
    # chat_orchestrator resolves models, pools, per-user query prefs, and global
    # retrieval settings (e.g. source_cap) through them. attach() only binds the
    # shared `db` handle — no extra network connection.
    settings_service.attach(db)
    model_profiles_service.attach(db)
    model_pool_service.attach(db)
    query_prefs_service.attach(db)

    logger.info(
        "MCP services connected: mongo=%s qdrant=%s neo4j=%s",
        settings.MONGODB_URI.split("@")[-1],
        settings.QDRANT_URL,
        settings.NEO4J_URI if settings.NEO4J_ENABLED else "DISABLED",
    )


def _build_mcp_server():
    """Construct the FastMCP server and register the tool surface."""
    from urllib.parse import urlparse

    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    from . import tools

    # DNS-rebinding protection. The SDK auto-enables a localhost-only allowlist
    # when the server host looks local, which 421s ("Invalid Host header") any
    # request whose Host is public — e.g. everything arriving through the
    # Cloudflare tunnel at MCP_PUBLIC_URL. Allowlist the public host (+ localhost
    # for in-container smoke tests) so remote agents (Hermes, OpenClaw) connect,
    # while keeping the protection on. Bearer auth is still the real gate.
    s = get_settings()
    allowed_hosts = [
        "localhost", "127.0.0.1",
        f"localhost:{s.MCP_PORT}", f"127.0.0.1:{s.MCP_PORT}",
    ]
    allowed_origins = [
        "http://localhost", "http://127.0.0.1",
        f"http://localhost:{s.MCP_PORT}", f"http://127.0.0.1:{s.MCP_PORT}",
    ]
    public = (s.MCP_PUBLIC_URL or "").strip()
    if public:
        parsed = urlparse(public)
        if parsed.hostname:
            allowed_hosts.append(parsed.hostname)
            if parsed.port:
                allowed_hosts.append(f"{parsed.hostname}:{parsed.port}")
        allowed_origins.append(public.rstrip("/"))
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )

    mcp = FastMCP(
        name="polymath",
        instructions=(
            "Polymath RAG — full read/write research toolkit. You are talking "
            "to a knowledge base that you can search, synthesize over, and "
            "extend with new documents.\n\n"
            "CAPABILITIES:\n"
            "  • SEARCH      polymath_search, polymath_cross_corpus_search — "
            "retrieve evidence chunks across corpora with the same vector, "
            "hybrid, Graph Augmented, profile, rerank, and search-mode knobs "
            "as chat.\n"
            "  • SYNTHESIZE  polymath_chat_query — natural-language question, "
            "grounded answer using the live chat RAG path, including facet "
            "coverage, multi-corpus retrieval, optional web/search tools, "
            "skills, and reasoning-cascade metadata.\n"
            "  • ANALYZE     polymath_graph_map_query, polymath_graph_query, "
            "polymath_graph_question_suggestions, polymath_search_entities, "
            "polymath_get_entity_relations, polymath_get_chunk_extraction — "
            "structural / thematic / provenance reads on the Neo4j graph.\n"
            "  • DISCOVER    polymath_list_corpora, polymath_list_documents, "
            "polymath_list_skills, polymath_list_tools.\n"
            "  • EXTEND      polymath_create_corpus, polymath_ingest_from_url, "
            "polymath_upload_document, polymath_get_ingest_status, "
            "polymath_delete_document.\n\n"
            "RESEARCH WORKFLOW (run in this order when extending the KB):\n"
            "  1. DISCOVER — list_corpora to see what's already indexed.\n"
            "  2. SEARCH   — query existing corpora before deciding to ingest "
            "new material; avoid duplicating work the user already paid to "
            "embed.\n"
            "  3. PLAN     — decide whether to add to an existing corpus or "
            "create a dedicated one for the project.\n"
            "  4. INGEST   — create_corpus if needed, then ingest_from_url "
            "(public URL) or upload_document (base64 body) per file.\n"
            "  5. POLL     — get_ingest_status until status='complete'. "
            "Ingestion is async; do not assume content is searchable until "
            "you have seen 'complete'.\n"
            "  6. VERIFY   — search for known terms from the new document to "
            "confirm it is retrievable, then chat_query to summarize the "
            "incorporated material.\n"
            "  7. REPORT   — surface the findings + the new corpus_id(s) to "
            "the user.\n\n"
            "CONSTRAINTS:\n"
            "  • All tools respect the caller's corpus ACL — you cannot read, "
            "write, or delete a corpus the authenticated user does not own.\n"
            "  • Ingestion size cap: 50 MB per file by default. Larger files "
            "must go through the multipart HTTP endpoint, not MCP.\n"
            "  • polymath_ingest_from_url blocks private/loopback IPs by "
            "default (SSRF safety). Public arXiv / GitHub / S3 URLs are "
            "fine; intranet URLs are not.\n"
            "  • Deletions are not reversible. Use polymath_delete_document "
            "only to clean up confirmed-failed ingests."
        ),
        transport_security=transport_security,
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
