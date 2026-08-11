# corroboration_gate

Source `backend/services/extraction/corroboration_gate.py` (999 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic corroboration gate between Relex raw scores and syntax evidence.

Synthesis: imported library module; first docstring sentence: “Deterministic corroboration gate between Relex raw scores and syntax evidence.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `load_policy` | 359 | `config_path: str \| Path \| None=None` |
| `reset_policy_cache` | 377 | `()` |
| `evaluate_relation` | 445 | `evidence: RelationEvidence, policy: RelationPolicy` |
| `PredicatePolicy.type_compatibility` | 232 | `self, head_type: str, tail_type: str` |
| `PredicatePolicy.allows_types` | 270 | `self, head_type: str, tail_type: str` |
| `RelationPolicy.predicates` | 352 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `math`, `dataclasses`, `pathlib`, `typing`, `yaml`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/gate_diagnostic.py`, `backend/scripts/p1b_precision_audit.py`, `backend/scripts/run_gold_entity_syntax_ceiling.py`, `backend/scripts/shadow_mode_corroboration.py`, `backend/scripts/unified_shadow_pipeline.py`, `backend/services/extraction/graphify_relations.py`
- **Tests**: `backend/tests/extraction/test_corroboration_gate.py`, `backend/tests/extraction/test_relex_adapter.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_corroboration_gate.py`, `backend/tests/extraction/test_relex_adapter.py`
- Size 999 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (999 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
