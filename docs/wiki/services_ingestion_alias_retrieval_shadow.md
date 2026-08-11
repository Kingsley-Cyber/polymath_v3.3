# alias_retrieval_shadow

Source `backend/services/ingestion/alias_retrieval_shadow.py` (475 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Phase-8 live wiring for alias schema retrieval (shadow / fixture-canary).

Synthesis: imported library module; first docstring sentence: “Phase-8 live wiring for alias schema retrieval (shadow / fixture-canary).”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `register_shadow_schema_records` | 31 | `corpus_id: str, records: Sequence[ShadowSchemaRecordV1]` |
| `clear_shadow_schema_registry` | 39 | `corpus_id: str \| None=None` |
| `hydrate_shadow_records_from_db` | 52 | `corpus_ids: Sequence[str]` |
| `fixture_corpus_allowlist` | 88 | `settings: Any \| None=None` |
| `alias_retrieval_controls` | 94 | `settings: Any \| None=None` |
| `apply_fixture_ranking_effect` | 245 | `chunks: list[SourceChunk], result: DualLaneRetrievalResult, *, allow: bool` |
| `run_alias_retrieval_shadow` | 301 | `*, query: str, tier: RetrievalTier \| str, corpus_ids: Sequence[str], finalists: Sequence[SourceChunk], settings: Any...` |
| `run_alias_retrieval_shadow_async` | 435 | `*, query: str, tier: RetrievalTier \| str, corpus_ids: Sequence[str], finalists: Sequence[SourceChunk], settings: Any...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `time`, `typing`, `config`, `models`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/alias_shadow_build.py`, `backend/services/ingestion/fixture_knowledge_pipeline.py`
- **Tests**: `backend/tests/test_alias_retrieval_shadow_phase8.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_alias_retrieval_shadow_phase8.py`
- Size 475 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (475 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
