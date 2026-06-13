# Entity Dedup / Resolution Pass — Final Hardened Implementation Plan (Polymath v3.3)

Deterministic embedding-merge over the live ~796k-entity Neo4j graph. Collapse surface-form fragments, quarantine true junk, lose zero real edges/facts/mentions, fully reversible, **durable against re-ingest**.

All paths absolute under `/Users/king/polymath_v3.3`. APOC 5.26 and GDS are **confirmed installed** (`docker-compose.yml:92`) — no fallback design needed.

---

## What the adversarial review changed (read this first)

- **The headline case is cross-type.** Live data: `entity:flame` = `Organization`, `entity:flame-engine` = `Software`. A blanket "never cross primary_entity_type" gate makes the pass a **no-op on its own flagship pair**. Type is now an *allow-listed transition* gated behind a hard **shared-`canonical_family` AND shared-neighbor (Jaccard ≥ 0.3)** requirement — the graph signal, not the string suffix, is what legitimately links `flame`↔`flame-engine`.
- **The whole pass was not durable.** Re-ingesting "flame engine" re-derives `entity:flame-engine` and (a) resurrects the tombstone via the unconditional `MERGE...SET` at `neo4j_writer.py:1738`, and (b) re-fragments. Fixed with a **write-back alias collection** read by `_load_alias_lookup` **plus** a tombstone-redirect in the write MERGE. This is mandatory, not optional.
- **The merge Cypher was incorrect and unbounded.** It created *directed* RELATES_TO while every reader is *undirected* (double-counting); it had no transaction-size bound on hub merges (heap-bomb on un-tuned community Neo4j); and undo was not exact (survivor array-widening was unsnapshotted). All three fixed: direction-normalized collapse, `MERGE_DEGREE_CAP` + batched re-point, and full survivor-edge pre-merge snapshot.
- **The junk regex was a data-loss engine.** It would quarantine `1970s`, `8020 rule`, and the **C and R programming languages** (`c`, `r`). Rewritten to exact-noise-only with a hard mention/degree exemption.
- **Dry-run was not side-effect-free.** It persisted `dedup_embedding` onto live nodes. Vectors now live in **Mongo**, never on Entity nodes (also avoids ~3GB store bloat on un-tuned pagecache).
- **Two non-issues removed:** APOC availability (it's installed) and the suffix-strip string rule (`-engine`/`-lib`) — deleted as a false-merge generator (`game`/`game engine`, `search`/`search engine`).

---

## Phase 0 — Prerequisites & invariants (foundation)

### 0a. Neo4j memory config (HARD PREREQUISITE — no apply runs before this)
`docker-compose.yml` has **zero** memory config and runs `neo4j:5-community` with 1.97M nodes already loaded. Add under the neo4j service env:
```yaml
      NEO4J_server_memory_heap_initial__size: '2G'
      NEO4J_server_memory_heap_max__size: '6G'         # size to host RAM
      NEO4J_server_memory_pagecache_size: '4G'         # size to store-on-disk
```
**Verify baseline before touching anything:** `CALL dbms.listConfig() YIELD name, value WHERE name CONTAINS 'memory' RETURN name, value`. Record in the run doc. **Acceptance:** heap_max ≥ 6G live; APOC + GDS confirmed via `RETURN apoc.version()` and `CALL gds.version()`.

### 0b. Schema constraints/indexes — `backend/services/graph/schema.py` (current 14–56)
```cypher
CREATE CONSTRAINT entity_merge_audit_id IF NOT EXISTS
  FOR (m:EntityMergeAudit) REQUIRE m.merge_id IS UNIQUE;
CREATE INDEX entity_tombstone IF NOT EXISTS FOR (e:Entity) ON (e.tombstone);
CREATE INDEX entity_merged_into IF NOT EXISTS FOR (e:Entity) ON (e.merged_into);
CREATE INDEX entity_quarantine IF NOT EXISTS FOR (e:Entity) ON (e.quarantined);
```
Skip `normalized_name` index if already present (cited schema.py:30). **Acceptance:** re-running `schema.py` apply is a no-op.

### 0c. Load-bearing invariants (the decisions everything relies on)
- **Survivor keeps its `entity_id`.** Never mint a new id. Only the minority (duplicate) references get rewritten.
- **Tombstone is NOT a real Entity with the dup's id.** The `entity_id IS UNIQUE` constraint (schema.py:17) + the writer's `MERGE (e:Entity {entity_id})` would resurrect a same-id tombstone. **Decision:** the tombstone is `(:Entity {entity_id: "tombstone:" + <dup_id>, original_entity_id: <dup_id>, merged_into: <survivor_id>, tombstone: true})`. Read-path resolution (Phase 7) looks up `original_entity_id`. This sidesteps the UNIQUE collision and the resurrection-via-SET path entirely.
- **Tombstone, never hard-delete, in v1.** Reversible by construction.
- **Re-ingest durability is a first-class requirement** (Phase 6.5), not an afterthought.

---

## Phase 1 — Candidate generation (blocking — never O(796k²))

**Create:** `backend/services/graph/entity_dedup/blocking.py`

Bucket in Neo4j (indexed), score only within a bucket. **Drive from the MENTIONS corpus_id index** — never scan the Entity label then test corpus via pattern-comprehension (that ignores the schema.py:50 index):

```cypher
// Corpus-scoped, index-driven, paged with SKIP/LIMIT
MATCH (c:Chunk {corpus_id: $corpus_id})-[:MENTIONS]->(e:Entity)
WHERE e.merged_into IS NULL AND e.quarantined IS NULL AND e.tombstone IS NULL
  AND e.normalized_name IS NOT NULL AND e.normalized_name <> ''
WITH DISTINCT e
WITH e,
     head(split(e.normalized_name, ' ')) AS head_token,
     left(e.normalized_name, 3)          AS char3,
     size([ (e)<-[:MENTIONS]-() | 1 ])    AS mention_count,
     size([ (e)-[:RELATES_TO]-() | 1 ])   AS degree
RETURN head_token, char3, e.primary_entity_type AS ptype,
       collect({entity_id: e.entity_id, norm: e.normalized_name, ptype: e.primary_entity_type,
                mentions: mention_count, degree: degree, confidence: e.confidence,
                family: e.canonical_family})[..$bucket_max] AS members
ORDER BY head_token, char3
SKIP $skip LIMIT $page
```

**Buckets (deterministic):**
1. **`(head_token, char3)`** — primary fragment catcher. `flame`/`flame engine` → `("flame","fla")`; `abstract base class`/`...classes` → `("abstract","abs")`. **This bucket carries both probe pairs.**
2. **`canonical_family`** — necessary-but-not-sufficient; promoted to a *required scoring gate* in Phase 2, not relied on for bucketing recall.
3. **Shared-top-neighbor** (degree ∈ `[2, HUB_DEGREE_CAP]`) — semantic backstop.

In Python: split each bucket by `primary_entity_type` only for **same-type** candidate emission; cross-type pairs are emitted **only** when both share a `canonical_family` (Phase 2 gates them hard). Pairs ordered `a.entity_id < b.entity_id` (idempotent).

**Guards:** `BUCKET_MAX = 400` (enforced server-side via `[..$bucket_max]` + logged as `oversized_bucket_truncated` with **entity-coverage count reported as a first-class metric**); `CANDIDATE_PAIR_CAP = 50_000` per run (overflow deferred + logged). Bucket enumeration is **paged** (`SKIP/LIMIT`), processed bucket-by-bucket — never hold all rows + all pairs in Python heap at once.

**Acceptance:** candidate set contains `(entity:flame, entity:flame-engine)` and `(entity:abstract-base-class, entity:abstract-base-classes)`; total scored pairs ≤ cap; oversized buckets truncate-and-report coverage; function never materializes a list > cap (asserted).

---

## Phase 2 — Scoring & merge decision (deterministic, graph-gated)

**Create:** `backend/services/graph/entity_dedup/scoring.py`

A pair is a **merge candidate iff ALL hold** (hard AND):

1. **`round(cosine(emb_a, emb_b), 6) >= 0.95`** (`SIM_THRESHOLD = 0.95`). **Single tier, no 0.92 band.** Rationale: at 0.92 Qwen3 routinely scores co-hyponyms; the only backstop was the deleted string rule. Cosine is rounded to 6 dp before compare to survive MLX/mxfp8 float drift across runs (determinism requirement).
2. **Shared `canonical_family`** — REQUIRED. `flame`(Org) and `flame-engine`(Software) both carry `canonical_family` (the real link). This replaces type as the primary semantic gate.
3. **Shared-neighbor Jaccard ≥ 0.30** over high-confidence (`confidence ≥ 0.55`) RELATES_TO neighbor `entity_id` sets — REQUIRED. Two fragments of the same thing share neighbors; `game`/`game engine` do not.
4. **Type rule (allow-list, NOT blanket-reject):**
   - same `primary_entity_type` → OK; **OR**
   - an **allow-listed transition**: `{Organization↔Software, Concept↔Software, Product↔Software, Concept↔Method}` (covers flame Org↔Software). Any other cross-type pair → reject.
5. **String-compatibility gate** (deterministic, defends against embedding FPs). One of:
   - vetted singular/plural: strip trailing `s`/`es` **only if** the result is a real prefix of the other; **OR**
   - token-set equality after stop-token removal (`abstract base class` ≡ `class abstract base`); **OR**
   - edit-distance ≤ 1 **only when** `min(len) ≥ 7` (kills `go`/`no`, `case`/`base`, `java`/`lava` over-merges on short surfaces).
   - **The `-engine`/`-lib`/`-framework`/`-class` suffix-strip clause is DELETED** (false-merge engine).

**Embedding source:** on-the-fly via MLX 8082 (`embedder.embed_batch`, batch 32). **Vectors persisted in Mongo `graph_entity_dedup_vectors` keyed `{entity_id, embedding_model_id, content_sig}`** — NOT on Entity nodes (dry-run must be node-side-effect-free; avoids ~3GB store bloat). Pre-filter candidate union against already-cached vectors so steady-state runs embed ~0. `EMBED_PER_RUN_CAP = 20_000` independent of pair cap; overflow deferred. Embed loop runs **inside the Phase-4 isolated thread with its own httpx client**, never the request-loop client. Embed text = `f"{canonical_name}. {definitional_phrase[:200]}"`; emit embed wall-clock to the run doc.

**Output:** sorted `MergeProposal{survivor_id, duplicate_id, cosine, family, jaccard, type_transition, rule_fired}`.

**Acceptance:** `flame`/`flame-engine` pass (shared family + Jaccard + allow-listed Org↔Software) → proposed; `abstract base class(es)` pass (same type Concept) → proposed; `game`/`game engine` rejected (no shared family/neighbors); `flame`/`flamingo` rejected (string gate); byte-identical re-run from cached vectors (determinism test).

---

## Phase 3 — The MERGE operation (apply + exact undo)

**Create:** `backend/services/graph/entity_dedup/merge.py`

### Survivor selection (deterministic tiebreak order)
1. higher `mention_count` → 2. higher `confidence` → 3. shorter `canonical_name` → 4. lexicographically smaller `entity_id`.

### Safety rejects before any write
- **`MERGE_DEGREE_CAP = 1000`**: skip+log any dup with `degree(dup) + mentions(dup) > cap` (no single unbounded transaction in v1).
- **Hub-into-leaf reject:** if `degree(dup) > HUB_DEGREE_CAP (=200)` AND `degree(survivor) < 0.1 * degree(dup)` → reject.
- Both nodes live, neither tombstoned, `sv <> dup`.

### APPLY (one transaction per merge; MENTIONS/RELATES_TO re-point internally batched at LIMIT 5000)

**Step A — audit BOTH sides (this is what makes undo exact):**
```cypher
MATCH (sv:Entity {entity_id: $survivor_id}), (dup:Entity {entity_id: $dup_id})
WHERE sv.merged_into IS NULL AND dup.merged_into IS NULL AND sv <> dup
// snapshot dup's full edge set
CALL { WITH dup
  MATCH (dup)-[r]-(other)
  RETURN collect({ rel_type: type(r),
                   direction: CASE WHEN startNode(r)=dup THEN 'out' ELSE 'in' END,
                   other_id: coalesce(other.entity_id, other.chunk_id, other.fact_id),
                   other_label: head(labels(other)),
                   props: properties(r) }) AS dup_edges }
// snapshot survivor edges that an ON MATCH widen will mutate (keyed by other_id+predicate+type)
CALL { WITH sv
  MATCH (sv)-[r]-(other)
  RETURN collect({ rel_type: type(r),
                   predicate: r.predicate,
                   other_id: coalesce(other.entity_id, other.chunk_id, other.fact_id),
                   props: properties(r) }) AS sv_edges }
CREATE (m:EntityMergeAudit {
  merge_id: $merge_id, run_id: $run_id, created_at: $ts,
  survivor_id: $survivor_id, dup_id: $dup_id,
  dup_props: properties(dup), dup_labels: labels(dup),
  dup_edges: apoc.convert.toJson(dup_edges),
  sv_edges:  apoc.convert.toJson(sv_edges),
  undone: false })
```

**Step B — RELATES_TO re-point, DIRECTION-NORMALIZED (fixes the double-count blocker).** Every reader is undirected, so we must NOT create antiparallel pairs. For each dup RELATES_TO edge, check both orientations on the survivor before creating:
```cypher
WITH sv, dup
MATCH (dup)-[r:RELATES_TO]-(x:Entity)
WHERE x <> sv AND x <> dup          // x<>sv kills self-loops on BOTH directions
WITH sv, dup, x, r,
     CASE WHEN startNode(r)=dup THEN 'out' ELSE 'in' END AS dir
// fold into an existing survivor edge of same predicate in EITHER orientation
OPTIONAL MATCH (sv)-[fwd:RELATES_TO {predicate: r.predicate}]->(x)
OPTIONAL MATCH (x)-[bwd:RELATES_TO {predicate: r.predicate}]->(sv)
WITH sv, dup, x, r, dir, coalesce(fwd, bwd) AS existing
CALL apoc.do.when(existing IS NOT NULL,
  // ON MATCH: widen arrays + max confidence on the existing edge
  'SET existing.confidence = CASE WHEN r.confidence > coalesce(existing.confidence,0) THEN r.confidence ELSE existing.confidence END,
       existing.eligible_for_synthesis = coalesce(existing.eligible_for_synthesis,false) OR coalesce(r.eligible_for_synthesis,false),
       existing.evidence_chunk_ids = apoc.coll.toSet(coalesce(existing.evidence_chunk_ids,[]) + coalesce(r.evidence_chunk_ids,[])),
       existing.evidence_doc_ids   = apoc.coll.toSet(coalesce(existing.evidence_doc_ids,[])   + coalesce(r.evidence_doc_ids,[])),
       existing.evidence_phrases   = apoc.coll.toSet(coalesce(existing.evidence_phrases,[])   + coalesce(r.evidence_phrases,[])),
       existing.source_predicates  = apoc.coll.toSet(coalesce(existing.source_predicates,[])  + coalesce(r.source_predicates,[])),
       existing.corpus_ids         = apoc.coll.toSet(coalesce(existing.corpus_ids,[])         + coalesce(r.corpus_ids,[]))
   RETURN 0',
  // ON CREATE: create in dup''s original orientation, stamp created_by_merge for exact undo
  'WITH sv, x, r, dir
   CALL apoc.create.relationship(CASE dir WHEN "out" THEN sv ELSE x END, "RELATES_TO",
        apoc.map.merge(properties(r), {created_by_merge: $merge_id}),
        CASE dir WHEN "out" THEN x ELSE sv END) YIELD rel RETURN 0',
  {sv:sv, x:x, r:r, dir:dir, existing:existing, merge_id:$merge_id}) YIELD value
DELETE r
```
Then replicate the **writer's weak-edge prune** invariant (neo4j_writer.py:1865-1873) so the merge leaves the graph in the shape the writer would: after collapse, `MATCH (sv)-[weak:RELATES_TO {predicate:'related_to'}]-(x) WHERE EXISTS {(sv)-[s:RELATES_TO]-(x) WHERE s.predicate <> 'related_to'} DELETE weak`.

**Step C — HAS_FACT re-point** (batched if many): `MATCH (dup)-[hf:HAS_FACT]->(f) MERGE (sv)-[:HAS_FACT]->(f) DELETE hf`. Fact nodes keep their original `subject`/`fact_id` (provenance — do NOT rewrite `fact_id`; it's content-hashed). Verified safe: fact retrieval traverses `(e)-[:HAS_FACT]->(f)`, not `f.subject` string-equality (Phase 8 asserts this).

**Step D — MENTIONS re-point, BATCHED at LIMIT 5000** (the heap-bomb fix):
```cypher
CALL apoc.periodic.commit(
 "MATCH (c:Chunk)-[mm:MENTIONS]->(dup:Entity {entity_id:$dup_id}) WITH c,mm LIMIT 5000
  MATCH (sv:Entity {entity_id:$sv_id})
  MERGE (c)-[nm:MENTIONS]->(sv)
    ON CREATE SET nm = apoc.map.merge(properties(mm), {created_by_merge:$merge_id})
    ON MATCH  SET nm.confidence = CASE WHEN mm.confidence > coalesce(nm.confidence,0) THEN mm.confidence ELSE nm.confidence END
  DELETE mm RETURN count(*)", {dup_id:$dup_id, sv_id:$survivor_id, merge_id:$merge_id, limit:5000})
```

**Step E — fold identity metadata** (additive; cap `query_aliases` at survivor-consistent policy, see Open Q): `sv.query_aliases = apoc.coll.toSet(coalesce(sv.query_aliases,[]) + coalesce(dup.query_aliases,[]) + [dup.canonical_name])`, observed types union, max confidence. Also stamp `sv.merge_run_id`, and on the tombstone store `merge_cosine`, `merge_family`, `merge_jaccard`, `merge_rule` so an operator inspecting Neo4j can see *why*.

**Step F — tombstone** (distinct id; NOT the dup's id):
```cypher
WITH sv, dup
DETACH DELETE dup
CREATE (t:Entity {entity_id: 'tombstone:' + $dup_id, original_entity_id: $dup_id,
                  merged_into: $survivor_id, tombstone: true, merged_run_id: $run_id,
                  merge_cosine:$cosine, merge_family:$family, merge_jaccard:$jaccard})
```

`merge_id = sha256(survivor_id|dup_id|run_id)` (UNIQUE-constrained → duplicate apply fails closed). Re-run is a no-op (guard rejects already-merged).

### UNDO (exact, executable — fixes the pseudocode + array-widen blockers)
```cypher
MATCH (m:EntityMergeAudit {merge_id: $merge_id, undone: false})
// 1. recreate dup with its original id (tombstone uses tombstone:id, so no collision)
MATCH (t:Entity {entity_id: 'tombstone:' + m.dup_id}) DETACH DELETE t
CREATE (dup:Entity) SET dup = m.dup_props
WITH m, dup, apoc.convert.fromJson(m.dup_edges) AS dedges, apoc.convert.fromJson(m.sv_edges) AS sedges
// 2. recreate every dup edge by per-label endpoint MATCH (no pseudocode)
UNWIND dedges AS e
CALL apoc.do.case([
  e.other_label='Entity', 'MATCH (o:Entity {entity_id:e.other_id}) RETURN o',
  e.other_label='Chunk',  'MATCH (o:Chunk  {chunk_id:e.other_id})  RETURN o',
  e.other_label='Fact',   'MATCH (o:Fact   {fact_id:e.other_id})   RETURN o'],
  'RETURN null AS o', {e:e}) YIELD value
WITH m, dup, sedges, e, value.o AS other WHERE other IS NOT NULL
CALL apoc.create.relationship(
  CASE e.direction WHEN 'out' THEN dup ELSE other END, e.rel_type, e.props,
  CASE e.direction WHEN 'out' THEN other ELSE dup END) YIELD rel
// 3. delete ONLY edges this merge created on the survivor
WITH m, sedges
MATCH (:Entity {entity_id: m.survivor_id})-[r {created_by_merge: m.merge_id}]-() DELETE r
// 4. restore survivor edges this merge WIDENED (the array/confidence rollback)
WITH m, sedges
UNWIND sedges AS se
CALL apoc.do.case([
  se.rel_type='RELATES_TO', 'MATCH (:Entity {entity_id:$sid})-[r:RELATES_TO {predicate:se.predicate}]-(o) WHERE coalesce(o.entity_id,o.chunk_id,o.fact_id)=se.other_id SET r = se.props RETURN 0',
  se.rel_type='MENTIONS',   'MATCH (c)-[r:MENTIONS]->(:Entity {entity_id:$sid}) WHERE c.chunk_id=se.other_id SET r = se.props RETURN 0',
  se.rel_type='HAS_FACT',   'MATCH (:Entity {entity_id:$sid})-[r:HAS_FACT]->(f) WHERE f.fact_id=se.other_id RETURN 0'],
  'RETURN 0', {sid:m.survivor_id, se:se}) YIELD value
SET m.undone = true, m.undone_at = timestamp()
```

**Acceptance:** parallel/antiparallel same-predicate RELATES_TO collapse to ONE undirected-equivalent edge with union'd evidence + max confidence; zero self-loops; no Fact loses its sole HAS_FACT; re-run no-op; **merge→undo yields a graph isomorphic to pre-merge (exact node/edge/prop equality)** — the gate test in Phase 10.

---

## Phase 4 — Where it runs (isolated background pass + routes)

**Create:** `backend/services/graph/entity_dedup/pass.py`; **Touch:** `backend/routers/graph.py` (routes near `discovery_router`, 1692+; task registry like `_CACHE_REBUILD_TASKS` 1660).

- **Isolation:** copy `analytics._compute_metrics_isolated` exactly (analytics.py:816) — `asyncio.to_thread` → private `asyncio.run` → own `AsyncIOMotorClient` + own `AsyncGraphDatabase.driver`, closed in `finally`. Embed + apply + measure all run in this thread. Never the request loop.
- **Slot throttling:** `admission.try_acquire_ingest_slot()` in `finally`. **Confirm the ingest semaphore is a single GLOBAL pool, not per-corpus** (Open Q) — because `entity_id` is global, a corpus-A dedup mutates entities shared with corpus B; a per-corpus slot would let a corpus-B ingest race the merge. **Release/re-acquire between the embed phase and the apply phase** so a 20-min embed doesn't hold the slot through apply.
- **Modes:** `dry_run=true` (default) = Phase 1+2 + audit-projection only, **zero Neo4j mutation incl. properties** (vectors → Mongo). Preview doc → `graph_entity_dedup_preview {corpus_id, run_id, corpus_change_signature, proposals[], quarantined[], stats{}, reviewed_at, reviewed_by}`. `apply=true` requires `reviewed:true` + matching stored preview `run_id`.
- **Routes:**
  - `POST /api/graph/entity-dedup/{corpus_id}/preview` → `{run_id}`
  - `POST /api/graph/entity-dedup/{corpus_id}/apply` `{run_id, reviewed:true}` → `{applying, run_id}` (409 if not reviewed)
  - `GET  /api/graph/entity-dedup/{corpus_id}/status` → mirrors `_resolve_ingest_progress` (ingestion.py:363)
  - `POST /api/graph/entity-dedup/undo` `{run_id}` → `{undone:N}`

**Acceptance:** dry-run mutates **zero** Neo4j nodes/edges **and zero node properties** (assert property-hash equality, not just counts); apply blocks without reviewed preview (409); pass never starves `/health`.

---

## Phase 5 — Safety rails (deterministic, evidence-aware)

In `scoring.py`/`merge.py`/`pass.py`:

1. **Confidence floor:** skip dup with `confidence < 0.55` (matches synthesis floor, neo4j_writer.py:514).
2. **Type allow-list** (Phase 2.4) — no free cross-type.
3. **Hub-into-leaf reject** + **`MERGE_DEGREE_CAP=1000`** (Phase 3).
4. **Junk quarantine — exact-noise-only WITH evidence exemption (rewritten):**
   - Quarantine iff `normalized_name =~ '^[0-9]+$'` OR `=~ '^[\W_]+$'` OR `= ''` — **AND** `mention_count <= 2` **AND** `degree <= 1`.
   - **Hard EXEMPTION (never quarantine):** `mention_count >= 5` OR `primary_entity_type IN {Software, Method, Standard, TimeReference, Rule, Event}`. This protects `c`(2890), `r`(307), `1970s`(328), `8020 rule`(55).
   - **Reuse `entity_stoplist.json`** (it exists) as the seed noise set — union with the digit pattern, cite provenance in `quarantined[]`. Do not hand-roll a parallel list.
   - **Quarantine flips ALSO trigger Phase 6 cache invalidation** even on a preview/quarantine-only run (otherwise junk stays visible in cached metrics).
   - **[GAP]** eyeball the live sample first: `MATCH (e:Entity) WHERE e.normalized_name =~ '^[0-9].*' RETURN e.normalized_name, e.primary_entity_type, size([(e)<-[:MENTIONS]-()|1]) AS m ORDER BY m DESC LIMIT 200`.
5. **Per-run merge cap:** `MERGE_CAP=2000` (body-configurable, hard max 5000); **cross-type and string-rule-fired merges capped at ≤50 in the first run**, each requiring explicit per-item approval.
6. **Human-audit gate:** apply requires `reviewed:true` + stored preview. Review UI shows **global blast radius** — all `corpus_ids` on affected edges + the type transition — so a reviewer approving for corpus A sees it rewires corpus B.

**Acceptance:** `0`/`0 factor` quarantined; `c`/`r`/`1970s`/`8020 rule` **never** quarantined; synthetic hub→leaf rejected; apply without reviewed preview → 409.

---

## Phase 6 — Cache / metrics invalidation (post-apply AND post-quarantine)

**Touch:** `pass.py` post-mutation hook; reuse `analytics.py` + `routers/graph.py`.

Merge/quarantine change cardinality but NOT `corpus_change_signature` (keyed on doc updated_at, analytics.py:569) → all signature-keyed caches go stale. Run once at end (debounced — see Phase 9):
```python
import re
await db["graph_metrics_cache"].delete_one({"corpus_id": cid})
await db["graph_domain_cache"].delete_one({"corpus_id": cid})
await db[ANCHOR_CACHE_COLLECTION].delete_many({"corpus_id": cid})   # not auto-rebuilt
await db["graph_brain_view_cache"].delete_many({"key": {"$regex": re.escape(cid)}})  # escaped
# force rebuild — repopulates metrics fresh (kills dead-id entity_betweenness/pagerank)
await emerge_domains(qdrant, neo4j, db, cid, force=True)   # analytics.py:688/809
```
**GDS projection check:** `CALL gds.graph.list()` → `gds.graph.drop` any named projection touching the corpus before rebuild (stale topology otherwise). `_compute_metrics_isolated` loads fresh from Neo4j so the NetworkX path is safe, but a persisted GDS projection is not.

**Ingest-race guard:** defer rebuild via `should_defer_warmup_for_active_ingest` (graph.py:1669).

**Acceptance:** `metrics_cache:"warming"`→`"ready"`; `node_count` reflects reduced count; no `entity_betweenness` references a tombstoned id; brain-view rebuild count logged.

---

## Phase 6.5 — Re-ingest durability (THE critical omission from the draft)

Without this, the next ingest of "flame engine" resurrects the tombstone and re-fragments — the entire pass is undone. Two mandatory mechanisms:

**a) Alias write-back.** **Create** Mongo `graph_entity_dedup_aliases {dup_normalized_name → survivor_canonical_name, run_id}` on apply. **Touch** `neo4j_writer._load_alias_lookup` (530) to merge this collection on top of `entity_aliases.json`, so `canonicalize_entity_name("flame engine") → "flame"` → `entity_id_from_name` yields `entity:flame` on the next ingest. `_load_alias_lookup` is **not** `@lru_cache`'d (verified — it re-reads each call), so no in-process cache invalidation needed for the file path; but the Mongo merge must be read fresh per ingest batch (or cached with a short TTL keyed on a version counter).

**b) Tombstone-redirect in the write MERGE.** **Touch** `neo4j_writer.py:1738`. Before the MERGE, redirect any id that points at a tombstone:
```cypher
UNWIND $rows AS row
OPTIONAL MATCH (t:Entity {entity_id: 'tombstone:' + row.entity_id})
WITH row, coalesce(t.merged_into, row.entity_id) AS eid
MERGE (e:Entity {entity_id: eid})
ON CREATE SET e.first_seen = timestamp()
SET e.normalized_name = row.normalized_name, ...   // unchanged body
```
Belt-and-suspenders with (a): even if a surface slips the alias map, the write lands on the survivor, never on a resurrected node.

**Acceptance (durability gate — Phase 10 test):** merge dup→survivor, write alias, simulate `write_document_graph` for a chunk mentioning the dup surface → the **survivor** id is hit, NO live `entity:<dup>` node exists (only `tombstone:<dup>`).

---

## Phase 7 — Downstream referential integrity (tombstone-follow)

The tombstone now has id `tombstone:<dup_id>` with `original_entity_id=<dup_id>`. Read paths resolving a historical dead id must follow it:

1. **`backend/services/graph/orchestrator.py:6061`** (stored-packet enrichment). The dead id is the dup's *original* id, now living only as `original_entity_id` on the tombstone. Two-step resolve:
   ```cypher
   OPTIONAL MATCH (t:Entity {entity_id: 'tombstone:' + triple.s})
   WITH triple, coalesce(t.merged_into, triple.s) AS sid
   MATCH (a:Entity {entity_id: sid})-[r:RELATES_TO]-(b:Entity)  // UNDIRECTED, matches reader
   ...
   ```
   Note: edge match must be **undirected** (the merge direction-normalized, so the directed `{predicate}` edge may have flipped).
2. **`backend/services/retriever/graph_decoration.py:317-318`** — resolve `seed_entity_id`/`neighbor_entity_id` through the same tombstone lookup.
3. **Mongo `graph_sessions`** — immutable historical snapshots; do NOT rewrite. The read-path tombstone-follow self-heals them.
4. **Qdrant** — no action (chunk payloads carry no `entity_id`, funnel_a.py:129; no entity collection). Note in run doc to revisit if an entity vector collection is ever added.
5. **In-process caches** (`context_manager`, hydrated `GraphDecoration`): the tombstone-follow only helps on Neo4j *read*. Any objects already hydrated in the current backend uptime hold the dead id until evicted — acceptable (bounded by uptime; Phase 6 invalidates the persisted caches).

**Acceptance:** a pre-merge session resumed post-merge hydrates the survivor's enrichment; a decoration lookup on a dead id resolves to the survivor's betweenness/pagerank.

---

## Phase 8 — Measurement (before/after on live graph)

**Create:** `backend/services/graph/entity_dedup/measure.py` (emits to run doc).

| Metric | Cypher |
|---|---|
| Live entity count | `MATCH (e:Entity) WHERE e.merged_into IS NULL AND e.quarantined IS NULL AND e.tombstone IS NULL RETURN count(e)` |
| Fragmentation rate | fraction of entities sharing a `(head_token,char3,ptype)` bucket with ≥1 other |
| `related_to` share | `MATCH ()-[r:RELATES_TO]->() RETURN sum(CASE WHEN r.predicate='related_to' THEN 1 ELSE 0 END)*1.0/count(r)` (baseline 13.9% — must NOT rise) |
| `eligible_for_synthesis` | `MATCH ()-[r:RELATES_TO]->() WHERE r.eligible_for_synthesis RETURN count(r)` (baseline 700,663 — modest rise OK) |
| Fact count | `MATCH (:Fact) RETURN count(*)` (must be UNCHANGED) |
| Self-loop count | `MATCH (e)-[:RELATES_TO]->(e) RETURN count(*)` (must NOT rise — direction-normalize guard) |

**Probe set:** `entity:flame` survives, `tombstone:flame-engine` has `merged_into:"entity:flame"`; `entity:abstract-base-class` survives, `...classes` tombstoned into it.
**Fact reconciliation:** assert fact retrieval for the survivor returns the dup's facts (proves HAS_FACT traversal-sufficiency; no path relies on `f.subject`).
**A/B:** run the ~20-question QA eval pre/post (target equal-or-better grounding); A/B `compute_cluster_pair_gaps` (analytics.py:2422) `terminological_gaps` count (merging fragments should reduce false gaps). **[GAP]** confirm QA harness path before wiring.

**Acceptance:** entity count strictly decreases; fact count unchanged; `related_to` share flat-or-down; self-loops not increased; probes pass; QA not worse.

---

## Phase 9 — Rollout

1. Pick ONE small sub-corpus (lowest `doc_count`; the 523-doc set is the whole graph — do not start there). Run **preview**.
2. **Human review:** spot-check 30 proposals across the type-transition and same-type sets + the full `quarantined[]`; set `reviewed_at`. Cross-type/string-rule proposals require per-item approval.
3. **Apply** with `MERGE_CAP=500`, cross-type cap ≤50. Run Phase 8.
4. **Verify** probes + QA + cache rebuild + durability (simulate a re-ingest of one merged surface).
5. **Undo the first run** to prove the safety net before scaling, then re-apply.
6. **Expand** per corpus, raising `MERGE_CAP` to 2000. In a multi-corpus loop, **coalesce `emerge_domains(force=True)` to a single rebuild after the LAST corpus** (it's the heaviest op in the system, analytics.py:809 — never N times in a loop).
7. **Never** apply across all corpora in one shot pre-measurement.

**Acceptance:** first corpus shows reduced fragmentation + green QA + successful re-ingest-durability check before any second corpus; undo restores exact pre-merge counts.

---

## Phase 10 — Test plan

**Create:** `test_entity_dedup_blocking.py`, `test_entity_dedup_scoring.py`, `test_entity_dedup_merge.py`, `test_entity_dedup_integration.py`.

- **Blocking:** probe pairs co-bucket; oversized bucket truncates+reports coverage; pair list ≤ cap; index-driven query (no Entity-label scan).
- **Scoring:** hard-AND (high cosine + no shared family → reject; high cosine + shared family + Jaccard<0.3 → reject; `game`/`game engine` → reject; `flame`/`flame-engine` Org↔Software allow-listed → accept). Determinism from cached vectors (byte-identical). Junk quarantine exemptions (`c`,`r`,`1970s` never quarantined).
- **Merge (throwaway test Neo4j):**
  - **Reversibility (THE gate):** seed graph incl. an **antiparallel same-predicate pair**, a **dup↔survivor direct edge**, a **shared Fact**, and a **widened-array survivor edge** → snapshot → apply → undo → assert **isomorphic (exact per-edge prop equality)**. Gate the whole feature on this passing, ideally on an *exported copy of the real subgraph* for each batch, not only a synthetic graph.
  - **Idempotency:** apply twice → second no-op.
  - **Conservation:** no Fact loses sole HAS_FACT; antiparallel collapse → one edge, union'd evidence, max confidence; zero self-loops.
  - **Tombstone:** id is `tombstone:<dup>`, has `original_entity_id`, zero relationships.
- **Orphan-cleanup (highest-risk untested interaction):** seed corpus + tombstone, call `delete_document_graph` AND `delete_corpus_graph` (the real callers at 1327/1356), assert **tombstone survives** (proves the `AND e.tombstone IS NULL` guard + batching landed).
- **Re-ingest durability:** merge + alias write + simulated `write_document_graph` of the dup surface → survivor hit, no live dup node.
- **Integration:** ~30-node graph, 2 corpora (cross-corpus `corpus_ids` union), full preview→apply→measure→undo; assert orchestrator/decoration tombstone-follow resolves a dead id.

Note: the orphan-cleanup edit at neo4j_writer.py:1203 must **also be batched** (it's currently an unbounded `MATCH (e:Entity)...DETACH DELETE e` over 796k nodes in one tx):
```cypher
CALL apoc.periodic.commit(
 "MATCH (e:Entity) WHERE e.tombstone IS NULL AND NOT EXISTS {(:Chunk)-[:MENTIONS]->(e)}
  WITH e LIMIT 10000 DETACH DELETE e RETURN count(*)", {limit:10000})
```

---

## Consolidated file manifest

**Create:**
- `backend/services/graph/entity_dedup/__init__.py`
- `.../entity_dedup/blocking.py` (P1), `scoring.py` (P2), `merge.py` (P3), `pass.py` (P4), `measure.py` (P8)
- 4 test files (P10)

**Touch:**
- `docker-compose.yml` — Neo4j heap/pagecache (**P0a, prerequisite**)
- `backend/services/graph/schema.py` — constraints/indexes (P0b; 14–56)
- `backend/routers/graph.py` — 4 routes + `_DEDUP_BG_TASKS` (P4; 1660/1692)
- `backend/services/graph/orchestrator.py:6061` — undirected tombstone-follow (P7)
- `backend/services/retriever/graph_decoration.py:317-318` — tombstone-follow (P7)
- `backend/services/graph/neo4j_writer.py:1203-1211` — `tombstone IS NULL` guard **+ batch** (P0c/P10)
- `backend/services/graph/neo4j_writer.py:1738` — tombstone-redirect before write MERGE (**P6.5b, mandatory**)
- `backend/services/graph/neo4j_writer.py:530` `_load_alias_lookup` — merge `graph_entity_dedup_aliases` (**P6.5a, mandatory**)

**Reuse unchanged (must match for re-ingest safety):** `entity_id_from_name` (825), `canonicalize_entity_name` (557), `normalize_entity_name` (521), `fact_id_from_parts` (839, do NOT rewrite), `embedder.embed_batch` (98), `analytics.emerge_domains` (688), `compute_corpus_change_signature` (569), `admission.try_acquire_ingest_slot`.

**New Mongo collections:** `graph_entity_dedup_preview`, `graph_entity_dedup_runs`, `graph_entity_dedup_vectors`, `graph_entity_dedup_aliases`.

---

## Decision forks (RECOMMENDATIONS)

- **Blocking strategy — bespoke `(head_token,char3)` buckets vs GDS `nodeSimilarity`/`knn`.** **RECOMMENDATION: bespoke buckets for v1, GDS as the scale path.** Bespoke is deterministic, debuggable, and provably carries both probe pairs; GDS is the documented fallback if oversized-bucket skip-rate gets high. Rationale: determinism + auditability now beat the (real) elegance of GDS, which is already on the stack and can be swapped in later.
- **Embedding source — on-the-fly vs precomputed entity vectors.** **RECOMMENDATION: on-the-fly, cached in Mongo `graph_entity_dedup_vectors`, never on Entity nodes.** Rationale: no entity vectors exist; node-side storage breaks dry-run side-effect-freedom and bloats the un-tuned store ~3GB.
- **Cosine threshold.** **RECOMMENDATION: single tier at 0.95, rounded to 6 dp, gated behind shared-family + Jaccard≥0.3.** Rationale: 0.92 over-merges co-hyponyms and the only backstop was the deleted string rule; 0.95 + graph-overlap is the defensible floor.
- **Survivor selection.** **RECOMMENDATION: mention-count → confidence → shorter canonical_name → smaller entity_id.** Rationale: keeps the highest-evidence id (fewest references to rewrite) and yields the base lemma as the public survivor.
- **Offline-batch vs on-write.** **RECOMMENDATION: offline batch pass (preview→review→apply), PLUS a thin on-write tombstone-redirect + alias map for durability.** Rationale: cross-KB resolution needs the global view a batch gives; the on-write hooks only prevent re-fragmentation, they don't do resolution.

---

## Open questions for the user (genuine forks only)

1. **Ingest semaphore scope** — is `admission.try_acquire_ingest_slot()` a single global pool or per-corpus? Apply mutates *global* entities; if per-corpus, a corpus-B ingest can race a corpus-A merge → need an explicit global write-lock for apply.
2. **QA eval harness location** (~20 questions) — confirm path before Phase 8 wiring (likely `CONTINUITY/` or `backend/tests/eval_*`).
3. **Type-transition allow-list** — confirm `{Organization↔Software, Concept↔Software, Product↔Software, Concept↔Method}` is the right set, or tighten/loosen it. This is the single most consequential correctness knob.
4. **Survivor `query_aliases` cap** — ingest path caps at 5 (ghost_b_local.py:320); merge union is uncapped. Cap survivors at 5 too, or document an uncapped-survivor exception?
5. **Fact `subject` provenance** — leave merged facts carrying the dup's `subject` string (historical, recommended) vs reconcile to survivor (changes nothing in traversal, risks fact_id confusion)?

---

## Effort / sequence estimate

- **P0 prereqs (memory config + schema + APOC/GDS verify):** 0.5 day. *Gate: nothing else proceeds without heap config live.*
- **P1 blocking + P2 scoring (+ Mongo vector cache):** 1.5 days.
- **P3 merge apply+undo (the hard part — direction-normalize, batched re-point, exact undo):** 2 days.
- **P4 pass/routes/isolation + P5 rails:** 1 day.
- **P6 cache invalidation + P6.5 durability (alias write-back + write-MERGE redirect):** 1 day.
- **P7 tombstone-follow + P8 measure:** 0.5 day.
- **P10 tests (reversibility isomorphism + orphan-cleanup + durability are the must-pass gates):** 1.5 days.
- **P9 rollout on first corpus (preview→review→apply→undo→re-apply):** 0.5 day active + review wall-time.

**Total: ~8.5 engineering days** (up from the draft's "1-2 days" — the durability layer, exact undo, batched/bounded merges, and the graph-signal gate are real work the original estimate omitted). **Critical path:** P0 → P3 → P6.5 → P10 reversibility+durability gates. Do not run a single apply until the P10 isomorphism, orphan-cleanup, and re-ingest-durability tests are green.
