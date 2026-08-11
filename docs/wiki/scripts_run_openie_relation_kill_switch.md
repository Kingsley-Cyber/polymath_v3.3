# run_openie_relation_kill_switch

Source `backend/scripts/run_openie_relation_kill_switch.py` (605 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Compare syntax, triplet-extract, and their precision-gated union.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_openie_relation_kill_switch.py`), not a runtime service; first docstring sentence: “Compare syntax, triplet-extract, and their precision-gated union.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--fixture`, `--gold`, `--output`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `align_argument` | 58 | `argument: str, endpoints: Iterable[str]` |
| `compile_predicate` | 130 | `compiler: PredicateCompiler, *, surface: str, subject_type: str, object_type: str, subject_name: str, object_name: st...` |
| `surface_pair_candidates` | 236 | `spans: list[EndpointSpan], text: str` |
| `precision_reduce` | 273 | `*, syntax: set[tuple[str, str, str]], openie: set[tuple[str, str, str]], surface: set[tuple[str, str, str]], promoted...` |
| `run` | 330 | `fixture_path: Path, gold_path: Path, output_path: Path` |
| `main` | 592 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `hashlib`, `json`, `re`, `sys`, `time`, `collections`, `dataclasses`, `pathlib`, `typing`, `services`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 605 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (605 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
