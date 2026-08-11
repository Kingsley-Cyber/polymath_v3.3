# backfill_parent_domains_llm

Source `backend/scripts/backfill_parent_domains_llm.py` (150 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> LLM backfill of `domain` + `topics` onto parent_chunks (Ghost-A-style tags).

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/backfill_parent_domains_llm.py`), not a runtime service; first docstring sentence: “LLM backfill of `domain` + `topics` onto parent_chunks (Ghost-A-style tags).”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--sample`, `--limit`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 93 | `sample: int \| None=None, limit: int \| None=None` |

## 3. Dependencies

- **Imports IN** (first-party stems): `argparse`, `asyncio`, `json`, `logging`, `os`, `httpx`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `parent_chunks`
- **Env vars** (name → default): `DOMAIN_POOL_PATH`→`/tmp/domain_pool.json`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/RETRIEVAL_WIRING_VERIFICATION.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 150 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
