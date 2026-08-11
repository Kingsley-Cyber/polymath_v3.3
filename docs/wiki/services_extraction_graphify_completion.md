# graphify_completion

Source `backend/services/extraction/graphify_completion.py` (184 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Document-local exact mention completion with ambiguity guards.

Synthesis: imported library module; first docstring sentence: “Document-local exact mention completion with ambiguity guards.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `complete_document_mentions` | 77 | `document: NormalizedDocumentV1, entities: Sequence[DocumentEntityV1], raw_mentions: Sequence[RawMentionV1]=()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `dataclasses`, `typing`, `spacy`, `spacy`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/replay_downstream.py`, `backend/scripts/run_graphify_completion.py`, `backend/services/extraction/graphify_pipeline.py`
- **Tests**: `backend/tests/extraction/test_graphify_completion.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_completion.py`
- Size 184 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
