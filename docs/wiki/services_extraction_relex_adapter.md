# relex_adapter

Source `backend/services/extraction/relex_adapter.py` (686 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Adapter: converts Relex raw predictions + syntax candidates → RelationEvidence.

Synthesis: imported library module; first docstring sentence: “Adapter: converts Relex raw predictions + syntax candidates → RelationEvidence.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_relation_evidence` | 87 | `chunk_id: str, prediction_row: dict[str, Any], *, oracle_types: dict[tuple[int, int], str] \| None=None` |
| `join_syntax_evidence` | 259 | `evidence_list: list[RelationEvidence], syntax_records: Iterable[dict[str, Any]], *, surface_join: bool=False, enable_...` |
| `svo_to_syntax_records` | 572 | `svo_candidates: Iterable[Any], chunk_id: str, entity_spans: Sequence[dict[str, Any]], resolve_lemma: Any=None` |
| `load_predictions_jsonl` | 669 | `path: str \| Path` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `logging`, `pathlib`, `typing`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/shadow_mode_corroboration.py`, `backend/scripts/unified_shadow_pipeline.py`, `backend/services/extraction/syntax_lane.py`
- **Tests**: `backend/tests/extraction/test_relex_adapter.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_relex_adapter.py`
- Size 686 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (686 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
