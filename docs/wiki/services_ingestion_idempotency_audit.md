# idempotency_audit

Source `backend/services/ingestion/idempotency_audit.py` (352 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Exact source identity audit for corpus ingestion.

Synthesis: imported library module; first docstring sentence: “Exact source identity audit for corpus ingestion.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `summarize_identity_audit` | 208 | `*, corpus_id: str, doc_total: int, source_keyed_documents: int, content_hash_documents: int, duplicate_source_key_gro...` |
| `audit_corpus_idempotency` | 283 | `db: Any, *, corpus_id: str, group_limit: int=25, missing_limit: int=25` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_idempotency_audit.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `documents`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_idempotency_audit.py`
- Size 352 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
