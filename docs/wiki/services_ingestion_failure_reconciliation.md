# failure_reconciliation

Source `backend/services/ingestion/failure_reconciliation.py` (531 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Ghost B failure-metadata reconciliation.

Synthesis: imported library module; first docstring sentence: “Ghost B failure-metadata reconciliation.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `classify_failure_row_staleness` | 90 | `row: dict[str, Any], *, chunk: dict[str, Any] \| None, doc: dict[str, Any] \| None, current_contract_hash: str \| Non...` |
| `repair_action_for_stale_reason` | 121 | `reason: str \| None` |
| `classify_stale_failure_rows` | 129 | `db: Any, *, corpus_id: str, limit: int=5000` |
| `build_document_failure_reconciliation` | 212 | `*, doc: dict[str, Any], split_error_rows: list[dict[str, Any]] \| None=None, live_chunk_ids: set[str] \| None=None, s...` |
| `reconcile_ghost_b_failure_metadata` | 275 | `db: Any, *, corpus_id: str, apply: bool=False, limit: int=5000` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `datetime`, `typing`, `uuid`, `pymongo`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/routers/ingestion.py`, `backend/services/ingestion/corpus_repair.py`, `backend/services/ingestion/readiness.py`
- **Tests**: `backend/tests/test_failure_reconciliation.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `documents`, `extraction_jobs`, `ghost_b_extractions`, `ingest_repair_runs`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_failure_reconciliation.py`
- Size 531 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (531 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “9b1f528 fix: harden and optimize durable ingestion”. Full list: `git log --all --oneline -- backend/services/ingestion/failure_reconciliation.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
