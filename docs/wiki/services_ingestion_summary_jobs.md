# summary_jobs

Source `backend/services/ingestion/summary_jobs.py` (1516 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable summary job planner.

Synthesis: imported library module; first docstring sentence: “Durable summary job planner.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `reconcile_satisfied_summary_jobs` | 70 | `db: Any, *, corpus_id: str, limit: int=5000` |
| `summary_provider_contract` | 189 | `corpus: dict[str, Any] \| None` |
| `summary_contract_hash` | 221 | `corpus: dict[str, Any] \| None` |
| `summary_job_id` | 234 | `*, corpus_id: str, kind: str, target_id: str, source_hash: str, contract_hash: str` |
| `build_parent_summary_job` | 249 | `*, parent: dict[str, Any], doc: dict[str, Any] \| None, corpus: dict[str, Any] \| None` |
| `classify_document_summary_status` | 294 | `*, required_parent_count: int, summarized_parent_count: int` |
| `build_document_summary_job` | 307 | `*, doc: dict[str, Any], corpus: dict[str, Any] \| None, required_parent_count: int, summarized_parent_count: int` |
| `reevaluate_blocked_summary_jobs` | 751 | `db: Any, *, corpus_id: str, limit: int=500, doc_ids: list[str] \| None=None` |
| `plan_summary_jobs` | 856 | `db: Any, *, corpus_id: str, user_id: str \| None=None, apply: bool=False, limit: int=500, kinds: list[str] \| None=No...` |
| `backfill_summary_stage_identity` | 1087 | `db: Any, *, corpus_id: str, user_id: str \| None=None, apply: bool=False, limit: int=1000` |
| `run_summary_jobs` | 1271 | `db: Any, *, corpus_id: str, user_id: str \| None=None, limit: int=25, statuses: list[str] \| None=None, kinds: list[s...` |
| `list_summary_jobs` | 1485 | `db: Any, *, corpus_id: str, limit: int=100, statuses: list[str] \| None=None, kinds: list[str] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `datetime`, `typing`, `pymongo`, `db`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/run_deterministic_summary_materialization.py`, `backend/services/control_plane/desired_state.py`, `backend/services/control_plane/reconciler.py`, `backend/services/ingestion/corpus_commander.py`, `backend/services/ingestion/corpus_repair.py`, `backend/services/ingestion/summary_vector_reconcile.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_corpus_commander_invariants.py`, `backend/tests/test_q9_parse_policy.py`, `backend/tests/test_summary_jobs.py`, `backend/tests/test_summary_readiness_baseline.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `parent_chunks`, `summary_jobs`, `summary_tree`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_corpus_commander_invariants.py`, `backend/tests/test_q9_parse_policy.py`, `backend/tests/test_summary_jobs.py`, `backend/tests/test_summary_readiness_baseline.py`
- Size 1516 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1516 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 3 — e.g. “59f8b53 fix: harden ingestion and retrieval contracts”. Full list: `git log --all --oneline -- backend/services/ingestion/summary_jobs.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
