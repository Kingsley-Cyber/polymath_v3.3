# document_summaries

Source `backend/services/ingestion/document_summaries.py` (407 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Bounded document-level summary repair.

Synthesis: imported library module; first docstring sentence: “Bounded document-level summary repair.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `backfill_document_summaries` | 135 | `db: Any, *, corpus_id: str, qdrant_client: Any \| None=None, user_id: str \| None=None, limit: int=25, doc_ids: list[...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `datetime`, `typing`, `models`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/summary_jobs.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_deterministic_document_profile.py`, `backend/tests/test_deterministic_summary_tree.py`, `backend/tests/test_document_summaries.py`, `backend/tests/test_summary_provider_not_required.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_deterministic_document_profile.py`, `backend/tests/test_deterministic_summary_tree.py`, `backend/tests/test_document_summaries.py`, `backend/tests/test_summary_provider_not_required.py`
- Size 407 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (407 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
