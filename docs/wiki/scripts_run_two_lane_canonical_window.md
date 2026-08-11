# run_two_lane_canonical_window

Source `backend/scripts/run_two_lane_canonical_window.py` (819 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the owner-bounded canonical Agent-T verification window.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_two_lane_canonical_window.py`), not a runtime service; first docstring sentence: “Run the owner-bounded canonical Agent-T verification window.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--output`, `--api`, `--request-timeout`, `--lock-owner`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `require` | 94 | `condition: bool, message: str` |
| `select_cases` | 103 | `prereg: dict[str, Any]` |
| `summarize` | 499 | `executions: Sequence[dict[str, Any]], repeats: Sequence[dict[str, Any]]` |
| `run` | 606 | `args: argparse.Namespace` |
| `main` | 808 | `argv: Sequence[str] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `concurrent`, `json`, `os`, `sys`, `urllib`, `uuid`, `collections`, `datetime`, `pathlib`, `typing`, `config`, `scripts`
- **Imports OUT** (repo-wide): `backend/scripts/run_temporal_canonical_window.py`, `backend/scripts/run_two_lane_zero_provider_diagnosis.py`
- **Tests**: `backend/tests/test_two_lane_canonical_window.py`
- **Env vars** (no default captured): `POLYMATH_EVAL_TOKEN`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_two_lane_canonical_window.py`
- Size 819 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- `require` body is verbatim-identical to `backend/scripts/run_final_acceptance_v1.py`, `backend/scripts/run_temporal_canonical_window.py`, `backend/scripts/run_tier0_bridge_diagnostic.py`, `backend/scripts/run_waterfall_pressure_diagnostic.py` (AST dump hash match) — dedup candidate.
- Large module (819 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 3 — e.g. “e7295d9 fix: attest T on the combined live stack”. Full list: `git log --all --oneline -- backend/scripts/run_two_lane_canonical_window.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
