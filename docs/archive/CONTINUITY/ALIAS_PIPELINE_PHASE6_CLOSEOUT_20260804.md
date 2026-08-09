# Alias Pipeline Phase 6 Closeout — 2026-08-04

## Status: COMPLETE — HARD PAUSE

Corpus identity clustering implemented on qualified `DocumentEntityV1` records
only. Shadow artifacts written under `data_eval/alias_pipeline/`.

**STOP.** Do not begin Phase 7 schemas projection activation or retrieval
integration without owner authorization.

```yaml
production:
  production_alias_records_mutated: false
  production_entity_ids_replaced: false
  production_schemas_overwritten: false
  production_graph_identity_edges_created: false
  Fast_schema_expansion_activated: false
  full_corpus_backfill_started: false
```

---

## 1. Exact files changed

| Path | Role |
|---|---|
| `backend/models/alias_identity.py` | `CorpusMergeDecisionV1`, expanded `CorpusEntityV1`, temporal record |
| `backend/services/ingestion/alias_corpus_clustering.py` | blocking, pair policy, bridge protection, shadow projection |
| `backend/tests/test_alias_corpus_clustering_phase6.py` | fixture + determinism + safety tests |
| `backend/tests/test_alias_identity_contracts.py` | CorpusEntityV1 create kwargs update |
| `scripts/alias_pipeline/generate_phase6_artifacts.py` | shadow artifact generator |
| `data_eval/alias_pipeline/*` | Phase-6 JSONL/JSON deliverables |
| `CONTINUITY/ALIAS_PIPELINE_PHASE6_CLOSEOUT_20260804.md` | this closeout |

---

## 2. Blocking strategy

Bounded keys only (never all-pairs):

- `canon:<normalized canonical>`
- `curated:<explicit curated canonical>` (explicit inventory field only)
- `alias:<trusted non-acronym alias>`
- `acro:<SHORT>|<normalized long form>`
- `temporal:<former name>` / `canon:<former>` for chronology links
- `alnum:<stripped canonical>` (length ≥ 4)
- `uniqacro:<SHORT>` only when the short form has a **unique** corpus-wide expansion

MEASURED on fixture run: 17 document entities → 35 blocking keys → **8** candidate pairs (≪ C(17,2)=136).

---

## 3. Pair-level merge policy

| Method | Decision |
|---|---|
| Explicit curated canonical match | `MERGE` |
| Same explicit full canonical | `MERGE` |
| Casing/punctuation-equivalent full name | `MERGE` |
| Trusted non-acronym alias overlap | `MERGE` |
| Validated acronym+long-form match | `MERGE` |
| Unique corpus acronym promotion (define↔usage) | `MERGE` |
| Temporal former↔current | `LINK_TEMPORAL_IDENTITY` |
| Incompatible entity types | `REJECT` |
| Conflicting acronym expansions | `REJECT` |
| Bare acronym only | `REJECT` |
| Retrieval / related / description overlap only | `REJECT` |
| Blocked but no authoritative evidence | `REVIEW` |

Every union cites a `CorpusMergeDecisionV1` with deterministic `decision_hash`.

---

## 4. Bridge-conflict behavior

Before unioning two components, `_union_compatible` checks the **full proposed
set** for:

- conflicting acronym expansions
- incompatible entity types
- conflicting curated targets

Shared weak alias `IR` cannot merge Information Retrieval with Infrared.
Fixture: 2 IR clusters; `unsupported_transitive_bridge_merges` policy rejects
optimistic bridges.

---

## 5. Acronym-scope behavior

- Default: document-scoped
- Corpus promotion only when long-form matches, types compatible, **no conflicting
  expansion in corpus scope**, and deterministic support present
- Multiple expansions → separate corpus entities; short form retained in
  `ambiguous_aliases` (scoped retrieval only)
- Frequency alone never picks a meaning

---

## 6. Temporal-identity behavior

- `LINK_TEMPORAL_IDENTITY` / former-name inventory → `temporal_aliases` +
  `temporal_identity_records[{former_name,current_name,evidence_ids,…}]`
- Former names are **not** flattened into timeless `trusted_aliases` without
  chronological retention

---

## 7. Canonical-name selection

Precedence (deterministic):

1. Explicit curated canonical  
2. Validated acronym long form  
3. Supported proper-name form  
4. Definition form  
5. Frequency among qualified surfaces  
6. Lexicographic tie-break  

No embedding / insertion-order / nondeterministic set iteration.

Note: auto-deriving curated from global `canonicalize_entity_name()` is
**disabled** (map collapses “Retrieval-Augmented Generation”→`rag`). Curated
must be explicit on the inventory.

---

## 8. Fixture precision / false-merge count

From `phase6_acceptance_matrix.json` (MEASURED):

```yaml
positive_rag_clusters: 1
positive_rag_trusted_alias_RAG: true
ambiguous_ir_clusters: 2
homonym_apple_clusters: 2
cooccurrence_false_merge: false
false_merge_count: 0
replay_ok: true
production_mutated: false
```

---

## 9. Replay determinism

```yaml
input_order_independent: true   # 8 randomized shuffles in unit tests; 5 in artifact generator
corpus_entity_ids_identical_on_replay: true
cluster_hashes_identical_on_replay: true
merge_decision_hashes_identical_on_replay: true
canonical_names_identical_on_replay: true
```

Artifact: `data_eval/alias_pipeline/corpus_cluster_replay.json`.

---

## 10. Runtime and peak memory (MEASURED)

Host: local Mac fixture run via `generate_phase6_artifacts.py`

```yaml
document_entities_total: 17
candidate_pairs_total: 8
accepted_merges_total: 6
temporal_links_total: 1
rejected_pairs_total: 1
clustering_wall_time_ms: 1.155
peak_rss_mb: 267.938
```

Complexity driven by blocking groups, not N².

---

## 11. Known unresolved cases

- Document-only bare acronym usage under an **ambiguous** short form stays
  unmerged/unpaired (no `uniqacro` block) — correct safety; may need a later
  scoped retrieval key projection (Phase 7).
- Global `entity_aliases.json` collapse of long RAG forms is not used as
  curated authority here; explicit curated inventories required.
- `valid_before` / `valid_after` temporal dates are nullable until extraction
  supplies them.
- Idempotent **persistence** is shadow-file only; no Mongo upsert path yet
  (by design — production_writes=false).

---

## 12. Production not mutated

Confirmed: generator writes only under `data_eval/alias_pipeline/`. No writes to
`corpus_*_schemas`, Neo4j identity edges, or live alias collections.

Shadow projection: `corpus_shadow_schemas_projection.jsonl` with
`identity_authority: false`.

---

## Receipts

```text
cd backend && ../local_ghost_b/.venv/bin/python -m pytest \
  tests/test_alias_corpus_clustering_phase6.py \
  tests/test_alias_document_clustering_phase5.py \
  tests/test_alias_gate_phase4.py \
  tests/test_alias_apposition_phase3.py \
  tests/test_alias_candidates_phase2.py \
  tests/test_alias_identity_contracts.py -q
→ 75 passed

local_ghost_b/.venv/bin/python scripts/alias_pipeline/generate_phase6_artifacts.py
→ false_merge_count: 0, replay_ok: true
```

## Deliverables present

```text
CONTINUITY/ALIAS_PIPELINE_PHASE6_CLOSEOUT_20260804.md
data_eval/alias_pipeline/corpus_merge_candidates.jsonl
data_eval/alias_pipeline/corpus_merge_decisions.jsonl
data_eval/alias_pipeline/corpus_entities.jsonl
data_eval/alias_pipeline/corpus_cluster_conflicts.jsonl
data_eval/alias_pipeline/corpus_cluster_replay.json
data_eval/alias_pipeline/corpus_cluster_metrics.json
data_eval/alias_pipeline/phase6_acceptance_matrix.json
data_eval/alias_pipeline/corpus_shadow_schemas_projection.jsonl
```

## Resume pointer

Next authorized slice: **Phase 7 — schemas projection / retrieval integration**
(owner go required). Still `production_mutation_authorized=false` until
explicitly lifted.
