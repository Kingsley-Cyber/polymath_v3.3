# run_q9_quality_use_suite

Source `backend/scripts/run_q9_quality_use_suite.py` (389 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> 15 real q9 questions — paired baseline vs candidate (DeepSeek Flash).

Synthesis: imported library module; first docstring sentence: “15 real q9 questions — paired baseline vs candidate (DeepSeek Flash).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 159 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `json`, `os`, `time`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `CQ_SYNTHESIS_POOL_ENTRY`→`pool:deepseek-api__deepseek-v4-flash`, `Q9_QUALITY_OUT`→`/tmp/q9_quality_use`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 389 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
