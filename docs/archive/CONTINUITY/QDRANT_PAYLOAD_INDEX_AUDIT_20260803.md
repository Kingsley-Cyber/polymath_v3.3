# Qdrant Payload-Index Audit — 2026-08-03

Status: **AUDIT ONLY. No index was created, modified, or deleted during this
audit.** Live stack was queried read-only; static analysis grepped the repo.

Scope: every field in `_CHUNK_PAYLOAD_INDEXES`
(`backend/services/storage/qdrant_writer.py`), plus the 14 non-tuple fields
that ad-hoc creators index into the same chunk collections.

## Method

1. **Live probe (read-only):** `tmp/qdrant_payload_index_audit_probe.py`,
   executed in the backend container against `qdrant:6333`. Raw output:
   `tmp/qdrant_audit_live.json`. Enumerated collections matching
   `corpus_[0-9a-f]{8}_(naive|hrag|graph)`, read `get_collection` →
   `payload_schema` per field (`points` = populated points).
2. **Static query-usage:** grep of every Qdrant filter construction
   (`FieldCondition(key=...)`, `must`/`must_not`/`should`, `count`, `scroll`
   with filter) across `backend/services`, `backend/routers`,
   `backend/scripts`, `backend/polymath_mcp`. No variable-key filters exist.
   MCP tools construct **zero** Qdrant filters (they scope by corpus_ids and
   delegate to the retriever).

Live topology at probe time: **27 chunk collections** (9 corpora × 3 kinds),
**1,071,617 points**, all 15 tuple fields indexed in all 27 collections
(eager creation in `ensure_collections_for_corpus`), plus 14 extra fields
indexed in all 27 (via `promote.py::promoted_index_fields` and backfill
scripts).

Caveat: `indexed_points` reported 0 for every field — the running Qdrant
version's `payload_schema` does not reliably expose that counter. Population
numbers below come from `payload_schema.points` and are trustworthy. Do not
read `indexed_points: 0` as "indexes not built".

---

## Field matrix — `_CHUNK_PAYLOAD_INDEXES` (15 fields)

```yaml
field: corpus_id
collections_indexed: 27
populated_points: 1071617
population_rate: 1.0
query_usage:
  filters_found: 6            # funnel_a, funnel_b, qdrant_writer doc-scoped
                              # scrolls/deletes, desired_state, verify;
                              # plus collection-owner scroll (no filter)
  production_callers:
    - retriever.funnel_a
    - retriever.funnel_b
    - storage.qdrant_writer (doc deletion / summary scroll)
    - control_plane.desired_state
    - ingestion.verify
classification: KEEP
reason: >
  Protected (corpus isolation). Every retrieval lane filters on it. Also the
  8-char-prefix-collision ownership check reads it. Mandatory.
```

```yaml
field: doc_id
collections_indexed: 27
populated_points: 1071617
population_rate: 1.0
query_usage:
  filters_found: 13
  production_callers:
    - retriever.funnel_a / funnel_b / lexical / vocabulary
    - storage.qdrant_writer (per-doc delete + summary scrolls)
    - ingestion.worker (count checks) / ingestion.verify
    - ingestion_service (summary count) / control_plane.desired_state
classification: KEEP
reason: >
  Protected (document identity). Heaviest-filtered field after corpus_id;
  document deletion, verification, and doc-scoped retrieval all need it.
```

```yaml
field: chunk_id
collections_indexed: 27
populated_points: 1071617
population_rate: 1.0
query_usage:
  filters_found: 3
  production_callers:
    - ingestion.verify (post-ingest point verification)
    - scripts.backfill_child_domain (set_payload scoping)
    - scripts.reclassify_citation_chunks (maintenance)
classification: KEEP
reason: >
  Protected (chunk identity). Production verify path filters on it;
  maintenance scripts depend on it. Point IDs are derived UUIDs, not the
  string chunk_id, so the payload filter is the only lookup path.
```

```yaml
field: parent_id
collections_indexed: 27
populated_points: 1071617
population_rate: 1.0
query_usage:
  filters_found: 3
  production_callers:
    - retriever.funnel_a / funnel_b (parent-scoped MatchAny)
    - scripts.backfill_mechanisms
classification: KEEP
reason: Protected (parent/child identity). Actively filtered by both funnels.
```

```yaml
field: chunk_type
collections_indexed: 27
populated_points: 1071617
population_rate: 1.0
query_usage:
  filters_found: 7
  production_callers:
    - retriever.tier0_router (must_not semantic_digest)
    - retriever.four_lane_router (must semantic_digest)
    - storage.qdrant_writer (summary-lane scrolls)
    - ingestion.worker / ingestion_service (summary counts)
classification: KEEP
reason: >
  Routing eligibility discriminator (chunk vs summary vs semantic_digest).
  Actively filtered on the hot routing path.
```

```yaml
field: source_tier
collections_indexed: 27
populated_points: 1071617
population_rate: 1.0
query_usage:
  filters_found: 0
  production_callers: []      # payload-projection + in-memory tier counting
                              # only (retriever.__init__._source_tier_counts)
classification: KEEP
reason: >
  PROTECTED per owner's protected-field list (tier/route eligibility) — no
  filter construction exists today, but removal requires a direct
  full-scan-regression benchmark first, which has not been run. Single
  low-cardinality keyword, population 1.0, marginal cost. Re-audit after
  pilot benchmarks; candidate for future MEASURE_FIRST reclassification.
```

```yaml
field: user_id
collections_indexed: 27
populated_points: 1071617
population_rate: 1.0
query_usage:
  filters_found: 0
  production_callers: []      # ownership enforced structurally via
                              # per-corpus collections, not point filters
classification: KEEP
reason: >
  PROTECTED (ownership defence-in-depth). Primary isolation is
  collection-per-corpus; the per-point user_id is the fallback check if a
  collection is ever shared or migrated (see scripts_migrate_multitenant).
  Benchmark required before any removal.
```

```yaml
field: chunk_kind
collections_indexed: 27
populated_points: 1071617
population_rate: 1.0
query_usage:
  filters_found: 3
  production_callers:
    - retriever.funnel_a (must_not NOISY_KINDS)
    - retriever.funnel_b (must_not NOISY_KINDS)
    - retriever.lexical (must_not NOISY_KINDS)
classification: KEEP
reason: >
  Every retrieval lane excludes noisy kinds (toc/bibliography/index/
  appendix/front_matter/back_matter) via this field. The original reason it
  was added (unindexed must_not was a full scan) still applies.
```

```yaml
field: language
collections_indexed: 27
populated_points: 3486
population_rate: 0.0033
query_usage:
  filters_found: 0
  production_callers: []      # designed for code-lane per-language scopes
                              # (/python, /rust, /luau) — lane not shipped
classification: LAZY_CREATE
reason: >
  Valid field with a designed future use, but population 0.33% is below
  INDEX_CREATION_MIN_POPULATION_RATE (0.01) and no filter exists today.
  Stop eager creation; create the index only when a code corpus populates it
  past threshold. Payload stays; nothing reads the index today.
```

```yaml
field: schema_version
collections_indexed: 27
populated_points: 138164
population_rate: 0.1289
query_usage:
  filters_found: 0
  production_callers: []      # payload-projection only (summary payloads,
                              # reconcile lists)
classification: DROP
reason: >
  No filter construction, no count/facet use, no MCP use, no migration or
  backfill dependency, no contract requiring it. Reads are always
  with_payload scrolls keyed by other indexed fields. Payload retained.
```

```yaml
field: summary_type
collections_indexed: 27
populated_points: 138164
population_rate: 0.1289
query_usage:
  filters_found: 0
  production_callers: []      # payload-projection only
classification: DROP
reason: Same evidence as schema_version. No planned contract found in code.
```

```yaml
field: source_child_ids
collections_indexed: 27
populated_points: 138152
population_rate: 0.1289
query_usage:
  filters_found: 0
  production_callers: []      # payload-projection only; Mongo is the source
                              # of truth (summary_tree / claim anchors)
classification: DROP
reason: >
  No filter use, and it is the most expensive index per point: a keyword
  index over multi-element child-ID arrays on summary points. Highest
  expected disk saving of the tuple DROP set.
```

```yaml
field: domain
collections_indexed: 27
populated_points: 0
population_rate: 0.0
query_usage:
  filters_found: 0
  production_callers: []      # scripts/backfill_child_domain.py writes the
                              # payload AND creates its own index (wait=True)
                              # but has populated zero points fleet-wide
classification: DROP
reason: >
  Zero populated points across 1.07M points and zero query filters. The only
  justification on record is the M1 comment "future query-domain pre-filter"
  — a comment is not a contract. The backfill script self-creates the index
  on any corpus it ever populates, so dropping the eager index cannot break
  that path. Owner-named DROP candidate — confirmed.
```

```yaml
field: concepts
collections_indexed: 27
populated_points: 824708
population_rate: 0.7696
query_usage:
  filters_found: 1
  production_callers:
    - retriever.funnel_b (should soft prefilter, MatchAny)
classification: KEEP
reason: >
  Protected Q2/U2 field: the funnel-B soft prefilter matches it, and the
  unindexed full-scan regression was measured at 21.6s on a 561k-chunk
  corpus. Removal requires direct benchmark; not attempted this slice.
```

```yaml
field: entity_ids
collections_indexed: 27
populated_points: 824708
population_rate: 0.7696
query_usage:
  filters_found: 1
  production_callers:
    - retriever.funnel_b (should soft prefilter, MatchAny)
classification: KEEP
reason: Same as concepts — protected Q2/U2 field with measured regression.
```

### Tuple-field summary

| classification | fields |
|---|---|
| KEEP (9) | corpus_id, doc_id, chunk_id, parent_id, chunk_type, source_tier*, user_id*, chunk_kind, concepts, entity_ids |
| LAZY_CREATE (1) | language |
| DROP (4) | schema_version, summary_type, source_child_ids, domain |

\* protected with zero current filter use — benchmark-gated, see pilot plan.

---

## Non-tuple fields indexed ad-hoc in all 27 chunk collections

Created by `promote.py::promoted_index_fields()` (also by
`scripts_backfill_promote.py`) and one-off backfills. Static analysis found
**zero Qdrant filter constructions for every one of these fields** across
services, routers, scripts, and MCP.

```yaml
fields:                      # all: collections_indexed 27, filters_found 0
  mechanisms:        {type: keyword, populated_via: promote/backfill_mechanisms}
  key_terms:         {type: keyword, populated_via: promote}
  entity_families:   {type: keyword, populated_via: promote}
  entity_domains:    {type: keyword, populated_via: promote}
  relation_predicates: {type: keyword, populated_via: promote}
  relation_families: {type: keyword, populated_via: promote}
  fact_types:        {type: keyword, populated_via: promote}
  related_entities:  {type: keyword, populated_via: promote}
  graph_neighbors:   {type: keyword, populated_via: promote}
  has_relations:     {type: bool,    populated_via: promote}
  semantic_chunk_type: {type: keyword, populated_via: promote}
  topic_key:         {type: keyword, populated_via: promote}
  neighbor_chunks:   {type: keyword, populated_via: promote}
  graph_degree:      {type: integer, populated_via: promote}
classification: DROP (candidate — pilot-benchmark gated)
reason: >
  Owner-named candidates mechanisms, key_terms, topic_key, graph_neighbors
  all confirmed: no filter construction, no scroll/filter request, no
  count/facet usage, no MCP query use, no migration/backfill dependency on
  the INDEX (backfills set payloads; only backfill_mechanisms additionally
  self-creates the mechanisms index), no planned contract in code. These are
  "future graph-lane filter" speculations. They ride on 77%-populated
  multi-value lists, so they are the dominant share of payload-index disk
  cost. Payloads stay; only indexes are candidates.
```

Note: `promote.py` runs index creation for these 14 fields on every promoted
document's corpus (in-memory cached per worker process) — this is a second,
untracked creation site that the threshold-gated policy must absorb.

---

## Additional findings

1. **Unindexed-but-filtered field:** `funnel_a.py` applies
   `must_not summary_model == ""` (placeholder-summary exclusion) but
   `summary_model` has no payload index anywhere. Candidate for
   MEASURE_FIRST: if the pilot shows this filter costs measurable latency on
   the summary lane, add it to the threshold-gated set. Not urgent — it is a
   must_not on a low-frequency value.
2. **Three independent index creators** exist today:
   `ensure_collections_for_corpus` (tuple, eager, at corpus creation),
   `promote.py::promoted_index_fields` (14 fields, at first promote), and
   one-off scripts (`backfill_child_domain`, `backfill_mechanisms`). Any
   drop/lazy policy must land in ONE shared helper or the dropped indexes
   will be silently recreated.
3. **Schemas collection (`_SCHEMA_PAYLOAD_INDEXES`) is out of scope** of this
   audit — different shape, active filters on kind/corpus_id/doc_id/node_id.

---

## Threshold-gated lazy creation design (for approval; not implemented)

```python
INDEX_CREATION_MIN_POINTS = 100          # provisional — pilot decides
INDEX_CREATION_MIN_POPULATION_RATE = 0.01  # provisional — pilot decides
```

- One shared helper in `qdrant_writer`:
  `ensure_payload_index_if_warranted(client, collection, field)`.
- Gate: `payload_schema[field].points >= MIN_POINTS` AND
  `points / collection.points_count >= MIN_POPULATION_RATE`.
- Idempotent: skip when the field already appears in `payload_schema`.
- Concurrent-worker safe: tolerate "already exists" as success (existing
  `_create_payload_index_with_retry` semantics) + process-local negative
  cache with TTL to avoid re-probing every call.
- Classification drives wiring: KEEP fields remain eagerly created at corpus
  creation (readiness guarantees them); LAZY_CREATE fields go through the
  gate at write time; DROP fields are removed from both creation sites.
- `promote.py` and backfill scripts switch to the shared helper.

## Pilot plan — corpus `65cae4a1` (owner-designated)

Existing state: 3 collections × 710 points, all 29 fields indexed. Small and
disposable — correct pilot size.

**No index mutation happens until the owner approves this procedure.**

1. **Before:** record `GET /collections/{name}` for the 3 collections —
   points, segments, `payload_index` disk bytes (telemetry), full
   `payload_schema`. Run a fixed query battery (funnel_b-style filtered:
   corpus_id + chunk_kind must_not; and unfiltered) 20× each; record
   p50/p95/max, candidate chunk IDs + ordering. Persist to
   `data_eval/qdrant_index_pilot_65cae4a1_before.json`.
2. **Action (pilot only):** `delete_payload_index` on the 3 corpus
   collections for the DROP set: schema_version, summary_type,
   source_child_ids, domain + the 14 non-tuple fields. KEEP/LAZY fields
   untouched. Nothing outside `corpus_65cae4a1_*` is modified.
3. **After:** repeat the battery; compare — disk bytes saved, p50/p95/max
   deltas, and candidate-ID/ordering identity (must be bit-identical for
   filtered and unfiltered queries; any reordering fails the pilot).
4. **Ingest test:** re-ingest one fixture document into the pilot corpus;
   confirm no code path errors on missing indexes (this catches any hidden
   filter dependency the grep missed).
5. **Rollback:** `create_payload_index` for each dropped field (idempotent).

Pilot passes if: disk saving measurable, zero latency regression beyond
noise, zero result-ordering change, zero ingestion errors. Only then is a
fleet-wide change proposed (separate owner decision).

---

## Owner confirmation checklist (owner-named DROP candidates)

| candidate | filter construction | scroll/filter request | count/facet | MCP use | migration/backfill dep | planned contract | verdict |
|---|---|---|---|---|---|---|---|
| domain | none | none | none | none | backfill sets payload; self-creates own index | comment only (M1) | DROP |
| mechanisms | none | none | none | none | backfill self-creates index | none | DROP |
| key_terms | none | none | none | none | none | none | DROP |
| topic_key | none | none | none | none | none | none | DROP |
| graph_neighbors | none | none | none | none | none | none | DROP |

Protected fields verified untouched by any proposed action: corpus_id,
user_id, doc_id, chunk_id, parent_id, chunk_kind, chunk_type (route
eligibility), source_tier (benchmark-gated), concepts, entity_ids.
