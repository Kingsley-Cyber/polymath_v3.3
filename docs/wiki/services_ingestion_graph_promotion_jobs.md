# graph_promotion_jobs

Source `backend/services/ingestion/graph_promotion_jobs.py` (1384 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Durable graph-promotion job queue.

Synthesis: imported library module; first docstring sentence: “Durable graph-promotion job queue.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `graph_gap_reason` | 59 | `row: dict[str, Any]` |
| `graph_job_id` | 72 | `*, corpus_id: str, doc_id: str, reason: str` |
| `graph_promotion_contract_hash` | 77 | `row: dict[str, Any]` |
| `extraction_artifact_id` | 94 | `row: dict[str, Any]` |
| `classify_graph_promotion_candidate` | 112 | `row: dict[str, Any]` |
| `mark_doc_extractions_promoted` | 401 | `db: Any, *, corpus_id: str, doc_id: str, graph_job_id: str \| None=None, result: dict[str, Any] \| None=None, promote...` |
| `backfill_promoted_extraction_marks` | 464 | `db: Any, *, corpus_id: str, apply: bool=False, limit: int=100` |
| `plan_graph_promotion_jobs` | 657 | `db: Any, *, corpus_id: str, user_id: str \| None=None, apply: bool=False, limit: int=100, max_chunks: int \| None=None` |
| `reevaluate_stale_graph_promotion_jobs` | 722 | `db: Any, *, corpus_id: str, user_id: str \| None=None, limit: int=500` |
| `list_graph_promotion_jobs` | 829 | `db: Any, *, corpus_id: str, limit: int=100, statuses: list[str] \| None=None` |
| `release_gate_mode` | 859 | `()` |
| `active_release_pin` | 875 | `()` |
| `instrument_canonical_writer_calls` | 892 | `counter: dict[str, int]` |
| `evaluate_release_gate` | 925 | `release: Any, *, mode: str \| None=None` |
| `authorize_canonical_graph_write` | 969 | `*, mode: str, release_pin: ReleasePin \| None` |
| `evaluate_cli_graph_write_gate` | 1001 | `*, script_name: str, operator: str` |
| `cli_operator_identity` | 1042 | `()` |
| `run_graph_promotion_jobs` | 1051 | `db: Any, *, qdrant_client: Any, neo4j_driver: Any, corpus_id: str, user_id: str, limit: int=5, release: ReleasePin \|...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `time`, `datetime`, `typing`, `pydantic`, `pymongo`, `db`, `models`, `models`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/canary_graph_release_gate_probe.py`, `backend/scripts/polymath_failed_chunk_backfill.py`, `backend/scripts/polymath_graph_replay_backlog.py`, `backend/scripts/promote_backfilled_relations.py`, `backend/scripts/shadow_a_graph_release_gate_probe.py`, `backend/scripts/shadow_b_graph_release_gate_probe.py`, `backend/services/control_plane/reconciler.py`, `backend/services/ingestion/corpus_commander.py`, `backend/services/ingestion/corpus_repair.py`, `backend/services/ingestion/worker.py` …
- **Tests**: `backend/tests/test_graph_promotion_jobs.py`, `backend/tests/test_graph_promotion_release_gate.py`, `backend/tests/test_release_registry_loader.py`, `backend/tests/test_release_stamp_step3.py`, `backend/tests/test_route_readiness_certificate.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `extraction_jobs`, `ghost_b_extractions`, `graph_promotion_jobs`
- **Env vars** (no default captured): `USER`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_graph_promotion_jobs.py`, `backend/tests/test_graph_promotion_release_gate.py`, `backend/tests/test_release_registry_loader.py`, `backend/tests/test_release_stamp_step3.py`, `backend/tests/test_route_readiness_certificate.py`
- Size 1384 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1384 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
