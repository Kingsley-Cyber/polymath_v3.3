# tools

Source `backend/polymath_mcp/tools.py` (3811 lines) · subsystem [mcp](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Polymath MCP tool surface — Phase 8.2+.

Synthesis: imported library module; first docstring sentence: “Polymath MCP tool surface — Phase 8.2+.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `polymath_search` | 512 | `query: str, corpus_ids: list[str] \| None=None, retrieval_tier: Literal['qdrant_only', 'qdrant_mongo', 'qdrant_mongo_...` |
| `polymath_cross_corpus_search` | 587 | `query: str, corpus_ids: list[str] \| None=None, retrieval_tier: Literal['qdrant_only', 'qdrant_mongo', 'qdrant_mongo_...` |
| `search` | 633 | `query: str, corpus_ids: list[str] \| None=None, top_k: int=10, filters: dict[str, Any] \| None=None` |
| `fetch` | 700 | `id: str` |
| `polymath_chat_query` | 762 | `message: str, corpus_ids: list[str] \| None=None, retrieval_tier: Literal['qdrant_only', 'qdrant_mongo', 'qdrant_mong...` |
| `polymath_graph_query` | 1040 | `query: str, corpus_id: str \| None=None, corpus_ids: list[str] \| None=None, mode: Literal['auto', 'connect', 'gaps',...` |
| `polymath_graph_map_query` | 1173 | `query: str, corpus_id: str \| None=None, corpus_ids: list[str] \| None=None, max_hops: int=2, limit: int=80, seed_lim...` |
| `polymath_graph_question_suggestions` | 1422 | `question: str, corpus_id: str \| None=None, corpus_ids: list[str] \| None=None, model: str \| None=None, force_refres...` |
| `polymath_get_chunk_extraction` | 1515 | `corpus_id: str, chunk_id: str` |
| `polymath_search_entities` | 1538 | `corpus_id: str, query: str='', limit: int=20, doc_id: str \| None=None` |
| `polymath_get_entity_relations` | 1571 | `corpus_id: str, entity_id: str \| None=None, canonical_name: str \| None=None, limit: int=20` |
| `polymath_mcp_status` | 1611 | `detail: Literal['summary', 'full']='summary'` |
| `polymath_plan_ingestion` | 1725 | `filename: str, source_url: str \| None=None, content_type: str \| None=None, summary_required: Literal['auto', 'yes',...` |
| `polymath_check_source` | 1903 | `source_url: str \| None=None, filename: str \| None=None, content_type: str \| None=None, content_text_sample: str \|...` |
| `polymath_app_guide` | 1954 | `detail: Literal['summary', 'full']='summary'` |
| `polymath_list_corpora` | 1972 | `()` |
| `polymath_list_documents` | 2012 | `corpus_id: str, limit: int=100, offset: int=0` |
| `polymath_list_skills` | 2090 | `()` |
| `polymath_get_skill` | 2118 | `skill_id: str` |
| `polymath_list_tools` | 2143 | `()` |
| `polymath_create_corpus` | 2308 | `name: str, description: str \| None=None, preset: Literal['custom', 'fast', 'balanced', 'deep']='balanced', use_neo4j...` |
| `polymath_ingest_from_url` | 2618 | `corpus_id: str, url: str, filename: str \| None=None, duplicate_policy: Literal['skip', 'allow']='skip', summary_cost...` |
| `polymath_upload_document` | 2730 | `corpus_id: str, filename: str, content_base64: str, source_url: str \| None=None, duplicate_policy: Literal['skip', '...` |
| `polymath_get_ingest_status` | 2798 | `doc_id: str, corpus_id: str \| None=None` |
| `polymath_verify_ingestion` | 3099 | `doc_id: str, corpus_id: str \| None=None` |
| `polymath_extraction_engine` | 3176 | `()` |
| `polymath_wake_extraction_engine` | 3245 | `mac_address: str='', sidecar_url: str='', wait_seconds: int=180` |
| `polymath_set_engine_throughput` | 3312 | `vram_budget_gb: int` |
| `polymath_set_extraction_engine` | 3360 | `sidecar_url: str, mode: Literal['production', 'qualification']='production', note: str=''` |
| `polymath_delete_document` | 3454 | `corpus_id: str, doc_id: str` |
| `polymath_backfill_summaries` | 3486 | `corpus_id: str, generate: bool=True, index: bool=True, limit: int \| None=200, batch: int=32` |
| `polymath_fleet_status` | 3524 | `()` |
| `polymath_worker_stack` | 3553 | `action: str='status', workers: int \| None=None` |
| `polymath_engine_pool` | 3628 | `action: str='status', replicas: int \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `base64`, `dataclasses`, `json`, `os`, `logging`, `uuid`, `datetime`, `enum`, `typing`, `config`, `models`, `services`, `services`, `services`, `services`, `services`, `services`, `auth`, `app_guide`
- **Imports OUT** (repo-wide): `backend/routers/mcp_info.py`
- **Tests**: `backend/tests/test_mcp_ingest_slot_gate.py`, `backend/tests/test_polymath_mcp_engine_tool.py`, `backend/tests/test_polymath_mcp_ingest_tools.py`, `backend/tests/test_polymath_mcp_query_tools.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `ingest_batch_items`
- **Env vars** (name → default): `RELEX_EXPECT_RELEASE`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_mcp_ingest_slot_gate.py`, `backend/tests/test_polymath_mcp_engine_tool.py`, `backend/tests/test_polymath_mcp_ingest_tools.py`, `backend/tests/test_polymath_mcp_query_tools.py`
- Size 3811 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (3811 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 7 — e.g. “612481f feat: polymath_worker_stack nfs_remount action — agentless recovery for the stale-NFS-volume hang”. Full list: `git log --all --oneline -- backend/polymath_mcp/tools.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

## VERIFY

Drift check — grep the repo and confirm these still hold (run `docs/wiki/verify_claims.py`):

- `lane-manager fallback http://192.168.1.83:8085` → backend/polymath_mcp/tools.py:3582
- `RTX_LANE_MANAGER_API_KEY env` → backend/polymath_mcp/tools.py:3585