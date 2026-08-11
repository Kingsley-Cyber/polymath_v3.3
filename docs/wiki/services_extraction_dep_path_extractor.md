# dep_path_extractor

Source `backend/services/extraction/dep_path_extractor.py` (1545 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Core dependency-path relation extractor.

Synthesis: imported library module; first docstring sentence: “Core dependency-path relation extractor.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `new_counters` | 236 | `()` |
| `pair_allowed` | 441 | `predicate: str, subject_type: str, object_type: str` |
| `resolve_predicate` | 694 | `signature: str, lemma: str, subject_type: str, object_type: str, pred_tok: Token \| None=None, object_tok: Token \| N...` |
| `ExtractedTriple.is_graph_edge` | 104 | `self` |
| `DepPathExtractor.extract` | 1239 | `self, text: str, entities: list[EntitySpan], *, section_path: str='', chunk_id: str='', doc_id: s...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `os`, `collections`, `dataclasses`, `pathlib`, `typing`, `spacy`, `yaml`, `spacy`, `spacy`
- **Imports OUT** (repo-wide): `backend/scripts/adjudicate_d_e_candidates.py`, `backend/scripts/gate_v2_sample.py`, `backend/scripts/recall_score.py`, `backend/scripts/rpre_measure.py`, `backend/scripts/rpre_sample_relations.py`, `backend/scripts/shadow_mode_corroboration.py`, `backend/scripts/unified_shadow_pipeline.py`, `backend/services/extraction/frame_extractor.py`, `backend/services/extraction/graphify_relations.py`, `backend/services/extraction/relex_gate.py` …
- **Tests**: `backend/tests/extraction/test_conjunction_adversarial.py`, `backend/tests/extraction/test_open_relation_fixtures.py`, `backend/tests/extraction/test_verb_prep_fixtures.py`, `backend/tests/test_dep_path_extractor.py`, `backend/tests/test_frame_extractor.py`, `backend/tests/test_relation_stage_trace.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_conjunction_adversarial.py`, `backend/tests/extraction/test_open_relation_fixtures.py`, `backend/tests/extraction/test_verb_prep_fixtures.py`, `backend/tests/test_dep_path_extractor.py`, `backend/tests/test_frame_extractor.py`, `backend/tests/test_relation_stage_trace.py`
- Size 1545 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1545 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “11efeac Fix latent contain-class direction inversion + participial subject position guard”. Full list: `git log --all --oneline -- backend/services/extraction/dep_path_extractor.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
