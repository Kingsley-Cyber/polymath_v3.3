# p0_5_facet_decontamination

Source `backend/scripts/p0_5_facet_decontamination.py` (411 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> P0.5 facet decontamination: measure and strip corpus-lens-inherited facet ids that lack per-row content evidence (dry-run by default).

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/p0_5_facet_decontamination.py`), not a runtime service; first docstring sentence: “P0.5 facet decontamination: measure and strip corpus-lens-inherited facet ids that lack per-row content evidence (dry-run by default).”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-id`, `--apply`, `--top`, `--batch-size`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `classify_facets` | 73 | `row: dict, lens_facet_ids: set \| None=None` |
| `audit_corpus` | 154 | `db, corpus: dict, *, top_n: int` |
| `main` | 321 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `sys`, `time`, `collections`, `pathlib`, `typing`, `urllib`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/EXECUTION_PLAN_2026-07-13.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 411 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (411 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
