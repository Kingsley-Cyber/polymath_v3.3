# audit_claim_assessment_ugo

Source `backend/scripts/audit_claim_assessment_ugo.py` (541 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Read-only, count-only T8.4 UGO claim-assessment census.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/audit_claim_assessment_ugo.py`), not a runtime service; first docstring sentence: “Read-only, count-only T8.4 UGO claim-assessment census.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--input`, `--output`, `--expected-row-count`, `--corpus-id`, `--corpus-name`, `--source-version-id`, `--spacy-model`, `--provider`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 88 | `args: argparse.Namespace` |
| `parse_args` | 505 | `()` |
| `main` | 522 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `collections`, `hashlib`, `json`, `pathlib`, `typing`, `models`, `models`, `models`, `services`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 541 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (541 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
