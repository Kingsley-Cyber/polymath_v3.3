# extraction_jobs

Source `backend/services/ingestion/extraction_jobs.py` (1604 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable chunk-level extraction job planner.

Synthesis: imported library module; first docstring sentence: “Durable chunk-level extraction job planner.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `active_ingest_doc_ids` | 56 | `db: Any, *, corpus_id: str, now: datetime \| None=None` |
| `chunk_content_hash` | 88 | `chunk: dict[str, Any]` |
| `extraction_provider_pool` | 100 | `doc: dict[str, Any] \| None` |
| `with_live_extraction_config` | 114 | `doc: dict[str, Any], live_config: dict[str, Any] \| None` |
| `extraction_provider_contract` | 133 | `doc: dict[str, Any] \| None` |
| `extraction_contract_hash` | 156 | `doc: dict[str, Any] \| None` |
| `extraction_job_id` | 170 | `*, corpus_id: str, doc_id: str, chunk_id: str, chunk_hash: str, contract_hash: str` |
| `classify_extraction_status` | 184 | `row: dict[str, Any] \| None` |
| `build_extraction_job` | 266 | `*, chunk: dict[str, Any], doc: dict[str, Any] \| None, extraction_row: dict[str, Any] \| None=None` |
| `build_extraction_job_run_update` | 505 | `job: dict[str, Any], *, succeeded: bool=False, result: dict[str, Any] \| None=None, failure: dict[str, Any] \| None=N...` |
| `terminal_extraction_artifact_matches_job` | 630 | `job: dict[str, Any], extraction_row: dict[str, Any]` |
| `reconcile_terminal_extraction_jobs` | 654 | `db: Any, *, corpus_id: str, user_id: str \| None=None, limit: int=5000` |
| `plan_extraction_jobs` | 777 | `db: Any, *, corpus_id: str, user_id: str \| None=None, apply: bool=False, limit: int=500, include_succeeded: bool=False` |
| `run_extraction_jobs` | 1235 | `db: Any, *, qdrant_client: Any, corpus_id: str, user_id: str \| None=None, limit: int=25, statuses: list[str] \| None...` |
| `list_extraction_jobs` | 1583 | `db: Any, *, corpus_id: str, limit: int=100, statuses: list[str] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `hashlib`, `json`, `dataclasses`, `datetime`, `typing`, `pymongo`, `db`, `models`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/control_plane/desired_state.py`, `backend/services/control_plane/reconciler.py`, `backend/services/ingestion/corpus_commander.py`, `backend/services/ingestion/corpus_repair.py`, `backend/services/ingestion/extraction_burst.py`, `backend/services/ingestion/failure_reconciliation.py`, `backend/services/ingestion/stage_identity_repair.py`, `backend/services/ingestion_service.py`, `backend/services/storage/mongo_writer.py`
- **Tests**: `backend/tests/test_extraction_jobs.py`, `backend/tests/test_extraction_parity_burst.py`, `backend/tests/test_release_stamp_step3.py`, `backend/tests/test_stage_identity_repair.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `extraction_jobs`, `ghost_b_extractions`, `ingest_batch_items`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_extraction_jobs.py`, `backend/tests/test_extraction_parity_burst.py`, `backend/tests/test_release_stamp_step3.py`, `backend/tests/test_stage_identity_repair.py`
- Size 1604 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1604 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “2. Job status state machine table”, “4. Chain-argument table for one hypothetical tick”, “6. Per-collection write amplification for one document ingest”, “`backend/db/queue_integrity.py`” — link into the map, do not duplicate it.
- Defect-fix commits: 1 — e.g. “d399aff fix: extraction-job claims lease 7200s — mid-run expiry was discarding completed work”. Full list: `git log --all --oneline -- backend/services/ingestion/extraction_jobs.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.

## VERIFY

Drift check — grep the repo and confirm these still hold (run `docs/wiki/verify_claims.py`):

- `lease_seconds=7200` → backend/services/ingestion/extraction_jobs.py:1240,1342
- `extend_running_job_leases renewal 1800s` → backend/services/ingestion/extraction_jobs.py:1470
- `extraction_job_id includes contract_hash+chunk_hash` → backend/services/ingestion/extraction_jobs.py:170