# entity_quality

Source `backend/services/extraction/entity_quality.py` (574 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic entity quality gate.

Synthesis: imported library module; first docstring sentence: “Deterministic entity quality gate.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `new_reject_counters` | 256 | `()` |
| `judge_entity` | 265 | `surface: str, entity_type: str, *, confidence: float=1.0, min_confidence: float=0.6, head_pos: str \| None=None, corp...` |
| `filter_entities` | 393 | `entities: list[dict], *, doc_frequency: dict[str, float] \| None=None, min_confidence: float=0.6, max_doc_frequency: ...` |
| `annotate_entities` | 431 | `entities: list[dict], *, doc_frequency: dict[str, float] \| None=None, min_confidence: float=0.6, max_doc_frequency: ...` |
| `eligible_only` | 475 | `entities: list[dict]` |
| `judge_relation_anchor` | 519 | `surface: str, entity_type: str, *, confidence: float=1.0, counters: dict[str, int] \| None=None` |
| `annotate_entities_two_tier` | 546 | `entities: list[dict], *, doc_frequency: dict[str, float] \| None=None, min_confidence: float=0.6, max_doc_frequency: ...` |
| `relation_anchors` | 568 | `entities: list[dict]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `dataclasses`, `typing`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/annotate_entity_quality.py`, `backend/scripts/recall_score.py`, `backend/scripts/relation_stage_trace.py`, `backend/services/extraction/graphify_reducer.py`, `backend/services/extraction/relex_gate.py`, `backend/services/extraction/spacy_relation_adapter.py`
- **Tests**: `backend/tests/test_entity_quality_gate.py`, `backend/tests/test_extraction_vocabulary_v2.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_entity_quality_gate.py`, `backend/tests/test_extraction_vocabulary_v2.py`
- Size 574 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (574 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
