# frame_extractor

Source `backend/services/extraction/frame_extractor.py` (1292 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Frame-licensed relation extraction — the rebuilt pairing model.

Synthesis: imported library module; first docstring sentence: “Frame-licensed relation extraction — the rebuilt pairing model.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `FrameExtractor.extract` | 869 | `self, text: str, entities: list[EntitySpan], *, section_path: str='', chunk_id: str='', doc_id: s...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `dataclasses`, `spacy`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/adjudicate_d_e_candidates.py`, `backend/scripts/gate_diagnostic.py`, `backend/scripts/p1b_precision_audit.py`, `backend/scripts/relation_stage_trace.py`, `backend/scripts/run_gold_entity_syntax_ceiling.py`, `backend/scripts/run_openie_relation_kill_switch.py`, `backend/scripts/unified_shadow_pipeline.py`, `backend/services/extraction/graphify_relations.py`, `backend/services/extraction/relex_gate.py`, `backend/services/extraction/spacy_relation_adapter.py` …
- **Tests**: `backend/tests/extraction/test_conjunction_adversarial.py`, `backend/tests/extraction/test_open_relation_fixtures.py`, `backend/tests/extraction/test_verb_prep_fixtures.py`, `backend/tests/test_frame_extractor.py`, `backend/tests/test_relation_stage_trace.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_conjunction_adversarial.py`, `backend/tests/extraction/test_open_relation_fixtures.py`, `backend/tests/extraction/test_verb_prep_fixtures.py`, `backend/tests/test_frame_extractor.py`, `backend/tests/test_relation_stage_trace.py`
- Size 1292 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1292 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
