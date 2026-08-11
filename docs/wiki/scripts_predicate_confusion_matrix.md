# predicate_confusion_matrix

Source `backend/scripts/predicate_confusion_matrix.py` (283 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> P3 Predicate Confusion Matrix — diagnostic tool for wrong-predicate traces.

Synthesis: imported library module; first docstring sentence: “P3 Predicate Confusion Matrix — diagnostic tool for wrong-predicate traces.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `load_results` | 43 | `path: Path` |
| `extract_confusions` | 54 | `results: list[dict]` |
| `build_matrix` | 64 | `confusions: list[dict]` |
| `analyze_cell` | 82 | `records: list[dict]` |
| `suggest_corrective` | 128 | `gold_pred: str, gate_pred: str, analysis: dict` |
| `print_report` | 185 | `matrix: dict[tuple[str, str], list[dict]], total_confusions: int` |
| `main` | 247 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `sys`, `collections`, `pathlib`, `statistics`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 283 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
