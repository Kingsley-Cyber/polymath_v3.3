# Schema vs Metadata — The Complete Map (re-architecture foundation)

**Date:** 2026-07-02 · **Method:** 4 parallel code sweeps (extraction shapes · parent/child+Qdrant
payload · Neo4j · facets/summary/filtering), every claim file:line-verified, reconciled against
EXTRACTION_VS_METADATA.md + REBUILD_IMPLEMENTATION.md. This is the map to re-architect against.

---

## 1. THE CONCEPT — schema ≠ metadata, four planes

- **Schema** = the *declared contract*: what shape data promises to have (Pydantic models,
  versioned envelopes, Neo4j constraints, payload key lists).
- **Metadata** = the *actual field values at rest* that the retriever can use to store, filter,
  route, rank, cite, hydrate.
- A system is healthy when every metadata field is governed by a schema AND every schema field is
  populated AND consumed. Polymath violates all three directions in places — that's the gap space.

**The four planes every field must cross:**

| Plane | What lives here | Polymath today |
|---|---|---|
| **P1 Contract** (schema) | Pydantic models, envelope versions, DB constraints | ~150 models; `polymath.extract.v1`; `polymath.facets.v1`; `graph/schema.py` constraints — but the STORAGE shapes are untyped dicts |
| **P2 Extraction** (produce) | Ghost A (summary/domain/topics) · Ghost B local+cloud (entities/relations/facts) · facets normalizer · docling parse | Rich; local+cloud already converge on one envelope |
| **P3 Storage** (at rest) | Mongo `parent_chunks`/`chunks`/`documents.facet_profile`/`ghost_b_extractions` · Qdrant payload (10 indexed keys) · Neo4j nodes/edges | Graph rich; vector payload thin |
| **P4 Consumption** (use) | Qdrant filters · rank signals (C3/B4) · coverage lanes · hydration joins · prompt context · citations | Consumes only: corpus/doc/chunk ids, chunk_type, chunk_kind, domain, language, heading_path(rank), facets(in-memory) |

**A field is real only if it crosses all four planes.** Most extraction output stops at P3-graph.

---

## 2. THE SCHEMAS THAT EXIST (P1 inventory)

| Contract | Version | Typed? | Governs |
|---|---|---|---|
| `ExtractionResult` (+ LLMEntity/LLMRelation/LLMFact) | `polymath.extract.v1` | ✅ Pydantic | both local GLiNER/GLiREL and all 3 cloud wire modes converge here; keyed (corpus,doc,chunk) in `ghost_b_extractions` |
| Facet profile / `semantic_facets` | `polymath.facets.v1` | ⚠️ half — builder typed-ish, outputs `dict[str,Any]` | doc/parent/child facet ids+text, content facets + confidence |
| Neo4j graph | constraints in `graph/schema.py` | ✅ (Cypher constraints + 20+ indexes) | Entity (ontology-enriched) / RELATES_TO (provenance arrays) / Fact / MENTIONS / Document / Chunk |
| Mongo parent/child docs | — | ❌ **untyped dict builders** (`worker.py:1173` `:1222`) | the PRIMARY chunk store has no Pydantic contract |
| Qdrant point payload | — | ❌ **untyped dict** (`qdrant_writer.py:597`); indexed keys listed at `:217` | the PRIMARY filter surface has no schema |
| `SourceChunk` (response) | — | ✅ Pydantic | what chat/citations see; `metadata: dict` free-for-all inside |

**Structural finding #1 — split-brain contracts:** typing is strong at the edges (extraction in,
API out) and absent exactly at the storage boundary where metadata is born. Drift between the two
dict builders and the payload index list is undetectable by construction.

---

## 3. THE GAP MATRIX (field-by-field truth test)

Legend: Declared (in any schema) / Populated (values at rest) / Indexed (filterable) / Consumed
(query path reads it). **Verdict names the failure.**

| Field | Declared | Populated | Indexed | Consumed | Verdict |
|---|---|---|---|---|---|
| `corpus_id`/`doc_id`/`chunk_id`/`parent_id` | ✅ | ✅ | ✅ all stores | ✅ isolation/join/hydration | **HEALTHY** — the identity spine |
| `chunk_kind` | ✅ | ✅ | ✅ | ✅ NOISY_KINDS filter + C3 penalty | **HEALTHY** |
| `language` | ✅ | ✅ code lane | ✅ | ✅ code scoping | **HEALTHY** |
| `summary` (parent) | ✅ | ✅ (post-backfill) | own vector point | ✅ child_summary hydration + Funnel A | **HEALTHY** |
| `heading_path` | ✅ | ✅ | ❌ | ✅ C3 + rank text + prompt | OK (rank-signal, not filter — fine) |
| `domain` | ✅ | ⚠️ backfilled; None on old parents | ✅ | ✅ soft boost | **PARTIAL** — the one ad-hoc promotion that shipped (M1) |
| `topics` (Ghost A) | ⚠️ | ✅ parents | ❌ | ❌ (tests only) | **ORPHAN** — produced, never consumed (red-team: live writes, needs coordinated migration not blind cut) |
| **entities/relations/facts** (Ghost B) | ✅ extract.v1 | ✅ `ghost_b_extractions` + Neo4j | Neo4j ✅ / **Qdrant ❌** | Mode-A live Cypher ONLY | **DEAD-END** — the richest signal never reaches the vector layer |
| `query_aliases`, `definitional_phrase` | ✅ | ✅ | Neo4j ft-index | graph only | **DEAD-END** — alias recall exists only via graph hop |
| `object_kind`/`domain_type`/`canonical_family` (ontology) | ✅ Neo4j | ✅ | ✅ Neo4j | graph only | **DEAD-END** for facets/vector |
| `facet_ids`/`facet_text`/`content_facet_*` | ✅ facets.v1 | ✅ | **❌ not in payload index list** | in-memory coverage lanes only | **PARTIAL** — facets never actually FILTER Qdrant; they score lanes post-retrieval |
| `semantic_chunk_type` / answer-type | ❌ | ❌ | ❌ | wanted (C3, operator match) | **MISSING** — needs Ghost A field + promote |
| `mechanisms[]`/`abstract_patterns[]` | in-flight (B1) | partial backfill | ? | bridge-lane design | **IN-FLIGHT** |
| `source_book`/`author`/`document_date`/`is_latest`/`supersedes` | ❌ | ❌ | ❌ | citation + as-of retrieval want them | **MISSING** (M2 — parse-time capture) |
| `extractor` provenance (local vs cloud) | ❌ | ❌ | ❌ | threshold calibration needs it | **MISSING** — confidence semantics differ (GLiNER softmax vs LLM self-report) |
| char spans (`char_start/end`) | local emits | ⚠️ local only | ❌ | quote/highlight | **INCONSISTENT** local vs cloud |
| `text_hash`/`text_len`/`is_truncated` | payload | ✅ | ❌ | integrity checks | OK |
| `provenance` (graph arrows on SourceChunk) | ✅ | Mode-A only | n/a | prompt decoration | PARTIAL by design |
| `Chunk.parent_id` in Neo4j | ❌ by design | ❌ | n/a | forces hydration Pass-0 Mongo repair | **JOIN ASYMMETRY** |
| extract-schema version on stored points | envelope has it | ❌ not on payload (facets DO carry `facet_schema_version`) | ❌ | migration selectivity | **VERSION-BLIND** storage |

---

## 4. THE SIX STRUCTURAL GAPS (concept-level, what the re-architecture must fix)

1. **Split-brain contracts.** Typed at the edges, untyped at the storage boundary
   (`worker.py` dict builders, `qdrant_writer.py` payloads). *Fix:* the 5 schemas as Pydantic in
   `models/`, and the writers accept ONLY those models — schema IS the storage shape.
2. **The promotion void.** P2→P3 has one lane (graph) and zero for vector/lexical. Entities,
   aliases, relations, facts, ontology families all dead-end. *Fix:* `promote()` as the ONLY
   writer of derived metadata (idempotent post-ghost — ghosts run parallel, children are written
   before ghosts finish; "before writes" is impossible, REBUILD §8).
3. **Two disconnected taxonomies.** Facets (`facet_ids`) and ontology
   (`object_kind`/`domain_type`/`canonical_family`) describe the same concept space and never
   join; query-time facet discovery re-derives what extraction already knows. *Fix:* facets become
   a PROJECTION of promoted extraction (entity families/kinds feed `content_facet_ids`), one
   taxonomy with two views.
4. **Consumer-less fields & field-less consumers.** `topics` produced-never-read;
   `semantic_chunk_type`/temporal/authorship read-in-design-never-produced. *Fix:* the Stage
   Contract as a CI TEST — every emitted field names its consumer; every consumer's field is
   asserted populated on a fixture ingest.
5. **Identity asymmetry across stores.** `parent_id` known to Mongo child + Qdrant payload but
   not Neo4j Chunk → hydration repair passes. *Fix:* the identity spine
   (corpus/doc/parent/chunk/entity:{slug}/fact:{sha}) carried IDENTICALLY in all three stores.
6. **Version-blind storage.** Envelopes versioned, points not (except facets). *Fix:* every
   written point/doc carries `extract_schema_version` + `promote_version` → selective, replayable
   migrations (backfills become the forward path).

---

## 5. TARGET PICTURE (one sentence per plane)

- **P1:** five typed schemas (`ExtractionOutput` v2 w/ `extractor` provenance · `ChunkMetadata` ·
  `RetrievalPayload` · `GraphWriteModel` · `RerankerInput`) are the only shapes writers accept.
- **P2:** local + cloud emit the SAME envelope (already true) + provenance + spans.
- **P3:** `promote()` (pure, idempotent, versioned) is the single P2→P3 crossing; Qdrant payload
  gains `concepts[]`, `entity_ids[]`, `relation_families[]`, `fact_types[]`, `semantic_chunk_type`,
  temporal/status — all indexed in the same migration.
- **P4:** tiers consume by contract — Fast: identity+kind; Hybrid: +concepts/key_terms/mechanisms;
  Graph: +relations/bridges — cross-encoder stays sole ranking authority; domain stays soft.

**Ordering note for the re-architecture:** matrix rows marked DEAD-END are pure mapping over data
already extracted (backfillable, no re-ingest). Rows marked MISSING need new capture (parse-time
or Ghost A prompt fields) and only pay off after promote() exists. Fix the void (gap 2) first;
everything else composes on top of it.
