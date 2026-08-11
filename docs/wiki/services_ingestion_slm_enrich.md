# slm_enrich

Source `backend/services/ingestion/slm_enrich.py` (301 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Pass-1 deterministic + Pass-2 SLM-residual enrichment for the local lane.

Synthesis: imported library module; first docstring sentence: “Pass-1 deterministic + Pass-2 SLM-residual enrichment for the local lane.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `pass1_enrich` | 71 | `results: list[Any]` |
| `pass2_enrich` | 188 | `results: list[Any]` |
| `run_enrichment` | 283 | `results: list[Any]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `os`, `typing`, `httpx`, `pydantic`, `services`, `enrich`
- **Imports OUT**: none found — dead-code candidate.
- **Env vars** (name → default): `LOCAL_SLM_ENRICH_TIMEOUT_S`→`30`, `LOCAL_SLM_ENRICH_URL`→`http://localhost:8083`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 301 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
