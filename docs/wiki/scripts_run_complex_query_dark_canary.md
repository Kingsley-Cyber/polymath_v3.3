# run_complex_query_dark_canary

Source `backend/scripts/run_complex_query_dark_canary.py` (301 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Bounded dark-canary canary run — shadow metrics via /api/chat.

Synthesis: imported library module; first docstring sentence: “Bounded dark-canary canary run — shadow metrics via /api/chat.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 293 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `os`, `sys`, `time`, `datetime`, `pathlib`, `typing`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `users`
- **Env vars** (name → default): `CQ_API`→`http://localhost:8000`, `CQ_AUTH_USERNAME`→`Sambenja`, `CQ_DARK_SYNTHESIS`→`1`, `CQ_OUT`→`/tmp/complex_query_dark_canary`, `CQ_SYNTHESIS_POOL_ENTRY`→`provider-readiness-longcat-1`, `GSEM_FIXTURE_CORPUS_ID`→`gsem-e2e-20260804a`

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
