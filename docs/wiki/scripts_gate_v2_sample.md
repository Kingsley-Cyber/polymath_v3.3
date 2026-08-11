# gate_v2_sample

Source `backend/scripts/gate_v2_sample.py` (182 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Gate v2 — build the judging worksheet, then score it.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/gate_v2_sample.py`), not a runtime service; first docstring sentence: “Gate v2 — build the judging worksheet, then score it.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--in`, `--out`, `--per-stratum`, `--worksheet`, `--out`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `wilson` | 34 | `successes: int, n: int, z: float=1.96` |
| `do_sample` | 45 | `inp: str, out: str, per_stratum: int` |
| `do_score` | 94 | `worksheet: str, out: str` |
| `main` | 163 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `math`, `sys`, `collections`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 182 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
