# gliner2_cpu_provider

Source `backend/services/extraction/gliner2_cpu_provider.py` (350 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Sole warm entity provider for the canonical Graphify extraction path.

Synthesis: imported library module; first docstring sentence: “Sole warm entity provider for the canonical Graphify extraction path.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `register_runtime_adapter` | 99 | `name: str, labels: dict[str, str], facets: dict[str, dict]` |
| `schema_descriptions` | 116 | `adapters: tuple[str, ...]=()` |
| `schema_hash` | 126 | `adapters: tuple[str, ...]=()` |
| `description_hash` | 162 | `()` |
| `provider_release_hash` | 167 | `()` |
| `get_gliner2_cpu_provider` | 334 | `()` |
| `GLiNER2CPUProvider.load_count` | 255 | `self` |
| `GLiNER2CPUProvider.health` | 258 | `self` |
| `GLiNER2CPUProvider.predict_entities` | 278 | `self, texts: Sequence[str], *, batch_size: int=DEFAULT_BATCH_SIZE, threshold: float=DEFAULT_THRES...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `hashlib`, `importlib`, `json`, `threading`, `dataclasses`, `pathlib`, `yaml`, `typing`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/ab_gliner2_device.py`, `backend/scripts/run_corpus_factory.py`, `backend/scripts/run_graphify_fixture_e2e.py`, `backend/services/extraction/entity_encoder.py`, `backend/services/extraction/graphify_census.py`, `backend/services/extraction/graphify_pipeline.py`, `backend/services/extraction/graphify_provider_registry.py`, `backend/services/ingestion/route_readiness.py`, `backend/services/ontology_adapter/providers/relex.py`
- **Tests**: `backend/tests/extraction/test_gliner2_cpu_provider.py`, `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_pipeline.py`, `backend/tests/extraction/test_graphify_reducer.py`, `backend/tests/extraction/test_ontology_adapter.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_gliner2_cpu_provider.py`, `backend/tests/extraction/test_graphify_census.py`, `backend/tests/extraction/test_graphify_pipeline.py`, `backend/tests/extraction/test_graphify_reducer.py`, `backend/tests/extraction/test_ontology_adapter.py`
- Size 350 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
