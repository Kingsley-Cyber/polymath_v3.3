# run_eval_with_embedder_preflight

Source `backend/scripts/run_eval_with_embedder_preflight.py` (80 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run an eval command only after the backend warms its MLX client pool.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_eval_with_embedder_preflight.py`), not a runtime service; first docstring sentence: “Run an eval command only after the backend warms its MLX client pool.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--preflight-url`, `--timeout-seconds`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `probe_embedder` | 18 | `url: str, timeout_seconds: float` |
| `run_after_preflight` | 35 | `command: Sequence[str], *, preflight_url: str, timeout_seconds: float` |
| `main` | 62 | `argv: Sequence[str] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `subprocess`, `sys`, `urllib`, `urllib`, `collections`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_embedder_eval_stability.py`

## 4. Linked scripts & configs

- Referenced by docs: `docs/MLX_EMBEDDER_STABILITY_SOAK_RECEIPT_2026-07-17.md`, `docs/TEMPORAL_QUERY_ROUTING_FROZEN_REGRESSION_2026-07-17.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_embedder_eval_stability.py`
- Size 80 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
