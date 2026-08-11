# schema_lens

Source `backend/services/ingestion/schema_lens.py` (870 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Auto schema lens generation for Ghost B ingestion.

Synthesis: imported library module; first docstring sentence: “Auto schema lens generation for Ghost B ingestion.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `build_deterministic_schema_lens` | 504 | `*, corpus_id: str, filename: str, parents: list[Any], children: list[Any], entity_schema: list[str] \| None=None, rel...` |
| `sanitize_schema_lens` | 591 | `payload: dict[str, Any] \| SchemaLens \| None, *, base: SchemaLens, entity_schema: list[str] \| None=None, relation_s...` |
| `merge_schema_lenses` | 663 | `stored: SchemaLens \| dict \| None, doc_lens: SchemaLens, *, entity_schema: list[str] \| None=None, relation_schema: ...` |
| `get_or_create_schema_lens` | 792 | `*, db: AsyncIOMotorDatabase, corpus_id: str, filename: str, parents: list[Any], children: list[Any], entity_schema: l...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `logging`, `re`, `time`, `datetime`, `typing`, `httpx`, `config`, `motor`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_object_kind_prompt_steering.py`, `backend/tests/test_schema_lens.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_object_kind_prompt_steering.py`, `backend/tests/test_schema_lens.py`
- Size 870 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (870 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “cd12181 fix: gate ALL api_base payload sites on router-owned prefixes”. Full list: `git log --all --oneline -- backend/services/ingestion/schema_lens.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
