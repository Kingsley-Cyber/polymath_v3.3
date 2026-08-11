# run_two_lane_zero_provider_diagnosis

Source `backend/scripts/run_two_lane_zero_provider_diagnosis.py` (560 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Diagnose Agent-T determinism without reaching a provider call.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_two_lane_zero_provider_diagnosis.py`), not a runtime service; first docstring sentence: “Diagnose Agent-T determinism without reaching a provider call.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--baseline`, `--output`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `diagnose` | 393 | `packet: dict[str, Any]` |
| `run` | 524 | `args: argparse.Namespace` |
| `main` | 550 | `argv: Sequence[str] \| None=None` |
| `Runtime.close` | 241 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `json`, `types`, `datetime`, `hashlib`, `pathlib`, `typing`, `bson`, `config`, `models`, `motor`, `neo4j`, `qdrant_client`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `scripts`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_two_lane_zero_provider_diagnosis.py`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/COORDINATION.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_two_lane_zero_provider_diagnosis.py`
- Size 560 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (560 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
