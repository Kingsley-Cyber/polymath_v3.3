# mcp modules

Module pages — subsystem index. Back to [INDEX](../INDEX.md).

6 pages:

| Page | Source | Lines | Purpose (first docstring line) |
|---|---|---|---|
| [app_guide](../polymath_mcp_app_guide.md) | `backend/polymath_mcp/app_guide.py` | 610 | Agent-facing Polymath app guide shared by MCP surfaces. |
| [auth](../polymath_mcp_auth.md) | `backend/polymath_mcp/auth.py` | 272 | MCP auth bridge — JWT validation + static API key reused from services.auth. |
| [key_store](../polymath_mcp_key_store.md) | `backend/polymath_mcp/key_store.py` | 171 | User-scoped MCP API keys. |
| [server](../polymath_mcp_server.md) | `backend/polymath_mcp/server.py` | 197 | Polymath MCP server entrypoint — Phase 8. |
| [tools](../polymath_mcp_tools.md) | `backend/polymath_mcp/tools.py` | 3811 | Polymath MCP tool surface — Phase 8.2+. |
| [transport](../polymath_mcp_transport.md) | `backend/polymath_mcp/transport.py` | 115 | MCP transport selector + Starlette auth middleware. |