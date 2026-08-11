# semantic_gateway_mark_prose_phase2

Source `backend/scripts/semantic_gateway_mark_prose_phase2.py` (2357 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the owner-authorized B1-scoped mark Phase-2 prose purchase.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/semantic_gateway_mark_prose_phase2.py`), not a runtime service; first docstring sentence: “Run the owner-authorized B1-scoped mark Phase-2 prose purchase.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--mode`, `--corpus-name`, `--expected-parent-count`, `--expected-child-count`, `--max-entities`, `--authorization-reference`, `--resume-authorization-reference`, `--continuation-authorization-reference`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `phase2_prose_stop_reason` | 306 | `rows: Sequence[dict[str, Any]]` |
| `phase2_prose_resume_stop_reason` | 339 | `rows: Sequence[dict[str, Any]], *, control: ProsePhase2ResumeControl` |
| `phase2_prose_concurrency` | 566 | `rows: Sequence[dict[str, Any]]` |
| `receipt_accounting_closes` | 1123 | `*, eligible_ids: set[str], selected_ids: set[str], attempted_ids: set[str], certified_ids: set[str], explicitly_exclu...` |
| `run` | 1806 | `args: argparse.Namespace` |
| `main` | 2323 | `()` |
| `ProsePhase2ResumeControl.deadline_terminal_count` | 202 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `collections`, `contextlib`, `dataclasses`, `datetime`, `decimal`, `hashlib`, `hmac`, `json`, `pathlib`, `sys`, `typing`, `urllib`, `uuid`, `motor`, `pymongo`, `config`, `models`, `scripts`, `scripts`, `scripts`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_semantic_gateway_mark_prose_phase2.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ghost_b_extractions`, `ingest_batches`, `parent_chunks`, `semantic_digest_cache`

## 4. Linked scripts & configs

- Referenced by docs: `docs/T9_3_PROSE_PHASE2_RESUME_SEAL_RECEIPT_2026-07-15.md`, `docs/T9_3_PROSE_PHASE2_TELEMETRY_DRIFT_RECOVERY_SEAL_RECEIPT_2026-07-15.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_semantic_gateway_mark_prose_phase2.py`
- Size 2357 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (2357 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “8c4df2e Seal paid-pass telemetry drift recovery”. Full list: `git log --all --oneline -- backend/scripts/semantic_gateway_mark_prose_phase2.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
