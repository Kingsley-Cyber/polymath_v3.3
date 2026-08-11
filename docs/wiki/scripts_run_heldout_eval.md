# run_heldout_eval

Source `backend/scripts/run_heldout_eval.py` (385 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the held-out evaluation suite against the deployed backend (P1.1).

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_heldout_eval.py`), not a runtime service; first docstring sentence: “Run the held-out evaluation suite against the deployed backend (P1.1).”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--tier`, `--ids`, `--limit`, `--model`, `--questions`, `--out-suffix`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `score` | 219 | `row: dict, run: dict, selected_corpus_ids: list[str] \| None=None` |
| `main` | 267 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `json`, `os`, `re`, `sys`, `time`, `urllib`, `pathlib`, `urllib`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `POLYMATH_API`→`http://127.0.0.1:8000`

## 4. Linked scripts & configs

- Referenced by docs: `docs/EXECUTION_PLAN_2026-07-13.md`, `docs/P7_CHAT_COST_SEAM_BUILD_RECEIPT_2026-07-17.md`, `docs/RAPTOR_RAG_IMPLEMENTATION_CHECKLIST.md`, `docs/baselines/T56_QWEN3_UNIVERSAL_AB_2026-07-14.md`, `docs/execution_plan_audit/raw/frozen_checklist_f049041.md`, `docs/execution_plan_audit/raw/parts/part_10_P0R.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 385 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
