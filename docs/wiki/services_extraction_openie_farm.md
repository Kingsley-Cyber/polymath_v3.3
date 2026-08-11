# openie_farm

Source `backend/services/extraction/openie_farm.py` (137 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> N-process warm triplet-extract farm — factory execution plane, station A.

Synthesis: imported library module; first docstring sentence: “N-process warm triplet-extract farm — factory execution plane, station A.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `default_worker_count` | 30 | `()` |
| `rendering_payload` | 40 | `rendering` |
| `get_openie_farm` | 127 | `workers: int \| None=None` |
| `OpenIEFarm.extract_all` | 99 | `self, tasks: Sequence[tuple[str, str]]` |
| `OpenIEFarm.close` | 115 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `multiprocessing`, `os`, `threading`, `typing`
- **Imports OUT** (repo-wide): `backend/services/extraction/graphify_openie.py`
- **Env vars** (name → default): `GRAPHIFY_OPENIE_WORKERS`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 137 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “3. Subprocess memory: OpenIE farm and chunk process pools” — link into the map, do not duplicate it.
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
