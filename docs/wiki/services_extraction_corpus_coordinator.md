# corpus_coordinator

Source `backend/services/extraction/corpus_coordinator.py` (147 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> CorpusCoordinator — factory execution plane, station B.

Synthesis: imported library module; first docstring sentence: “CorpusCoordinator — factory execution plane, station B.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `default_max_active_documents` | 41 | `()` |
| `run_corpus_factory` | 65 | `*, db, corpus_id: str, documents: Sequence[CorpusDocument], provider, max_active: int \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `logging`, `os`, `time`, `dataclasses`, `typing`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/run_corpus_factory.py`
- **Tests**: `backend/tests/extraction/test_corpus_coordinator.py`
- **Env vars** (name → default): `GRAPHIFY_MAX_ACTIVE_DOCS`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_corpus_coordinator.py`
- Size 147 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
