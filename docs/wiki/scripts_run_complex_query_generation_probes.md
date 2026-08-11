# run_complex_query_generation_probes

Source `backend/scripts/run_complex_query_generation_probes.py` (437 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Provider-backed synthesis probes for complex-query fixture (3 queries).

Synthesis: imported library module; first docstring sentence: “Provider-backed synthesis probes for complex-query fixture (3 queries).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 303 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `json`, `os`, `statistics`, `sys`, `time`, `datetime`, `pathlib`, `typing`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `users`
- **Env vars** (name → default): `CQ_API`→`http://localhost:8000`, `CQ_AUTH_USERNAME`→`Sambenja`, `CQ_OUT`→`/tmp/complex_query_generation`, `CQ_SYNTHESIS_POOL_ENTRY`→`provider-readiness-longcat-1`, `GSEM_FIXTURE_CORPUS_ID`→`gsem-e2e-20260804a`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/COMPLEX_QUERY_PRODUCTION_GAP_GRAPHIFY_20260805.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 437 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (437 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
