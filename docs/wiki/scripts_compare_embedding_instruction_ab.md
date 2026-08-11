# compare_embedding_instruction_ab

Source `backend/scripts/compare_embedding_instruction_ab.py` (194 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Assert the preregistered T5.6 query-instruction promotion gates.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/compare_embedding_instruction_ab.py`), not a runtime service; first docstring sentence: “Assert the preregistered T5.6 query-instruction promotion gates.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--baseline`, `--candidate`, `--out`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `compare_pair` | 63 | `baseline_path: Path, candidate_path: Path` |
| `main` | 166 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `pathlib`, `typing`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_embedding_instruction_ab.py`

## 4. Linked scripts & configs

- Referenced by docs: `docs/baselines/T56_QWEN3_UNIVERSAL_AB_2026-07-14.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_embedding_instruction_ab.py`
- Size 194 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
