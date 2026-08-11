# batches

Source `backend/services/ingestion/batches.py` (3674 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable ingestion batch helpers.

Synthesis: imported library module; first docstring sentence: “Durable ingestion batch helpers.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `reconcile_batch_enrichment_truth` | 443 | `db: AsyncIOMotorDatabase, *, batch_id: str, user_id: str \| None=None` |
| `reconcile_pending_batch_enrichment_truth` | 661 | `db: AsyncIOMotorDatabase, *, corpus_id: str, doc_ids: list[str] \| None=None, limit: int=500` |
| `discover_local_files` | 744 | `root_path: str, *, recursive: bool=True, extensions: list[str] \| None=None, max_files: int \| None=None` |
| `create_local_batch` | 844 | `*, db: AsyncIOMotorDatabase, corpus_id: str, user_id: str, root_path: str, recursive: bool=True, extensions: list[str...` |
| `create_upload_batch` | 980 | `*, db: AsyncIOMotorDatabase, corpus_id: str, user_id: str, files: list[dict[str, Any]], max_total_bytes: int \| None=...` |
| `get_batch` | 1132 | `db: AsyncIOMotorDatabase, batch_id: str, *, user_id: str, include_items: bool=True, item_limit: int=500` |
| `list_batches` | 1168 | `db: AsyncIOMotorDatabase, corpus_id: str, *, user_id: str, limit: int=10, include_archived: bool=False` |
| `append_new_files_to_batch` | 1198 | `*, db: AsyncIOMotorDatabase, batch_id: str, user_id: str` |
| `refresh_batch_counts` | 1323 | `db: AsyncIOMotorDatabase, batch_id: str, *, user_id: str \| None=None` |
| `reconcile_stale_items` | 1593 | `db: AsyncIOMotorDatabase, *, batch_id: str \| None=None, user_id: str \| None=None, stale_after_minutes: int \| None=...` |
| `requeue_failed_items_for_resume` | 1640 | `db: AsyncIOMotorDatabase, *, batch_id: str, user_id: str` |
| `recover_local_batch_runners` | 1704 | `*, db: AsyncIOMotorDatabase, ingestion_service: Any, user_id: str \| None=None, max_batches: int=100, reclaim_active_...` |
| `run_local_batch` | 3146 | `*, db: AsyncIOMotorDatabase, ingestion_service: Any, batch_id: str, user_id: str` |
| `start_local_batch_runner` | 3585 | `*, db: AsyncIOMotorDatabase, ingestion_service: Any, batch_id: str, user_id: str` |
| `_AdjustableDocSemaphore.set_limit` | 3538 | `self, limit: int` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `logging`, `mimetypes`, `os`, `shutil`, `uuid`, `contextlib`, `datetime`, `pathlib`, `typing`, `motor`, `pymongo`, `config`, `models`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/main.py`, `backend/routers/ingestion.py`, `backend/services/ingestion/corpus_repair.py`, `backend/services/ingestion/source_parse_jobs.py`, `backend/services/ingestion/summary_jobs.py`
- **Tests**: `backend/tests/test_batch_summary_cost_gate.py`, `backend/tests/test_ingest_batches.py`, `backend/tests/test_ingest_deferred_summary.py`, `backend/tests/test_pass_overlap_claims.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `ingest_lane_leases`, `ingest_repair_runs`, `parent_chunks`, `summary_tree`
- **Env vars** (name → default): `INGEST_MAX_ACTIVE_BATCHES`→`0`, `INGEST_PASS_OVERLAP`→`1`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_batch_summary_cost_gate.py`, `backend/tests/test_ingest_batches.py`, `backend/tests/test_ingest_deferred_summary.py`, `backend/tests/test_pass_overlap_claims.py`
- Size 3674 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (3674 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “6. Per-collection write amplification for one document ingest” — link into the map, do not duplicate it.
- Defect-fix commits: 11 — e.g. “a9bc153 fix: lane adoption widened to any dead batch-runner owner”. Full list: `git log --all --oneline -- backend/services/ingestion/batches.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

## VERIFY

Drift check — grep the repo and confirm these still hold (run `docs/wiki/verify_claims.py`):

- `livelock guard matches lease-adoption semantics` → backend/services/ingestion/batches.py:1825-1829
- `lease_seconds from INGEST_STALE_JOB_MINUTES` → backend/services/ingestion/batches.py:3224