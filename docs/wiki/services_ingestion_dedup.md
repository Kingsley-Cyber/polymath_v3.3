# dedup

Source `backend/services/ingestion/dedup.py` (653 lines) · subsystem [ingestion](INDEX.md) · [back to INDEX](INDEX.md)

## 1. Purpose

> Deterministic document-deduplication pipeline: DETECT / PREVENT / CORRECT.

Synthesis: imported library module; first docstring sentence: “Deterministic document-deduplication pipeline: DETECT / PREVENT / CORRECT.”.

Imported module only (no `__main__`).

## 2. Entry points

| Symbol | Line | Signature (verbatim from AST) |
|---|---|---|
| `classify_confidence` | 94 | `containment: float` |
| `content_words` | 102 | `texts: Iterable[str]` |
| `shingle_set` | 113 | `texts: Iterable[str], k: int=DEFAULT_SHINGLE_K` |
| `overlap` | 124 | `a: set[str], b: set[str]` |
| `jaccard` | 138 | `a: set[str], b: set[str]` |
| `containment` | 143 | `a: set[str], b: set[str]` |
| `choose_canonical` | 250 | `members: list[DuplicateMember]` |
| `find_duplicate_clusters` | 332 | `db: AsyncIOMotorDatabase, corpus_id: str, *, threshold: float=DEFAULT_DUPLICATE_THRESHOLD, min_edge_containment: floa...` |
| `resolve_duplicate_clusters` | 520 | `service: Any, corpus_id: str, clusters: list[DuplicateCluster], *, apply: bool=False, min_confidence: Optional[str]=N...` |
| `summarize_clusters` | 639 | `clusters: list[DuplicateCluster]` |
| `DuplicateMember.to_dict` | 173 | `self` |
| `DuplicateCluster.canonical` | 201 | `self` |
| `DuplicateCluster.redundant` | 205 | `self` |
| `DuplicateCluster.confidence` | 209 | `self` |
| `DuplicateCluster.auto_safe` | 218 | `self` |
| `DuplicateCluster.to_dict` | 222 | `self` |

## 3. Dependencies

- **Imports IN** (first-party stems): `__future__`, `logging`, `math`, `re`, `dataclasses`, `datetime`, `typing`, `motor`, `services`, `services`
- **Imports OUT** (repo-wide): `backend/routers/ingestion.py`, `backend/services/ingestion/worker.py`
- **Mongo collections** (db/collection-subscript literals, cross-module-verified): `chunks`, `documents`, `parent_chunks`

## 4. Linked scripts & configs

- Compose wiring: repo-root `docker-compose*.yml` — per-module mapping NOT EXAMINED (no service-to-file citation extracted).
- Skills: `~/.claude/skills/polymath-gpu-cluster/SKILL.md` (RTX cluster ops; references `scripts/setup_relex_sidecar_cuda.sh`, `INGEST_MAX_ACTIVE_BATCHES`), `~/.claude/skills/rtx-compute/SKILL.md`.

## 5. Maintenance summary

- Tests pinning it: none by import scan — edits unpinned
- Size 653 lines; coupling via §3 importers; docstring invariants (§1) are authoritative, undocumented invariants NOT EXAMINED.

## 6. Refactor candidates

- Large module (653 lines) — mechanical signal; decomposition claims need body reading (NOT EXAMINED here).

## 7. Bug dependencies

- NOT EXAMINED in [extraction_jobs_execution_map.md](../audit/extraction_jobs_execution_map.md) (module absent from the map).
- No defect-fix commit touches this file in `git log --all`.
- Regression pins: tests in §3/§5 (import scan); per-defect regression tests NOT EXAMINED beyond that.
