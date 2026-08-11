# label_structural_shadow_projections

Source `backend/scripts/label_structural_shadow_projections.py` (169 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Label existing structural graph projections as noncanonical shadow.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/label_structural_shadow_projections.py`), not a runtime service; first docstring sentence: “Label existing structural graph projections as noncanonical shadow.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--apply`, `--batch`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `count_class` | 118 | `session, match: str, var: str` |
| `apply_class` | 125 | `session, match: str, set_clause: str, var: str, batch: int` |
| `main` | 141 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `os`, `sys`, `time`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `NEO4J_URI`→`bolt://neo4j:7687`, `NEO4J_USER`→`neo4j`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/BOOK_INGESTION_P5_CANARY_REPORT_20260803.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 169 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
