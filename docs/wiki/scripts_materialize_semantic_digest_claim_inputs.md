# materialize_semantic_digest_claim_inputs

Source `backend/scripts/materialize_semantic_digest_claim_inputs.py` (1730 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Materialize and audit B2 atomic-claim inputs for mark digest packets.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/materialize_semantic_digest_claim_inputs.py`), not a runtime service; first docstring sentence: “Materialize and audit B2 atomic-claim inputs for mark digest packets.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-name`, `--expected-corpus-id`, `--expected-parent-count`, `--expected-child-count`, `--expected-claim-count`, `--expected-typed-claim-count`, `--expected-claim-link-count`, `--expected-evidence-count`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 1722 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `collections`, `dataclasses`, `datetime`, `hashlib`, `json`, `math`, `os`, `pathlib`, `typing`, `bson`, `motor`, `pymongo`, `config`, `models`, `models`, `models`, `services`, `services`, `services`, `services`, `scripts`
- **Imports OUT** (repo-wide): `backend/scripts/semantic_gateway_mark_atomic_b4.py`, `backend/scripts/semantic_gateway_mark_atomic_preflight.py`, `backend/scripts/semantic_gateway_mark_prose_phase2.py`, `backend/scripts/semantic_gateway_mark_sentence_hybrid_preflight.py`
- **Tests**: `backend/tests/test_materialize_semantic_digest_claim_inputs.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `corpora`, `documents`, `ghost_b_extractions`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_materialize_semantic_digest_claim_inputs.py`
- Size 1730 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1730 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
