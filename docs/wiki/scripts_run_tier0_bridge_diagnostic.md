# run_tier0_bridge_diagnostic

Source `backend/scripts/run_tier0_bridge_diagnostic.py` (499 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the immutable six-query Tier-0 bridge diagnostic.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_tier0_bridge_diagnostic.py`), not a runtime service; first docstring sentence: “Run the immutable six-query Tier-0 bridge diagnostic.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--prereg`, `--selection`, `--output`, `--arm`, `--base`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `require` | 49 | `condition: bool, message: str` |
| `rank_routed_documents` | 228 | `routes_by_lane: dict[str, list[dict[str, Any]]], *, document_names: dict[str, str] \| None=None` |
| `main` | 358 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `hashlib`, `json`, `os`, `time`, `urllib`, `urllib`, `datetime`, `pathlib`, `typing`, `bson`, `config`, `pymongo`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_tier0_bridge_diagnostic_runner.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_tier0_bridge_diagnostic_runner.py`
- Size 499 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- `require` body is verbatim-identical to `backend/scripts/run_final_acceptance_v1.py`, `backend/scripts/run_temporal_canonical_window.py`, `backend/scripts/run_two_lane_canonical_window.py`, `backend/scripts/run_waterfall_pressure_diagnostic.py` (AST dump hash match) — dedup candidate.
- Large module (499 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
