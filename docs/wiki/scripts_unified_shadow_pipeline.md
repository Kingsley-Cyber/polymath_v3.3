# unified_shadow_pipeline

Source `backend/scripts/unified_shadow_pipeline.py` (1642 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Unified shadow pipeline: Relex Large + FrameExtractor union evaluation.

Synthesis: imported library module; first docstring sentence: “Unified shadow pipeline: Relex Large + FrameExtractor union evaluation.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `load_jsonl` | 101 | `path: Path` |
| `generate_unified_syntax` | 199 | `text: str, relex_entities: list[dict], chunk_id: str, extractor: FrameExtractor, *, features=None, suppressions=None` |
| `build_union_evidence` | 473 | `sid: str, pred: dict, resolved_syntax: list[dict], unmapped_syntax: list[dict], text: str='', oracle_types: dict[tupl...` |
| `build_oracle_type_map` | 709 | `gold_sample: dict, text: str` |
| `main` | 757 | `()` |
| `SuppressionRecorder.record` | 85 | `self, component: str, **fields` |
| `SuppressionRecorder.records` | 89 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `re`, `sys`, `time`, `dataclasses`, `pathlib`, `services`, `services`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/scripts/gate_diagnostic.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 1642 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (1642 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
