# regen_relex_benchmark_batched

Source `backend/scripts/regen_relex_benchmark_batched.py` (374 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Regenerate Relex Large predictions with BATCHED MPS inference.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/regen_relex_benchmark_batched.py`), not a runtime service; first docstring sentence: “Regenerate Relex Large predictions with BATCHED MPS inference.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--device`, `--batch-size`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `load_gold_texts` | 78 | `()` |
| `run_model` | 228 | `gold_samples, device, batch_size: int` |
| `verify_invariants` | 270 | `rows` |
| `write_artifact` | 301 | `rows, out_path: Path, device, batch_size: int` |
| `main` | 333 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `hashlib`, `json`, `os`, `platform`, `sys`, `time`, `pathlib`, `typing`, `torch`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/MPS_BATCH_QUALIFICATION_20260805.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 374 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
