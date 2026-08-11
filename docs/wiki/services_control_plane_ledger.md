# ledger

Source `backend/services/control_plane/ledger.py` (359 lines) · subsystem [control-plane](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable intake + workflow ledger (`ingestion_runs`, `stage_attempts`).

Synthesis: imported library module; first docstring sentence: “Durable intake + workflow ledger (`ingestion_runs`, `stage_attempts`).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `graphify_receipt_id` | 41 | `*, run_id: str, stage: str, input_hash: str, release: str` |
| `get_graphify_stage_receipt` | 50 | `db: Any, *, receipt_id: str` |
| `record_graphify_stage_receipt` | 60 | `db: Any, *, receipt_id: str, run_id: str, corpus_id: str, doc_id: str, stage: str, status: str, receipt: dict[str, Any]` |
| `run_id_for` | 102 | `*, corpus_id: str, doc_id: str` |
| `ensure_ledger_indexes` | 107 | `db: Any` |
| `create_ingestion_run` | 131 | `db: Any, *, corpus_id: str, doc_id: str, user_id: str \| None=None, source: str='upload', filename: str \| None=None,...` |
| `get_run` | 192 | `db: Any, *, run_id: str` |
| `list_runs` | 196 | `db: Any, *, corpus_id: str, status: str \| None=None, limit: int=200` |
| `update_run_from_proof` | 211 | `db: Any, *, run_id: str, proof: dict[str, Any]` |
| `record_stage_attempt` | 254 | `db: Any, *, corpus_id: str, stage: str, action: str, status: str, run_id: str \| None=None, doc_id: str \| None=None,...` |
| `reconcile_runs_for_corpus` | 296 | `db: Any, *, corpus_id: str, limit: int \| None=None` |
| `sweep_outbox` | 333 | `db: Any, *, corpus_id: str \| None=None, limit: int=500` |
| `mark_outbox_consumed` | 347 | `db: Any, *, run_ids: list[str]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `logging`, `datetime`, `typing`
- **Imports OUT** (repo-wide): `backend/main.py`, `backend/polymath_mcp/tools.py`, `backend/routers/control_plane.py`, `backend/routers/ingestion.py`, `backend/scripts/run_graphify_fixture_e2e.py`, `backend/services/control_plane/reconciler.py`, `backend/services/extraction/graphify_pipeline.py`, `backend/services/ingestion_service.py`, `backend/services/storage/mongo_writer.py`
- **Tests**: `backend/tests/test_control_plane_v2.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_control_plane_v2.py`
- Size 359 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
