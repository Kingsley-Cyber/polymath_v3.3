# alias_candidates

Source `backend/services/ingestion/alias_candidates.py` (785 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Alias candidate unification around live miners (Phases 2–3).

Synthesis: imported library module; first docstring sentence: “Alias candidate unification around live miners (Phases 2–3).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `collect_alias_candidates` | 682 | `text: str, entities: Iterable[dict[str, Any]] \| None, *, document_id: str, chunk_id: str, include_curated: bool=True...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `dataclasses`, `typing`, `models`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/alias_shadow_build.py`, `backend/services/ingestion/fixture_knowledge_pipeline.py`
- **Tests**: `backend/tests/test_alias_apposition_phase3.py`, `backend/tests/test_alias_candidates_phase2.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_apposition_phase3.py`, `backend/tests/test_alias_candidates_phase2.py`
- Size 785 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (785 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
