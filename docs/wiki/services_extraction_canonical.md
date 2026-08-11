# canonical

Source `backend/services/extraction/canonical.py` (697 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Canonical entity and predicate representations — single source of truth.

Synthesis: imported library module; first docstring sentence: “Canonical entity and predicate representations — single source of truth.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `normalize_entity_name` | 153 | `name: str` |
| `singularize_token` | 190 | `word: str` |
| `name_core_span` | 228 | `text: str` |
| `endpoint_mint_policy` | 246 | `text: str` |
| `resolve_entity_alias` | 305 | `normalized_name: str` |
| `canonicalize_entity_name` | 310 | `name: str` |
| `get_entity_type_overrides` | 340 | `()` |
| `validate_alias_map` | 353 | `raw_map: dict[str, list[str]] \| None=None, *, raise_on_error: bool=False` |
| `entity_id_from_name` | 437 | `canonical_name: str, entity_type: str \| None=None` |
| `entity_id_with_type` | 455 | `canonical_name: str, entity_type: str \| None` |
| `detect_entity_id_collision` | 478 | `name_a: str, type_a: str \| None, name_b: str, type_b: str \| None` |
| `is_generic_entity_name` | 491 | `canonical_name: str` |
| `normalize_entity_type` | 549 | `raw: str` |
| `canonical_entity_type` | 573 | `raw: str` |
| `is_valid_entity_type` | 603 | `entity_type: str` |
| `get_predicate_label_mapping` | 667 | `()` |
| `canonicalize_predicate_label` | 679 | `label: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `logging`, `re`, `unicodedata`, `functools`, `pathlib`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/unified_shadow_pipeline.py`, `backend/services/extraction/entity_encoder.py`, `backend/services/extraction/entity_quality.py`, `backend/services/extraction/gliner2_cpu_provider.py`, `backend/services/extraction/graphify_pipeline.py`, `backend/services/extraction/graphify_reducer.py`, `backend/services/extraction/graphify_relations.py`, `backend/services/extraction/relation_evidence.py`, `backend/services/extraction/relex_adapter.py`, `backend/services/extraction/relex_gate.py` …
- **Tests**: `backend/tests/extraction/test_canonical_contract.py`, `backend/tests/extraction/test_graphify_contracts_offsets_survey.py`, `backend/tests/test_alias_candidates_phase2.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_canonical_contract.py`, `backend/tests/extraction/test_graphify_contracts_offsets_survey.py`, `backend/tests/test_alias_candidates_phase2.py`
- Size 697 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (697 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
