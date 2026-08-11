# summary_from_bundle

Source `backend/services/ingestion/summary_from_bundle.py` (194 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Build SummaryInformationRecordV1 from KnowledgeArtifactBundleV1.

Synthesis: imported library module; first docstring sentence: “Build SummaryInformationRecordV1 from KnowledgeArtifactBundleV1.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `summary_record_from_bundle` | 38 | `bundle: KnowledgeArtifactBundleV1, *, trusted_aliases: Iterable[str] \| None=None` |
| `json_key` | 139 | `item: Any` |
| `json_dumps` | 145 | `item: Any` |
| `aggregate_summary_records` | 151 | `records: Iterable[SummaryInformationRecordV1]` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `re`, `collections`, `typing`, `models`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/run_graph_semantic_e2e_phase2_plus.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 194 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- None from mechanical scan (no verbatim duplicate public bodies; module < 400 lines). Logic duplication NOT EXAMINED.

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
