# alias_schema_retrieval

Source `backend/services/ingestion/alias_schema_retrieval.py` (489 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Phase-7 dual-lane schema-assisted retrieval (shadow index only).

Synthesis: imported library module; first docstring sentence: “Phase-7 dual-lane schema-assisted retrieval (shadow index only).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `match_shadow_schema` | 105 | `query: str, records: Sequence[ShadowSchemaRecordV1], *, active_document_ids: Sequence[str] \| None=None, active_paren...` |
| `expansion_for_surface` | 174 | `record: ShadowSchemaRecordV1, surface: ShadowSchemaSurfaceV1` |
| `apply_route_link_budget` | 196 | `tier: str, record: ShadowSchemaRecordV1, *, ranking_contribution: str` |
| `run_dual_lane_retrieval` | 250 | `query: str, *, tier: str, shadow_records: Sequence[ShadowSchemaRecordV1], corpus_child_index: Sequence[dict[str, Any]...` |
| `replay_retrieval_fingerprint` | 477 | `result: DualLaneRetrievalResult` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `dataclasses`, `typing`, `models`
- **Imports OUT** (repo-wide): `backend/services/ingestion/alias_retrieval_shadow.py`
- **Tests**: `backend/tests/test_alias_retrieval_shadow_phase8.py`, `backend/tests/test_alias_schema_retrieval_phase7.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_retrieval_shadow_phase8.py`, `backend/tests/test_alias_schema_retrieval_phase7.py`
- Size 489 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (489 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
