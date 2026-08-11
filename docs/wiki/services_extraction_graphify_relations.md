# graphify_relations

Source `backend/services/extraction/graphify_relations.py` (2559 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Selective parse-once relation fast path and predicate compiler.

Synthesis: imported library module; first docstring sentence: “Selective parse-once relation fast path and predicate compiler.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `predicate_compiler` | 413 | `()` |
| `evaluate_relation_eligibility` | 524 | `documents: Sequence[NormalizedDocumentV1], surveys: Sequence[DocumentSurveyV1], mentions: Sequence[CompletedMentionV1]` |
| `run_relation_fast_path` | 2175 | `documents: Sequence[NormalizedDocumentV1], surveys: Sequence[DocumentSurveyV1], mentions: Sequence[CompletedMentionV1...` |
| `openie_fact_merge_disposition` | 2538 | `key: tuple[str \| None, str \| None, str \| None], span: tuple[int, int], accepted_keys: set, qualified_spans_by_key:...` |
| `RelationEligibilityDecision.as_dict` | 207 | `self` |
| `PredicateCompiler.compile` | 291 | `self, *, surface: str, lemma: str, canonical_hint: str \| None, subject_type: str, object_type: s...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `logging`, `os`, `re`, `threading`, `bisect`, `collections`, `dataclasses`, `functools`, `pathlib`, `typing`, `yaml`, `models`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/replay_downstream.py`, `backend/scripts/run_graphify_relations.py`, `backend/scripts/run_openie_relation_kill_switch.py`, `backend/scripts/run_relation_eligibility.py`, `backend/services/extraction/entity_encoder.py`, `backend/services/extraction/graphify_pipeline.py`, `backend/services/extraction/graphify_predicate_compiler.py`, `backend/services/ingestion/route_readiness.py`, `backend/services/ontology_adapter/providers/relex.py`, `backend/services/ontology_adapter/validator.py`
- **Tests**: `backend/tests/extraction/test_graphify_endpoint_eligibility.py`, `backend/tests/extraction/test_graphify_fact_merge.py`, `backend/tests/extraction/test_graphify_relations.py`, `backend/tests/extraction/test_graphify_unit_kind.py`, `backend/tests/extraction/test_graphify_value_ir.py`, `backend/tests/extraction/test_ontology_adapter.py` …
- **Env vars** (name → default): `GRAPHIFY_RELEX_RELATIONS`→``, `RELEX_ACCEPT_THRESHOLD`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_endpoint_eligibility.py`, `backend/tests/extraction/test_graphify_fact_merge.py`, `backend/tests/extraction/test_graphify_relations.py`, `backend/tests/extraction/test_graphify_unit_kind.py`, `backend/tests/extraction/test_graphify_value_ir.py`, `backend/tests/extraction/test_ontology_adapter.py`, `backend/tests/extraction/test_relation_eligibility_artifact.py`
- Size 2559 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (2559 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 5 — e.g. “46eeacb Metadata-key entity guard + prospective 'be going to' classification”. Full list: `git log --all --oneline -- backend/services/extraction/graphify_relations.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
