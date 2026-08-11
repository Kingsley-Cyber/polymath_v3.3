# shadow_b_graph_release_gate_probe

Source `backend/scripts/shadow_b_graph_release_gate_probe.py` (524 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Shadow B probe for the canonical graph-write release gate.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/shadow_b_graph_release_gate_probe.py`), not a runtime service; first docstring sentence: “Shadow B probe for the canonical graph-write release gate.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--capture`, `--report-path`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `passing_pin` | 66 | `()` |
| `write_registry_fixture` | 87 | `directory: Path, *, pin_overrides: dict[str, Any] \| None` |
| `main` | 296 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `importlib`, `json`, `os`, `subprocess`, `sys`, `tempfile`, `datetime`, `pathlib`, `typing`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `documents`, `graph_promotion_jobs`, `ingest_repair_runs`
- **Env vars** (no default captured): `SHADOW_B_REPORT_PATH`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/TEMPORAL_CONTRACT_V1.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 524 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (524 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
