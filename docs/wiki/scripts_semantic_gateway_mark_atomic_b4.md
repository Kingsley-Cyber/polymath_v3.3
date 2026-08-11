# semantic_gateway_mark_atomic_b4

Source `backend/scripts/semantic_gateway_mark_atomic_b4.py` (607 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Execute the senior-authorized, noncanonical atomic-claims B4 canary.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/semantic_gateway_mark_atomic_b4.py`), not a runtime service; first docstring sentence: “Execute the senior-authorized, noncanonical atomic-claims B4 canary.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--authorization-reference`, `--expected-packet-set-hash`, `--expected-selection-set-hash`, `--expected-prompt-hash`, `--expected-repair-prompt-hash`, `--expected-schema-hash`, `--max-authorized-cost-usd`, `--corpus-name`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 383 | `args: argparse.Namespace` |
| `main` | 585 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `decimal`, `hmac`, `json`, `pathlib`, `typing`, `urllib`, `uuid`, `motor`, `config`, `models`, `scripts`, `scripts`, `scripts`, `scripts`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/semantic_gateway_mark_sentence_hybrid_canary.py`
- **Tests**: `backend/tests/test_semantic_gateway_mark_atomic_b4.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ingest_batches`

## 4. Linked scripts & configs

- Referenced by docs: `docs/T9_3_B4_FAILURE_RECEIPT_2026-07-15.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_semantic_gateway_mark_atomic_b4.py`
- Size 607 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (607 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
