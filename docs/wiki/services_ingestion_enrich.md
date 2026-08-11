# enrich

Source `backend/services/ingestion/enrich.py` (717 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> enrich.py — Pass-1 deterministic enrichment (no model, bit-for-bit reproducible).

Synthesis: imported library module; first docstring sentence: “enrich.py — Pass-1 deterministic enrichment (no model, bit-for-bit reproducible).”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `norm` | 89 | `s` |
| `qualitative_cue_hits` | 117 | `text: str` |
| `should_enrich_facts` | 123 | `text: str, extracted_facts: list[dict]` |
| `schwartz_hearst_matches` | 162 | `text: str` |
| `schwartz_hearst` | 216 | `text: str` |
| `extract_aliases` | 232 | `text: str, entities: list[dict]` |
| `extract_facts` | 293 | `text: str, entities: list[dict]` |
| `extract_qualitative_facts` | 422 | `text: str, entities: list[dict]` |
| `extract_table_facts` | 558 | `text: str, columns: list[str] \| None=None, max_facts: int=24` |
| `table_entity_text` | 642 | `text: str, columns: list[str] \| None=None` |
| `extract_definitional_phrases` | 665 | `text: str, entities: list[dict]` |
| `extract` | 691 | `text: str, entities: list[dict]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `typing`
- **Imports OUT** (repo-wide): `backend/services/ingestion/alias_candidates.py`, `backend/services/ingestion/extraction_artifacts.py`, `backend/services/ingestion/organ_repair_jobs.py`
- **Tests**: `backend/tests/test_alias_apposition_phase3.py`, `backend/tests/test_alias_candidates_phase2.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_apposition_phase3.py`, `backend/tests/test_alias_candidates_phase2.py`
- Size 717 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (717 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
