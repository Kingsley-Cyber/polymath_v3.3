# run_claim_anchor_micro_ab

Source `backend/scripts/run_claim_anchor_micro_ab.py` (564 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Run one read-only arm of the preregistered claim-anchor micro A/B.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/run_claim_anchor_micro_ab.py`), not a runtime service; first docstring sentence: “Run one read-only arm of the preregistered claim-anchor micro A/B.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--spec`, `--output`, `--expected-flag`, `--base`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `main` | 364 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `hashlib`, `json`, `os`, `time`, `urllib`, `datetime`, `pathlib`, `typing`, `bson`, `config`, `pymongo`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/run_claim_anchor_additivity_replay.py`
- **Tests**: `backend/tests/test_claim_anchor_micro_ab.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_claim_anchor_micro_ab.py`
- Size 564 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (564 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
