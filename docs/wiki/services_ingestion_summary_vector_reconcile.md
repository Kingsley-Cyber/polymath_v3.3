# summary_vector_reconcile

Source `backend/services/ingestion/summary_vector_reconcile.py` (358 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> ID-level reconciliation for summary text vs summary vectors.

Synthesis: imported library module; first docstring sentence: “ID-level reconciliation for summary text vs summary vectors.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `required_parent_summary_ids` | 102 | `db: Any, *, corpus_id: str` |
| `indexed_summary_parent_ids` | 111 | `qdrant_client: Any, *, corpus_id: str, collection_kind: str` |
| `audit_parent_summary_vector_integrity` | 153 | `db: Any, qdrant_client: Any, *, corpus_id: str, target_kinds: list[str] \| None=None, sample_limit: int=25` |
| `repair_parent_summary_vector_integrity` | 235 | `db: Any, qdrant_client: Any, *, corpus_id: str, user_id: str \| None=None, target_kinds: list[str] \| None=None, batc...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `datetime`, `typing`, `pymongo`, `models`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/corpus_commander.py`
- **Tests**: `backend/tests/test_corpus_commander_invariants.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_corpus_commander_invariants.py`
- Size 358 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
