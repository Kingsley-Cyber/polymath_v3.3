# semantic_gateway_ugo_canary

Source `backend/scripts/semantic_gateway_ugo_canary.py` (1516 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run the T4.4 structured-gateway canary on accepted UGO evidence.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/semantic_gateway_ugo_canary.py`), not a runtime service; first docstring sentence: “Run the T4.4 structured-gateway canary on accepted UGO evidence.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-name`, `--count`, `--max-entities`, `--concurrency`, `--force-repair-index`, `--canary-tier`, `--tier1-provider-blocked`, `--credential-provider`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 1190 | `args: argparse.Namespace` |
| `parse_args` | 1453 | `()` |
| `main` | 1490 | `()` |
| `ProviderPriceCard.receipt_source` | 144 | `self` |
| `_MemoryStore.load_success` | 344 | `self, _cache_key: str` |
| `_MemoryStore.save_success` | 347 | `self, result: SemanticGatewayResult` |
| `_MemoryStore.save_dead_letter` | 350 | `self, **_kwargs` |
| `_StaticInvalidTransport.complete` | 360 | `self, **_kwargs` |
| `_StaticInvalidTransport.complete_tool` | 364 | `self, **_kwargs` |
| `_FirstResponseParentFaultTransport.complete` | 396 | `self, **kwargs` |
| `_FirstResponseParentFaultTransport.complete_tool` | 399 | `self, **kwargs` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `dataclasses`, `datetime`, `json`, `pathlib`, `re`, `sys`, `typing`, `urllib`, `motor`, `neo4j`, `qdrant_client`, `config`, `models`, `models`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/materialize_semantic_digest_claim_inputs.py`, `backend/scripts/semantic_gateway_mark_atomic_b4.py`, `backend/scripts/semantic_gateway_mark_atomic_preflight.py`, `backend/scripts/semantic_gateway_mark_paid_pass.py`, `backend/scripts/semantic_gateway_mark_prose_phase2.py`, `backend/scripts/semantic_gateway_mark_sentence_hybrid_canary.py`, `backend/scripts/semantic_gateway_mark_sentence_hybrid_preflight.py`
- **Tests**: `backend/tests/test_semantic_gateway_mark_paid_pass.py`, `backend/tests/test_semantic_gateway_ugo_canary.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `corpora`, `documents`, `ghost_b_extractions`, `ingest_batches`, `parent_chunks`, `semantic_digest_cache`, `semantic_digest_dead_letters`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_semantic_gateway_mark_paid_pass.py`, `backend/tests/test_semantic_gateway_ugo_canary.py`
- Size 1516 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1516 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
