# canary_graph_release_gate_probe

Source `backend/scripts/canary_graph_release_gate_probe.py` (698 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> 1C canary enforcement probe for the canonical graph-write release gate.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/canary_graph_release_gate_probe.py`), not a runtime service; first docstring sentence: “1C canary enforcement probe for the canonical graph-write release gate.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--report-path`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `passing_pin` | 78 | `()` |
| `write_registry` | 99 | `directory: Path, *, active_pin: dict \| None` |
| `main` | 669 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `importlib`, `json`, `os`, `subprocess`, `sys`, `tempfile`, `uuid`, `datetime`, `pathlib`, `typing`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `documents`, `graph_promotion_jobs`, `ingest_repair_runs`
- **Env vars** (no default captured): `CANARY_REPORT_PATH`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/TEMPORAL_CONTRACT_V1.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 698 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (698 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
