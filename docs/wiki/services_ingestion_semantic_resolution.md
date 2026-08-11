# semantic_resolution

Source `backend/services/ingestion/semantic_resolution.py` (491 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Local deterministic T9.1 domain and predicate→superframe resolution.

Synthesis: imported library module; first docstring sentence: “Local deterministic T9.1 domain and predicate→superframe resolution.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `resolve_domains` | 82 | `*, target_artifact_id: str, signals: Iterable[DomainSignalV1], context_profile_ids: Iterable[str]=(), domain_registry...` |
| `build_domain_affinity_serve_view` | 254 | `resolution: DomainResolutionV1, *, affinity_registry: dict[str, Any] \| None=None` |
| `resolve_superframe_rule` | 365 | `claim: ClaimRecordV1, *, entity_types_by_mention_id: Mapping[str, EntityType], rule_registry: dict[str, Any] \| None=...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `collections`, `typing`, `models`, `models`, `models`, `models`, `models`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/evals/frame_motif_t9_2_census.py`, `backend/evals/semantic_resolution_t9_1_census.py`, `backend/services/ingestion/document_semantic_profile.py`, `backend/services/retriever/four_lane_router.py`
- **Tests**: `backend/tests/test_frame_motif.py`, `backend/tests/test_semantic_resolution.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_frame_motif.py`, `backend/tests/test_semantic_resolution.py`
- Size 491 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (491 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
