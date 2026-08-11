# mine_dependency_patterns

Source `backend/scripts/mine_dependency_patterns.py` (329 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Dependency-path pattern mining script.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/mine_dependency_patterns.py`), not a runtime service; first docstring sentence: “Dependency-path pattern mining script.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--sample`, `--out`, `--batch-size`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `find_head_token` | 48 | `doc, start_char: int, end_char: int` |
| `shortest_dep_path` | 66 | `doc, tok_a, tok_b` |
| `build_signature` | 106 | `path, doc, tok_a, tok_b` |
| `load_supported_patterns` | 149 | `()` |
| `mine_claims` | 160 | `sample_size: int, batch_size: int=500` |
| `main` | 298 | `()` |
| `SignatureAccumulator.add` | 142 | `self, sentence: str, predicate: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `logging`, `sys`, `time`, `collections`, `dataclasses`, `datetime`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ghost_b_extractions`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/DETERMINISTIC_RELATION_RECALL_SPEC.md`, `docs/archive/COORDINATION.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 329 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
