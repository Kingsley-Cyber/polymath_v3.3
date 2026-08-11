# semantic_parent_eligibility

Source `backend/services/ingestion/semantic_parent_eligibility.py` (155 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Versioned, content-neutral semantic-parent eligibility.

Synthesis: imported library module; first docstring sentence: “Versioned, content-neutral semantic-parent eligibility.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `load_parent_eligibility_recipe` | 108 | `()` |
| `parent_eligibility_recipe_hash` | 121 | `()` |
| `classify_parent_text_v2` | 125 | `text: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `functools`, `json`, `pathlib`, `re`, `unicodedata`, `typing`, `models`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/audit_semantic_parent_eligibility_mark.py`, `backend/scripts/materialize_semantic_digest_claim_inputs.py`, `backend/scripts/semantic_gateway_mark_prose_phase2.py`, `backend/services/ingestion/semantic_digest_claim_inputs.py`
- **Tests**: `backend/tests/test_semantic_parent_eligibility.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_semantic_parent_eligibility.py`
- Size 155 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
