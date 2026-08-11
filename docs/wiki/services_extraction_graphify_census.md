# graphify_census

Source `backend/services/extraction/graphify_census.py` (619 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Structure-aware, corpus-batched entity census with mention conservation.

Synthesis: imported library module; first docstring sentence: “Structure-aware, corpus-batched entity census with mention conservation.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_census_windows` | 133 | `document: NormalizedDocumentV1, survey: DocumentSurveyV1, *, min_tokens: int=MIN_WINDOW_TOKENS, target_tokens: int=TA...` |
| `select_schema_adapters` | 303 | `document: NormalizedDocumentV1, survey: DocumentSurveyV1` |
| `run_entity_census` | 350 | `documents: Sequence[NormalizedDocumentV1], surveys: Sequence[DocumentSurveyV1], provider: GLiNER2CPUProvider, sink: R...` |
| `RawMentionSink.persist` | 54 | `self, mentions: Sequence[RawMentionV1]` |
| `InMemoryRawMentionSink.persist` | 61 | `self, mentions: Sequence[RawMentionV1]` |
| `JsonlRawMentionSink.persist` | 85 | `self, mentions: Sequence[RawMentionV1]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `os`, `re`, `time`, `dataclasses`, `pathlib`, `typing`, `models`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/ab_gliner2_device.py`, `backend/scripts/bench_entity_encoders.py`, `backend/scripts/run_graphify_census.py`, `backend/services/extraction/graphify_pipeline.py`
- **Tests**: `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_reducer.py`, `backend/tests/extraction/test_graphify_unit_kind.py`
- **Env vars** (name → default): `GRAPHIFY_FORCED_ADAPTERS`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_reducer.py`, `backend/tests/extraction/test_graphify_unit_kind.py`
- Size 619 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (619 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “0fbd2d0 Saturation closeout: Pareto pin confirmed, residual ledger (UNKNOWN=0), title miner”. Full list: `git log --all --oneline -- backend/services/extraction/graphify_census.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
