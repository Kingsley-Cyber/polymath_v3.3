# summary_tree_llm

Source `backend/services/ingestion/summary_tree_llm.py` (147 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Corpus-scoped LLM hook for document summary trees.

Synthesis: imported library module; first docstring sentence: “Corpus-scoped LLM hook for document summary trees.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `summary_tree_llm_from_pool` | 28 | `pool: list[dict[str, Any]], max_tokens: int, *, global_max_concurrent: int \| None=None, cost_controller: Any \| None...` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `typing`, `config`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/document_summaries.py`, `backend/services/ingestion/worker.py`
- **Tests**: `backend/tests/test_summary_tree_llm.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_summary_tree_llm.py`
- Size 147 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “cd12181 fix: gate ALL api_base payload sites on router-owned prefixes”. Full list: `git log --all --oneline -- backend/services/ingestion/summary_tree_llm.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
