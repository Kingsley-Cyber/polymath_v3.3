# graphify_assertion_semantics

Source `backend/services/extraction/graphify_assertion_semantics.py` (102 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Clause-local deterministic assertion-status rules shared by extraction lanes.

Synthesis: imported library module; first docstring sentence: “Clause-local deterministic assertion-status rules shared by extraction lanes.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `nominal_assertion_qualification` | 44 | `evidence_text: str, subject: str, relation: str, obj: str` |
| `reported_attribution_source` | 93 | `evidence_text: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`
- **Imports OUT** (repo-wide): `backend/services/extraction/graphify_proposition_reducer.py`, `backend/services/extraction/graphify_relations.py`
- **Tests**: `backend/tests/extraction/test_graphify_assertion_semantics.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_assertion_semantics.py`
- Size 102 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
