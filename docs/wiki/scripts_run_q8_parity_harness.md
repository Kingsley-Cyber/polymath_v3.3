# run_q8_parity_harness

Source `backend/scripts/run_q8_parity_harness.py` (473 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> q8 (owner directive 2026-08-04) — canary parity harness.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_q8_parity_harness.py`), not a runtime service; first docstring sentence: “q8 (owner directive 2026-08-04) — canary parity harness.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--token-file`, `--api-base`, `--runs`, `--output`, `--baselines`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run_one` | 106 | `api_base: str, token: str, query: str, tier: str, *, shadow: bool` |
| `universe_parity` | 194 | `cid8: str` |
| `vector_census` | 264 | `cid8: str` |
| `main` | 294 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `os`, `json`, `statistics`, `time`, `typing`, `requests`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `Q8_PARITY_CORPUS`→`c6518e7b-1327-4694-85c8-08a81542e425`

## 4. Linked scripts & configs

- Referenced by docs: `docs/archive/CONTINUITY/Q8_EVIDENCE_COLLECTION_CLOSEOUT.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 473 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (473 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
