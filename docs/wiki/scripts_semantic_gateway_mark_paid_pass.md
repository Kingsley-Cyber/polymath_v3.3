# semantic_gateway_mark_paid_pass

Source `backend/scripts/semantic_gateway_mark_paid_pass.py` (2330 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the certified, noncanonical T9.3 semantic-digest paid pass.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/semantic_gateway_mark_paid_pass.py`), not a runtime service; first docstring sentence: “Run the certified, noncanonical T9.3 semantic-digest paid pass.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--phase`, `--corpus-name`, `--expected-packet-count`, `--max-authorized-cost-usd`, `--max-entities`, `--concurrency`, `--credential-provider`, `--provider-price-cards`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `paid_pass_ceiling_usd` | 257 | `packet_count: int` |
| `paid_phase_checkpoint` | 386 | `rows: Sequence[dict[str, Any]], *, target_count: int, minimum_acceptance: float, canonical_before: dict[str, Any], ca...` |
| `phase1_checkpoint` | 489 | `rows: Sequence[dict[str, Any]], *, canonical_before: dict[str, Any], canonical_after: dict[str, Any]` |
| `phase2_auto_stop_reason` | 504 | `rows: Sequence[dict[str, Any]], *, cumulative_cost_usd: float, cost_ceiling_usd: float` |
| `run` | 1840 | `args: argparse.Namespace` |
| `parse_args` | 2283 | `()` |
| `main` | 2308 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `dataclasses`, `datetime`, `decimal`, `hashlib`, `hmac`, `json`, `math`, `pathlib`, `re`, `sys`, `typing`, `urllib`, `uuid`, `motor`, `pydantic`, `pymongo`, `config`, `db`, `models`, `models`, `models`, `scripts`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/semantic_gateway_mark_atomic_b4.py`, `backend/scripts/semantic_gateway_mark_atomic_preflight.py`, `backend/scripts/semantic_gateway_mark_prose_phase2.py`, `backend/scripts/semantic_gateway_mark_sentence_hybrid_canary.py`, `backend/scripts/semantic_gateway_mark_sentence_hybrid_preflight.py`
- **Tests**: `backend/tests/test_semantic_gateway_mark_atomic_preflight.py`, `backend/tests/test_semantic_gateway_mark_paid_pass.py`, `backend/tests/test_semantic_gateway_mark_prose_phase2.py`, `backend/tests/test_semantic_gateway_mark_sentence_hybrid_preflight.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `ghost_b_extractions`, `ingest_batches`, `parent_chunks`, `semantic_digest_cache`, `semantic_digest_dead_letters`

## 4. Linked scripts & configs

- Referenced by docs: `docs/T9_3_PROSE_PHASE2_TELEMETRY_DRIFT_RECOVERY_SEAL_RECEIPT_2026-07-15.md`, `docs/archive/COORDINATION.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_semantic_gateway_mark_atomic_preflight.py`, `backend/tests/test_semantic_gateway_mark_paid_pass.py`, `backend/tests/test_semantic_gateway_mark_prose_phase2.py`, `backend/tests/test_semantic_gateway_mark_sentence_hybrid_preflight.py`
- Size 2330 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (2330 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- Defect-fix commits: 1 — e.g. “8c4df2e Seal paid-pass telemetry drift recovery”. Full list: `git log --all --oneline -- backend/scripts/semantic_gateway_mark_paid_pass.py`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
