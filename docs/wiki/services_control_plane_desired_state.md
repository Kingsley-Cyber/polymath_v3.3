# desired_state

Source `backend/services/control_plane/desired_state.py` (455 lines) · subsystem [control-plane](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Desired-vs-observed artifact census. Exact ID joins, never counts.

Synthesis: imported library module; first docstring sentence: “Desired-vs-observed artifact census. Exact ID joins, never counts.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `compile_document_contract` | 84 | `doc: dict[str, Any], corpus: dict[str, Any] \| None` |
| `collect_doc_artifact_census` | 209 | `db: Any, qdrant_client: Any, *, corpus_id: str, doc_id: str, doc: dict[str, Any] \| None=None, corpus: dict[str, Any]...` |
| `active_document_ids` | 430 | `db: Any, *, corpus_id: str, limit: int \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `typing`, `models`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/control_plane/certificate.py`, `backend/services/control_plane/ledger.py`, `backend/services/control_plane/reconciler.py`
- **Tests**: `backend/tests/test_control_plane_v2.py`, `backend/tests/test_q9_parse_policy.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `ghost_b_extractions`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_control_plane_v2.py`, `backend/tests/test_q9_parse_policy.py`
- Size 455 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (455 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
