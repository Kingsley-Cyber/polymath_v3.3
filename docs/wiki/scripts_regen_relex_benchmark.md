# regen_relex_benchmark

Source `backend/scripts/regen_relex_benchmark.py` (608 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Regenerate the frozen Relex Large benchmark predictions with preserved entity types.

Synthesis: imported library module; first docstring sentence: “Regenerate the frozen Relex Large benchmark predictions with preserved entity types.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `resolve_torch_device` | 45 | `preference: str='auto'` |
| `load_gold_texts` | 108 | `()` |
| `run_model` | 121 | `gold_samples: list[dict[str, Any]], device: torch.device, dry_run: bool=False` |
| `verify_invariants` | 375 | `rows: list[dict[str, Any]]` |
| `write_artifact` | 431 | `rows: list[dict[str, Any]], out_path: Path, device: torch.device` |
| `print_label_inventory_report` | 474 | `rows: list[dict[str, Any]]` |
| `main` | 540 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `os`, `platform`, `sys`, `time`, `pathlib`, `typing`, `torch`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.
- **Env vars** (name → default): `PYTORCH_ENABLE_MPS_FALLBACK`→`0`, `PYTORCH_MPS_FAST_MATH`→`0`, `PYTORCH_MPS_PREFER_METAL`→`0`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 608 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (608 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
