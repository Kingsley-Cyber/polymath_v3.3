# run_two_lane_anchoring_ab

Source `backend/scripts/run_two_lane_anchoring_ab.py` (497 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run one preregistered Agent-T arm against an already deployed backend.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_two_lane_anchoring_ab.py`), not a runtime service; first docstring sentence: “Run one preregistered Agent-T arm against an already deployed backend.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--arm`, `--out`, `--api`, `--token`, `--tiers`, `--negative-tier`, `--repeat`, `--off-artifact`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 480 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `hashlib`, `json`, `os`, `re`, `sys`, `time`, `urllib`, `urllib`, `contextlib`, `pathlib`, `typing`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `POLYMATH_API`→`http://127.0.0.1:8000`, `TOKEN`→``

## 4. Linked scripts & configs

- Referenced by docs: `docs/TWO_LANE_ANCHORING_BUILD_RECEIPT_2026-07-17.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 497 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (497 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
