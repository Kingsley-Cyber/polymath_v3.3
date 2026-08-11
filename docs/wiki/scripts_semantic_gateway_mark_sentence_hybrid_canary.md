# semantic_gateway_mark_sentence_hybrid_canary

Source `backend/scripts/semantic_gateway_mark_sentence_hybrid_canary.py` (486 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Execute the exact senior-authorized sentence-hybrid v3 canary.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/semantic_gateway_mark_sentence_hybrid_canary.py`), not a runtime service; first docstring sentence: “Execute the exact senior-authorized sentence-hybrid v3 canary.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--authorization-reference`, `--expected-packet-set-hash`, `--expected-packet-schema-hash`, `--expected-selection-set-hash`, `--expected-prompt-hash`, `--expected-repair-prompt-hash`, `--expected-digest-schema-hash`, `--max-authorized-cost-usd`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 216 | `args: argparse.Namespace` |
| `main` | 461 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `decimal`, `json`, `pathlib`, `typing`, `urllib`, `uuid`, `motor`, `config`, `scripts`, `scripts`, `scripts`, `scripts`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_semantic_gateway_mark_sentence_hybrid_canary.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ingest_batches`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_semantic_gateway_mark_sentence_hybrid_canary.py`
- Size 486 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (486 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
