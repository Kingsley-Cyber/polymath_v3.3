# tier0

Source `backend/services/ingestion/tier0.py` (321 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> W1 Tier-0 — auto-embed the document ROUTING CARD at ingest.

Synthesis: imported library module; first docstring sentence: “W1 Tier-0 — auto-embed the document ROUTING CARD at ingest.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `embed_doc_profiles` | 54 | `db, client, *, corpus_id: str, doc_ids: list[str], dim: int, api_key: str \| None=None` |
| `embed_doc_profile` | 171 | `db, client, *, corpus_id: str, doc_id: str, dim: int, api_key: str \| None=None` |
| `delete_doc_profile` | 192 | `client, *, corpus_id: str, doc_id: str` |
| `delete_corpus_doc_profiles` | 210 | `client, *, corpus_id: str` |
| `reconcile_doc_profile_projection_state` | 234 | `db, client, *, corpus_id: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `datetime`, `typing`, `pymongo`
- **Imports OUT** (repo-wide): `backend/scripts/reconcile_tier0_profiles.py`, `backend/scripts_probe_tier0.py`, `backend/services/ingestion/document_summaries.py`, `backend/services/ingestion/worker.py`, `backend/services/ingestion_service.py`, `backend/services/retriever/four_lane_router.py`, `backend/services/retriever/tier0_router.py`, `backend/services/retriever/vocabulary.py`
- **Tests**: `backend/tests/test_document_summaries.py`, `backend/tests/test_tier0_ingestion.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `documents`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_document_summaries.py`, `backend/tests/test_tier0_ingestion.py`
- Size 321 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
