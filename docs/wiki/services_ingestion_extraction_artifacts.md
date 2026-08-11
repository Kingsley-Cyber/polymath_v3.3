# extraction_artifacts

Source `backend/services/ingestion/extraction_artifacts.py` (498 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Additive adapters into the shared P2.6 candidate artifact contract.

Synthesis: imported library module; first docstring sentence: “Additive adapters into the shared P2.6 candidate artifact contract.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `adapt_extraction_result` | 153 | `result: Any, *, engine: ExtractionEngine, engine_runtime_version: str, source_wire_contract_version: str, source_cont...` |
| `candidate_artifact_to_lexicon_row` | 400 | `artifact: CandidateExtractionArtifact` |
| `adapt_extraction_failure` | 452 | `failure: Any, *, engine: ExtractionEngine, engine_runtime_version: str, source_wire_contract_version: str, source_con...` |
| `_EvidenceBuilder.exact` | 98 | `self, value: str, *, label: str` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `hashlib`, `dataclasses`, `typing`, `models`, `services`
- **Imports OUT** (repo-wide): `backend/services/ingestion/corpus_lexicon.py`
- **Tests**: `backend/tests/test_corpus_lexicon.py`, `backend/tests/test_extraction_artifact.py`, `backend/tests/test_extraction_parity_burst.py`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: `backend/tests/test_corpus_lexicon.py`, `backend/tests/test_extraction_artifact.py`, `backend/tests/test_extraction_parity_burst.py`
- Size 498 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (498 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
