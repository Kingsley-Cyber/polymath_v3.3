# appos_enrichment

Source `backend/services/extraction/appos_enrichment.py` (481 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> spaCy appositional classification for the deterministic alias pipeline.

Synthesis: imported library module; first docstring sentence: “spaCy appositional classification for the deterministic alias pipeline.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `get_shared_nlp` | 90 | `()` |
| `classify_apposition_phrase` | 167 | `appos_text: str, *, sentence_text: str='', left_context: str=''` |
| `extract_apposition_observations` | 252 | `text: str, entities: list[dict], *, doc: Any=None` |
| `spacy_appos_enrichment` | 446 | `text: str, entities: list[dict], *, doc: Any=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `os`, `re`, `dataclasses`, `typing`
- **Imports OUT** (repo-wide): `backend/services/extraction/dep_path_extractor.py`, `backend/services/extraction/frame_extractor.py`, `backend/services/extraction/mention_normalizer.py`, `backend/services/ingestion/alias_candidates.py`
- **Tests**: `backend/tests/test_alias_apposition_phase3.py`
- **Env vars** (name → default): `SPACY_MODEL`→`en_core_web_sm`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_apposition_phase3.py`
- Size 481 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (481 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
