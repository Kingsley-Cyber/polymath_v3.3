# graphify_reducer

Source `backend/services/extraction/graphify_reducer.py` (714 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Document-local entity clustering and conservative quality adjudication.

Synthesis: imported library module; first docstring sentence: “Document-local entity clustering and conservative quality adjudication.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `reduce_document_entities` | 641 | `document: NormalizedDocumentV1, mentions: Sequence[RawMentionV1], survey: DocumentSurveyV1` |
| `MentionReductionAssignment.as_dict` | 44 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `collections`, `dataclasses`, `typing`, `models`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/replay_downstream.py`, `backend/scripts/run_graphify_reducer.py`, `backend/services/extraction/graphify_pipeline.py`, `backend/services/ingestion/route_readiness.py`
- **Tests**: `backend/tests/extraction/test_graphify_reducer.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_reducer.py`
- Size 714 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (714 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 2 — e.g. “46eeacb Metadata-key entity guard + prospective 'be going to' classification”. Full list: `git log --all --oneline -- backend/services/extraction/graphify_reducer.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
