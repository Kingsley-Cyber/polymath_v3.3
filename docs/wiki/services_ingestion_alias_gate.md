# alias_gate

Source `backend/services/ingestion/alias_gate.py` (536 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic alias gate (Alias Pipeline Phase 4).

Synthesis: imported library module; first docstring sentence: “Deterministic alias gate (Alias Pipeline Phase 4).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run_alias_gate` | 468 | `candidates: Iterable[AliasCandidateV1] \| None, *, incomplete: Iterable[IncompleteAliasCandidate] \| None=None` |
| `incomplete_never_accepted` | 522 | `batch: AliasGateBatch` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `dataclasses`, `typing`, `models`
- **Imports OUT** (repo-wide): `backend/services/ingestion/alias_shadow_build.py`, `backend/services/ingestion/fixture_knowledge_pipeline.py`
- **Tests**: `backend/tests/test_alias_document_clustering_phase5.py`, `backend/tests/test_alias_gate_phase4.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_document_clustering_phase5.py`, `backend/tests/test_alias_gate_phase4.py`
- Size 536 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (536 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
