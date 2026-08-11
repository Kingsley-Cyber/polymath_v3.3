# reconciler

Source `backend/services/control_plane/reconciler.py` (759 lines) · subsystem [control-plane](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Artifact-driven reconciler — replaces the queue-count scheduling gate.

Synthesis: imported library module; first docstring sentence: “Artifact-driven reconciler — replaces the queue-count scheduling gate.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `reconcile_corpus` | 343 | `db: Any, *, ingestion_service: Any, corpus_id: str, user_id: str, doc_census_limit: int \| None=None, execute: bool=True` |
| `corpus_has_actionable_work` | 615 | `db: Any, *, corpus_id: str` |
| `run_reconcile_tick` | 648 | `db: Any, *, ingestion_service: Any, corpus_limit: int \| None=None, _corpus_id: str \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `os`, `time`, `datetime`, `typing`, `config`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/main.py`, `backend/routers/control_plane.py`
- **Tests**: `backend/tests/test_control_plane_v2.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `ingest_batch_items`
- **Env vars** (name → default): `CONTROL_PLANE_V2_EXECUTE`→`false`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_control_plane_v2.py`
- Size 759 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (759 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “2. `backend/services/control_plane/reconciler.py`”, “4. Chain-argument table for one hypothetical tick”, “6. Per-collection write amplification for one document ingest” — link into the map, do not duplicate it.
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
