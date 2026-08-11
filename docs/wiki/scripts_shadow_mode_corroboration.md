# shadow_mode_corroboration

Source `backend/scripts/shadow_mode_corroboration.py` (640 lines) · subsystem [scripts](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Shadow-mode evaluation of the corroboration gate on 18 gold relations.

Synthesis: imported library module; first docstring sentence: “Shadow-mode evaluation of the corroboration gate on 18 gold relations.”.

Runs as **standalone CLI** (`__main__` block present).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `load_jsonl` | 60 | `path: Path` |
| `find_all_mentions` | 77 | `text: str, surface: str` |
| `build_all_mentions` | 103 | `text: str, gold_entities: list[dict]` |
| `generate_syntax_for_chunk` | 151 | `text: str, gold_entities: list[dict], chunk_id: str, extractor: DepPathExtractor` |
| `find_relex_pair_for_gold` | 281 | `gold_rel: dict, evidence_list: list[RelationEvidence]` |
| `main` | 353 | `()` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `json`, `sys`, `pathlib`, `services`, `services`, `services`, `services`, `services`, `services`
- **Imports OUT**: none — executed as a CLI (see §1), not imported.

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 640 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (640 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
