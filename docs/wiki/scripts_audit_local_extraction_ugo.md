# audit_local_extraction_ugo

Source `backend/scripts/audit_local_extraction_ugo.py` (208 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Read-only trained-spaCy audit of LocalExtractionV1 on UGO child text.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/audit_local_extraction_ugo.py`), not a runtime service; first docstring sentence: “Read-only trained-spaCy audit of LocalExtractionV1 on UGO child text.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--input`, `--output`, `--corpus-id`, `--corpus-name`, `--source-version-id`, `--spacy-model`, `--sample-count`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 64 | `args: argparse.Namespace` |
| `parse_args` | 178 | `()` |
| `main` | 190 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `collections`, `json`, `pathlib`, `typing`, `models`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Tests**: `backend/tests/test_local_extraction_ugo_audit.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_local_extraction_ugo_audit.py`
- Size 208 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- `parse_args` body is verbatim-identical to `backend/scripts/audit_claim_compiler_ugo.py` (AST dump hash match) — dedup candidate.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
