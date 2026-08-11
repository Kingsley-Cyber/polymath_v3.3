# graphify_predicate_compiler

Source `backend/services/extraction/graphify_predicate_compiler.py` (220 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Compile reduced OpenIE surface relations without inventing ontology edges.

Synthesis: imported library module; first docstring sentence: “Compile reduced OpenIE surface relations without inventing ontology edges.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `compile_openie_predicates` | 87 | `families: Sequence[OpenIEPropositionFamilyV1], propositions: Sequence[OpenIERawPropositionV1], arguments: Sequence[Ad...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `collections`, `dataclasses`, `typing`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/replay_downstream.py`, `backend/scripts/run_graphify_predicate_compiler.py`, `backend/services/extraction/graphify_pipeline.py`
- **Tests**: `backend/tests/extraction/test_graphify_predicate_compiler.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_predicate_compiler.py`
- Size 220 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “710511a fix(graphify): guard empty lemma-candidates in OpenIE canonical hint”. Full list: `git log --all --oneline -- backend/services/extraction/graphify_predicate_compiler.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
