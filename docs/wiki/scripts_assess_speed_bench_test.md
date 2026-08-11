# assess_speed_bench_test

Source `backend/scripts/assess_speed_bench_test.py` (364 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Assess speed_bench_test_20260805: extraction speed + ontology + summary quality.

Synthesis: imported library module; first docstring sentence: “Assess speed_bench_test_20260805: extraction speed + ontology + summary quality.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `score_summary` | 102 | `text: str, parent_text: str` |
| `main` | 135 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `json`, `os`, `re`, `collections`, `datetime`, `pathlib`, `typing`, `motor`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `SPEED_BENCH_BATCH`→`b0d12dba-11c5-4303-b3c4-ec2bad23b188`, `SPEED_BENCH_CORPUS`→`0189427c-6ccf-49e2-82cd-aedea87f4045`, `SPEED_BENCH_OUT`→`/Users/king/polymath_v3.3/data_eval/speed_bench_test_20260805`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/SPEED_BENCH_CLOSEOUT_AND_PERFORMANCE_PLAN_20260805.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 364 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
