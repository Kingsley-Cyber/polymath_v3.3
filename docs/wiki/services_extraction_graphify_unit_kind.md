# graphify_unit_kind

Source `backend/services/extraction/graphify_unit_kind.py` (197 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> unit.kind representation router — the factory's first station.

Synthesis: imported library module; first docstring sentence: “unit.kind representation router — the factory's first station.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `classify_block` | 86 | `block: SurveyBlockV1, definition_spans: Sequence[tuple[int, int]]` |
| `classify_document_blocks` | 155 | `document: NormalizedDocumentV1, survey: DocumentSurveyV1` |
| `kind_span_index` | 163 | `classified: Sequence[ClassifiedBlock]` |
| `kind_for_span` | 167 | `index: Sequence[tuple[int, int, str]], start: int, end: int` |
| `semantic_segments` | 177 | `classified: Sequence[ClassifiedBlock], text_length: int` |
| `ClassifiedBlock.as_dict` | 67 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `dataclasses`, `typing`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/services/extraction/graphify_census.py`, `backend/services/extraction/graphify_relations.py`
- **Tests**: `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_unit_kind.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_unit_kind.py`
- Size 197 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
