# run_canonical_heldout_negative_eval

Source `backend/scripts/run_canonical_heldout_negative_eval.py` (1120 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the immutable 28-probe held-out refusal measurement.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_canonical_heldout_negative_eval.py`), not a runtime service; first docstring sentence: “Run the immutable 28-probe held-out refusal measurement.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--expected-temporal`, `--output`, `--probe-selection`, `--concurrency`, `--api`, `--request-timeout`, `--lock-owner`, `--lock-wait-seconds`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `execution_completeness_errors` | 262 | `row: dict[str, Any]` |
| `run` | 893 | `args: argparse.Namespace` |
| `main` | 1109 | `argv: Sequence[str] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `concurrent`, `hashlib`, `json`, `os`, `sys`, `time`, `urllib`, `urllib`, `urllib`, `uuid`, `collections`, `contextlib`, `datetime`, `pathlib`, `typing`, `config`, `evals`
- **Imports OUT** (repo-wide): `backend/scripts/run_final_acceptance_v1.py`, `backend/scripts/run_temporal_canonical_window.py`, `backend/scripts/run_two_lane_canonical_window.py`
- **Tests**: `backend/tests/test_canonical_heldout_negative_eval.py`
- **Env vars** (no default captured): `POLYMATH_EVAL_TOKEN`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_canonical_heldout_negative_eval.py`
- Size 1120 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1120 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
