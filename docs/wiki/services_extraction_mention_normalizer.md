# mention_normalizer

Source `backend/services/extraction/mention_normalizer.py` (155 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic mention normalization for entity boundary equivalence.

Synthesis: imported library module; first docstring sentence: “Deterministic mention normalization for entity boundary equivalence.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `normalized_mention` | 72 | `text: str` |
| `normalized_mention_cached` | 102 | `text: str` |
| `is_morphological_variant` | 107 | `left: str, right: str` |
| `resolve_mention_pair` | 125 | `model_surface: str, model_span: tuple[int, int], gold_surface: str, gold_span: tuple[int, int]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `re`, `unicodedata`, `functools`
- **Imports OUT** (repo-wide): `backend/scripts/shadow_mode_corroboration.py`, `backend/scripts/unified_shadow_pipeline.py`, `backend/services/extraction/relex_adapter.py`, `backend/services/extraction/syntax_lane.py`
- **Tests**: `backend/tests/extraction/test_relex_adapter.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_relex_adapter.py`
- Size 155 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
