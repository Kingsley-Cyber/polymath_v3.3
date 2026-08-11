# credit_patterns

Source `backend/services/extraction/credit_patterns.py` (363 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Credit and metadata fragment patterns (P2B construction family).

Synthesis: imported library module; first docstring sentence: “Credit and metadata fragment patterns (P2B construction family).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `extract_credit_patterns` | 87 | `text: str, entities: list[dict[str, Any]], chunk_id: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `typing`
- **Imports OUT** (repo-wide): `backend/scripts/adjudicate_d_e_candidates.py`, `backend/scripts/unified_shadow_pipeline.py`, `backend/services/extraction/syntax_lane.py`
- **Tests**: `backend/tests/extraction/test_credit_pattern_fixtures.py`, `backend/tests/extraction/test_credit_patterns.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_credit_pattern_fixtures.py`, `backend/tests/extraction/test_credit_patterns.py`
- Size 363 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
