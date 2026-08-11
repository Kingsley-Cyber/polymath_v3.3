# adjudicate_d_e_candidates

Source `backend/scripts/adjudicate_d_e_candidates.py` (731 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Adjudicate every production-eligible candidate introduced by D and E.

Synthesis: imported library module; first docstring sentence: “Adjudicate every production-eligible candidate introduced by D and E.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run_frame_extraction` | 437 | `text: str, entities: list[dict], *, disabled_feature_groups: frozenset[str]=frozenset()` |
| `run_credit_extraction` | 471 | `text: str, entities: list[dict]` |
| `run_profile_c` | 496 | `text: str, entities: list[dict]` |
| `run_profile_d` | 504 | `text: str, entities: list[dict]` |
| `run_profile_e` | 514 | `text: str, entities: list[dict]` |
| `candidate_key` | 521 | `c: Candidate` |
| `compute_delta` | 526 | `baseline: list[Candidate], treatment: list[Candidate]` |
| `main` | 539 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `json`, `sys`, `time`, `dataclasses`, `pathlib`, `services`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/EXTRACTION_FREEZE_MANIFEST_20260802.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 731 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (731 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
