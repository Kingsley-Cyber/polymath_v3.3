# semantic_gateway_mark_atomic_preflight

Source `backend/scripts/semantic_gateway_mark_atomic_preflight.py` (699 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Build the zero-provider, read-only T9.3 B4 atomic-packet preflight.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/semantic_gateway_mark_atomic_preflight.py`), not a runtime service; first docstring sentence: “Build the zero-provider, read-only T9.3 B4 atomic-packet preflight.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--corpus-name`, `--expected-parent-count`, `--expected-child-count`, `--max-entities`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 674 | `()` |
| `AtomicPopulationRow.parent_id` | 88 | `self` |
| `AtomicPopulationRow.doc_id` | 92 | `self` |
| `AtomicPopulationRow.packet_bytes` | 96 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `collections`, `dataclasses`, `hashlib`, `json`, `pathlib`, `typing`, `config`, `models`, `models`, `models`, `scripts`, `scripts`, `scripts`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/semantic_gateway_mark_atomic_b4.py`
- **Tests**: `backend/tests/test_semantic_gateway_mark_atomic_preflight.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `ghost_b_extractions`, `ingest_batches`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_semantic_gateway_mark_atomic_preflight.py`
- Size 699 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (699 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
