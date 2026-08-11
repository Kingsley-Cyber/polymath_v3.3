# summary_backfill

Source `backend/services/ingestion/summary_backfill.py` (827 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Summary-tier backfill + readiness guardrail.

Synthesis: a one-shot operational tool (invoked via `python backend/services/ingestion/summary_backfill.py`), not a runtime service; first docstring sentence: “Summary-tier backfill + readiness guardrail.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus`, `--heal-all`, `--apply-heal`, `--index`, `--generate`, `--repair`, `--apply`, `--reindex`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `child_context_for_rows` | 115 | `db, corpus_id: str, rows: list[dict]` |
| `summary_record_from_result` | 178 | `result` |
| `summary_write_from_result` | 213 | `result, *, source_text: str \| None, updated_at: datetime` |
| `summary_result_fields` | 231 | `write: ParentSummaryWrite` |
| `summary_index_text` | 244 | `row: dict` |
| `index_existing` | 259 | `corpus_id: str, *, batch: int=256` |
| `generate` | 301 | `corpus_id: str, *, batch: int=400, limit: int \| None=None, summary_cost_run_id: str \| None=None, summary_cost_autho...` |
| `repair_existing` | 477 | `corpus_id: str, *, batch: int=500, limit: int \| None=None, apply: bool=False, reindex: bool=False` |
| `verify` | 661 | `corpus_id: str` |
| `heal_all` | 703 | `*, apply: bool=False` |
| `main` | 737 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `json`, `os`, `uuid`, `datetime`, `config`, `models`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/summary_vector_reconcile.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_parent_summary_contract.py`, `backend/tests/test_summary_backfill_scoped.py`, `backend/tests/test_summary_semantics_temporal.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `parent_chunks`
- **Env vars** (name → default): `POLYMATH_SUMMARY_POOL_FILE`→`/tmp/summary_pool.json`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_parent_summary_contract.py`, `backend/tests/test_summary_backfill_scoped.py`, `backend/tests/test_summary_semantics_temporal.py`
- Size 827 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (827 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 4 — e.g. “9b1f528 fix: harden and optimize durable ingestion”. Full list: `git log --all --oneline -- backend/services/ingestion/summary_backfill.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
