# semantic_gateway_mark_sentence_hybrid_preflight

Source `backend/scripts/semantic_gateway_mark_sentence_hybrid_preflight.py` (873 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Build the credential-blind, zero-provider sentence-hybrid v3 preflight.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/semantic_gateway_mark_sentence_hybrid_preflight.py`), not a runtime service; first docstring sentence: “Build the credential-blind, zero-provider sentence-hybrid v3 preflight.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-name`, `--expected-parent-count`, `--expected-child-count`, `--expected-packet-ready-count`, `--expected-non-packet-ready-count`, `--expected-source-sentence-count`, `--expected-mapped-sentence-count`, `--expected-context-only-sentence-count`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 846 | `()` |
| `SentenceHybridPopulationRow.parent_id` | 94 | `self` |
| `SentenceHybridPopulationRow.doc_id` | 98 | `self` |
| `SentenceHybridPopulationRow.packet_bytes` | 102 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `collections`, `dataclasses`, `decimal`, `hashlib`, `json`, `pathlib`, `typing`, `config`, `models`, `models`, `models`, `models`, `scripts`, `scripts`, `scripts`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/semantic_gateway_mark_sentence_hybrid_canary.py`
- **Tests**: `backend/tests/test_semantic_gateway_mark_sentence_hybrid_preflight.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ghost_b_extractions`, `ingest_batches`

## 4. Linked scripts & configs

- Referenced by docs: `docs/T9_3_SENTENCE_HYBRID_V3_PREFLIGHT_RECEIPT_2026-07-15.md`
- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_semantic_gateway_mark_sentence_hybrid_preflight.py`
- Size 873 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (873 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
