# summary_provider_pool

Source `backend/services/ingestion/summary_provider_pool.py` (321 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Resolve the production summary-provider pool without exposing secrets.

Synthesis: imported library module; first docstring sentence: “Resolve the production summary-provider pool without exposing secrets.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `prepare_summary_provider_pool` | 116 | `refs: Iterable[Any]` |
| `resolve_summary_provider_pool` | 164 | `*, configured_refs: Iterable[Any] \| None, runtime_refs: Iterable[Any] \| None=None, user_id: str \| None=None, db: A...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `typing`, `httpx`, `config`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/batches.py`, `backend/services/ingestion/document_summaries.py`, `backend/services/ingestion/summary_jobs.py`, `backend/services/ingestion/worker.py`, `backend/services/ingestion_service.py`
- **Tests**: `backend/tests/test_ghost_a_summary_fallback.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_ghost_a_summary_fallback.py`
- Size 321 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “6086601 fix: isolate summary lanes by credential”. Full list: `git log --all --oneline -- backend/services/ingestion/summary_provider_pool.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
