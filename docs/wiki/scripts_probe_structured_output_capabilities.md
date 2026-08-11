# probe_structured_output_capabilities

Source `backend/scripts/probe_structured_output_capabilities.py` (381 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Probe owner-configured routes for real native JSON-Schema support.

Synthesis: a one-shot operational tool (invoked via `python backend/scripts/probe_structured_output_capabilities.py`), not a runtime service; first docstring sentence: “Probe owner-configured routes for real native JSON-Schema support.”.

Runs as **standalone CLI** (`__main__` block present).
CLI flags (argparse, AST-captured): `--routes`, `--route-id`, `--out`, `--timeout-seconds`, `--max-provider-cost-usd`.

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `run` | 260 | `args: argparse.Namespace` |
| `parse_args` | 347 | `()` |
| `main` | 357 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `argparse`, `asyncio`, `datetime`, `json`, `math`, `pathlib`, `re`, `sys`, `time`, `typing`, `httpx`, `motor`, `config`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/probe_tier3_tool_capability.py`
- **Tests**: `backend/tests/test_structured_output_capabilities.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_structured_output_capabilities.py`
- Size 381 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
