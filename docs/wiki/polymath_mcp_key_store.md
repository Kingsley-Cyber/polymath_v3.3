# key_store

Source `backend/polymath_mcp/key_store.py` (171 lines) · subsystem [mcp](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> User-scoped MCP API keys.

Synthesis: imported library module; first docstring sentence: “User-scoped MCP API keys.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `generate_plaintext_key` | 28 | `()` |
| `hash_mcp_key` | 33 | `token: str` |
| `normalize_scopes` | 38 | `scopes: list[str] \| tuple[str, ...] \| None` |
| `ensure_mcp_key_indexes` | 58 | `db: Any` |
| `create_mcp_key` | 70 | `db: Any, *, user_id: str, name: str \| None=None, scopes: list[str] \| tuple[str, ...] \| None=None` |
| `list_mcp_keys` | 124 | `db: Any, *, user_id: str` |
| `revoke_mcp_key` | 135 | `db: Any, *, user_id: str, key_id: str` |
| `validate_user_mcp_key` | 145 | `db: Any, token: str \| None` |
| `validate_user_mcp_key_details` | 151 | `db: Any, token: str \| None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `secrets`, `datetime`, `typing`, `uuid`
- **Imports OUT** (repo-wide): `backend/routers/mcp_info.py`
- **Tests**: `backend/tests/test_mcp_api_keys.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_mcp_api_keys.py`
- Size 171 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
