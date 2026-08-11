# q9_phase0_snapshot

Source `backend/scripts/q9_phase0_snapshot.py` (164 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Phase 0 snapshot for q9 final E2E — source hashes + store counts.

Synthesis: imported library module; first docstring sentence: “Phase 0 snapshot for q9 final E2E — source hashes + store counts.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 17 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `asyncio`, `hashlib`, `json`, `os`, `pathlib`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `NEO4J_USER`→`neo4j`, `Q9_PHASE0_OUT`→`/tmp/q9_phase0_snapshot.json`, `QDRANT_URL`→`http://qdrant:6333`, `REDIS_HOST`→`redis`, `REDIS_PORT`→`6379`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 164 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
