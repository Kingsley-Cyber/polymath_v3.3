# auth

Source `backend/polymath_mcp/auth.py` (272 lines) · subsystem [mcp](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> MCP auth bridge — JWT validation + static API key reused from services.auth.

Synthesis: imported library module; first docstring sentence: “MCP auth bridge — JWT validation + static API key reused from services.auth.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `set_current_user_id` | 78 | `user_id: Optional[str], scopes: list[str] \| tuple[str, ...] \| set[str] \| None=None` |
| `set_current_auth_context` | 86 | `auth: MCPAuthContext \| None` |
| `get_current_user_id` | 95 | `()` |
| `get_current_scopes` | 100 | `()` |
| `require_mcp_scope` | 108 | `scope: str` |
| `extract_bearer_token` | 115 | `authorization_header: str \| None` |
| `validate_api_key` | 126 | `token: str \| None` |
| `validate_token` | 145 | `token: str \| None` |
| `validate_token_async` | 168 | `token: str \| None` |
| `validate_token_context_async` | 179 | `token: str \| None` |
| `allowed_corpus_ids` | 211 | `user_id: Optional[str]` |
| `filter_corpus_ids` | 234 | `requested: list[str] \| None, allowed: set[str]` |
| `resolve_request_scope` | 248 | `requested_corpus_ids: list[str] \| None` |
| `assert_corpus_allowed` | 261 | `corpus_id: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hmac`, `logging`, `contextvars`, `dataclasses`, `typing`, `config`, `services`, `services`, `services`
- **Imports OUT**: none found — dead-code candidate.
- **Tests**: `backend/tests/test_polymath_mcp_engine_tool.py`, `backend/tests/test_polymath_mcp_ingest_tools.py`, `backend/tests/test_polymath_mcp_query_tools.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_polymath_mcp_engine_tool.py`, `backend/tests/test_polymath_mcp_ingest_tools.py`, `backend/tests/test_polymath_mcp_query_tools.py`
- Size 272 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
