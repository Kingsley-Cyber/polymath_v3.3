# b_plus_normalizer

Source `backend/services/ingestion/b_plus_normalizer.py` (172 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> B+ tier synthetic header injection.

Synthesis: imported library module; first docstring sentence: “B+ tier synthetic header injection.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `inject_synthetic_headers` | 131 | `text: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `dataclasses`
- **Imports OUT** (repo-wide): `backend/services/ingestion/docling_adapter.py`, `backend/services/ingestion/tier_chunker.py`
- **Tests**: `backend/tests/test_tier_chunker.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_tier_chunker.py`
- Size 172 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
