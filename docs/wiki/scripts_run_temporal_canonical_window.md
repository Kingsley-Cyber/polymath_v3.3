# run_temporal_canonical_window

Source `backend/scripts/run_temporal_canonical_window.py` (599 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the owner-bounded canonical temporal activation window.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_temporal_canonical_window.py`), not a runtime service; first docstring sentence: “Run the owner-bounded canonical temporal activation window.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--output`, `--api`, `--request-timeout`, `--lock-owner`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `require` | 139 | `condition: bool, message: str` |
| `select_cases` | 144 | `prereg: dict[str, Any], negative: dict[str, Any]` |
| `score_temporal_anchors` | 184 | `case: dict[str, Any], raw_sources: Sequence[dict[str, Any]], document_names: dict[str, str]` |
| `summarize` | 312 | `executions: Sequence[dict[str, Any]], baseline_states: dict[str, str]` |
| `run` | 399 | `args: argparse.Namespace` |
| `main` | 585 | `argv: Sequence[str] \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `concurrent`, `json`, `os`, `re`, `sys`, `urllib`, `uuid`, `collections`, `pathlib`, `typing`, `config`, `scripts`, `scripts`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_temporal_canonical_window.py`
- **Env vars** (no default captured): `POLYMATH_EVAL_TOKEN`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_temporal_canonical_window.py`
- Size 599 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- `require` body is verbatim-identical to `backend/scripts/run_final_acceptance_v1.py`, `backend/scripts/run_tier0_bridge_diagnostic.py`, `backend/scripts/run_two_lane_canonical_window.py`, `backend/scripts/run_waterfall_pressure_diagnostic.py` (AST dump hash match) — dedup candidate.
- Large module (599 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
