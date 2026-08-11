# graphify_argument_adapter

Source `backend/services/extraction/graphify_argument_adapter.py` (424 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic entity-linking ladder for raw OpenIE arguments.

Synthesis: imported library module; first docstring sentence: “Deterministic entity-linking ladder for raw OpenIE arguments.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `adapt_openie_arguments` | 375 | `propositions: Sequence[OpenIERawPropositionV1], mentions: Sequence[CompletedMentionV1], entities: Sequence[DocumentEn...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `bisect`, `collections`, `dataclasses`, `functools`, `typing`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/replay_downstream.py`, `backend/scripts/run_graphify_argument_adapter.py`, `backend/services/extraction/graphify_pipeline.py`
- **Tests**: `backend/tests/extraction/test_graphify_argument_adapter.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_argument_adapter.py`
- Size 424 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (424 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
