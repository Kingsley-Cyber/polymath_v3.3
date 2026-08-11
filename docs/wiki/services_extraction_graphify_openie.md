# graphify_openie

Source `backend/services/extraction/graphify_openie.py` (493 lines) · subsystem [extraction](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Balanced CPU OpenIE proposal source for canonical Graphify.

Synthesis: imported library module; first docstring sentence: “Balanced CPU OpenIE proposal source for canonical Graphify.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `get_triplet_extract_cpu_provider` | 166 | `()` |
| `run_openie_extraction` | 271 | `documents: Sequence[NormalizedDocumentV1], units: Sequence[OpenIEUnit], provider: TripletExtractCPUProvider` |
| `TripletExtractCPUProvider.is_default_loader` | 124 | `self` |
| `TripletExtractCPUProvider.load_count` | 140 | `self` |
| `TripletExtractCPUProvider.health` | 143 | `self` |
| `TripletExtractCPUProvider.extract` | 161 | `self, text: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `importlib`, `threading`, `os`, `time`, `re`, `dataclasses`, `typing`, `services`, `models`
- **Imports OUT** (repo-wide): `backend/scripts/run_graphify_openie.py`, `backend/scripts/verify_factory_equality.py`, `backend/services/extraction/graphify_pipeline.py`
- **Tests**: `backend/tests/extraction/test_graphify_openie.py`
- **Env vars** (name → default): `GRAPHIFY_OPENIE_DISABLED`→``

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/extraction/test_graphify_openie.py`
- Size 493 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (493 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- Audited in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) sections: “3. Subprocess memory: OpenIE farm and chunk process pools” — link into the map, do not duplicate it.
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
