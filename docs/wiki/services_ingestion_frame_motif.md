# frame_motif

Source `backend/services/ingestion/frame_motif.py` (504 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Side-effect-free T9.2 frame binding and strict motif matching.

Synthesis: imported library module; first docstring sentence: “Side-effect-free T9.2 frame binding and strict motif matching.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `compile_frame_instance` | 57 | `claim: ClaimRecordV1, rule_match: SuperframeRuleMatchV1, *, thread_keys_by_filler_ref: Mapping[str, str], frame_role_...` |
| `match_motifs` | 246 | `*, target_artifact_id: str, frame_instances: Iterable[FrameInstanceCandidateV1], sequence_items: Iterable[FrameSequen...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `collections`, `typing`, `models`, `models`, `models`, `models`, `models`
- **Imports OUT** (repo-wide): `backend/evals/frame_motif_t9_2_census.py`, `backend/services/ingestion/document_semantic_profile.py`
- **Tests**: `backend/tests/test_frame_motif.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_frame_motif.py`
- Size 504 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (504 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
