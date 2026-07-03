> **Consolidated into [POLYMATH_ARCHITECTURE.md](POLYMATH_ARCHITECTURE.md) (canonical, 2026-07-03). Retained as history — the 5-schema derivation + evidence tables live here.**

# Extraction Schema vs Retrieval Metadata — Validation + Production Shape

**Date:** 2026-07-02 · **Owner insight (validated):** extraction ≠ metadata. Extraction is *what
the system pulls out of text*; metadata is *what the retriever uses to store, filter, route, rank,
cite, and retrieve*. The repo has extraction but **no promotion layer**, so the two are conflated
in practice — which explains a real slice of the retrieval confusion.

---

## Validation: is extraction promoted to retrieval metadata? (evidence)

| Extractor | Produces | Where it lands | Promoted to retrieval metadata? |
|---|---|---|---|
| **Ghost A** (LLM) | summary, domain, topics | parent_chunks (Mongo) + Qdrant **summary** payload | **PARTIAL** — domain promoted (parents; children via M1 backfill); topics parent-only, unused by retriever |
| **Facets** (rule/alias) | facet_ids, facet_text, content_facet_* | chunks + parents + Qdrant payload | **YES** — but coarse topic slugs, not mechanisms |
| **Ghost B = GLiNER/GLiREL** (model) | entities (canonical_name, surface_form, entity_type, **query_aliases**, **definitional_phrase**, object_kind), relations (30 predicates → relation_family), facts | **Neo4j + `ghost_b_extractions` (Mongo staging)** | **NO** — `grep` finds zero promote/to_metadata/to_payload mapper; `ghost_b_extractions` is never read by the retriever; consumed ONLY by live Mode-A Cypher at the graph tier |

**Verdict:** GLiNER/GLiREL handled *extraction*. Nobody built *promotion*. The richest signal —
entities, aliases, relations, and the concepts/mechanisms they imply — **dead-ends in the graph**
and never becomes a filterable/rankable Qdrant field. The retriever can filter only on
`corpus_id`, `chunk_type`, `chunk_kind`, `domain` (M1), `facet_ids`. That is the gap.

**Consequence:** the pipeline is `extraction → (store in graph) → [DEAD END]`. It should be
`extraction → normalize → PROMOTE selected fields → write to retrieval indexes → retriever uses them`.

---

## The five schemas (production shape) — grounded in the repo

Locking these separates concerns so a field can't be "extracted but unusable" or "dumped into
model input as noise."

### 1. `ExtractionOutput` — what the models pulled out (raw, verbose, per source)
Union of Ghost A + Ghost B + facet outputs. **Never** goes to the reranker or the answer model.
```
{ summary, domain_guess, topic_guesses[], mechanism_guesses[],          # Ghost A
  entities: [{canonical_name, surface_form, entity_type, object_kind,
              confidence, query_aliases[], definitional_phrase}],        # Ghost B / GLiNER
  relations: [{subject, predicate, object, relation_family, confidence,
               evidence_phrase}],                                        # Ghost B / GLiREL
  facts: [...], facet_ids[], facet_text }                               # facets
```

### 2. `ChunkMetadata` — stable identity & provenance (Mongo source of truth)
```
{ doc_id, chunk_id, parent_chunk_id, corpus_id, user_id,
  source_book(title), author_or_org, section_path[](heading_path),
  source_type, document_date, ingested_at, chunk_kind(structural),
  document_status, is_latest, supersedes[], superseded_by }
```
*Today: identity fields exist; source_book/author/source_type/dates/status/versioning MISSING
(METADATA_LAYER_AUDIT.md M2/M4).*

### 3. `RetrievalPayload` — ONLY fields used to filter / route / rank (Qdrant payload + indexes)
The promotion target. Small, indexed, no free text beyond what a filter needs.
```
{ chunk_id, parent_chunk_id, doc_id, corpus_id, user_id,          # identity/scoping
  chunk_type(vector role), chunk_kind(structural filter),
  domain(SOFT boost, indexed),                                    # M1 shipped
  semantic_chunk_type(definition|claim|procedure|principle|framework),  # MISSING → promote
  concepts[](from entity canonical_names + aliases),              # MISSING → promote from Ghost B
  mechanisms[], abstract_patterns[],                              # MISSING → B1 backfill
  document_status, is_latest, document_date }                     # MISSING → M2
```
**Promotion rules (the missing layer):**
- `concepts[]` ← Ghost B entity `canonical_name` + `query_aliases` (dedup, lowercase). This alone
  makes entity/alias recall work at the vector layer instead of only via live graph.
- `semantic_chunk_type` ← a small classifier (Ghost A can emit it; one prompt field).
- `mechanisms[]/abstract_patterns[]` ← Ghost A mechanisms (B1 backfill, proven).
- `domain` stays a **soft** signal, never a hard gate (cross-domain synthesis guardrail).

### 4. `GraphWriteModel` — only graph-ready entities/relations (Neo4j)
```
{ entities: [{entity_id, canonical_name, entity_type, object_kind, canonical_family}],
  relations: [{subject_id, predicate, object_id, relation_family, confidence}],
  bridge_cache: {structural_analogies, transfer_candidates, ...} }   # Mode A, currently unpopulated
```
*Today: this is the ONLY consumer of Ghost B — correct, but it should not be the only one.*

### 5. `RerankerInput` — short text actually scored (+ minimal prefix)
```
f"{source_book} › {section_path[-1]}\n{query_guided_excerpt(text)}"
```
*Today: raw excerpt only (B2 query-guided). A title/section prefix A/B is open; NO ids/hashes/paths.*

---

## The mapping layer (the code that doesn't exist yet)
One function per write target, fed by a single `promote(extraction) -> {ChunkMetadata,
RetrievalPayload, GraphWriteModel, SummaryPayload}`:
```
promote(ExtractionOutput, ChunkIdentity) ->
    mongo_chunk_record   (ChunkMetadata + denormalized RetrievalPayload)
    qdrant_child_payload (RetrievalPayload)
    qdrant_summary_payload (RetrievalPayload at parent granularity: domain, mechanisms, topics)
    neo4j_write          (GraphWriteModel)
```
It runs in `worker.py` after Ghost A + Ghost B complete, BEFORE the Qdrant/Mongo writes — the
single place extraction becomes metadata. Normalization (lowercase, snake_case, dedup, taxonomy
clamp) lives here, once, instead of scattered.

---

## What this reframes about the current work
- **Domain denorm (M1):** was an ad-hoc promotion of ONE field. The mapping layer generalizes it.
- **Mechanisms (B1):** a new extraction field + its promotion — the first field designed
  extraction→metadata correctly end to end.
- **`concepts[]` promotion is the highest-leverage quick win:** Ghost B already has
  canonical_name + query_aliases per chunk in `ghost_b_extractions`; promoting them to a
  `concepts[]` Qdrant payload (indexed) turns dead extraction into entity/alias recall WITHOUT any
  new model work — pure mapping over data you already extracted.

## Amendments — local/cloud extractor bridge (reconciled 2026-07-02, second review)
An independent pass over the same code converged on this doc's verdict and adds four deltas so
ONE envelope serves both the local (GLiNER/GLiREL) and cloud extraction paths:

1. **`extractor` provenance on `ExtractionOutput`** — `"gliner_glirel_local" | "cloud_llm"`.
   Both paths already normalize to the same `ExtractionResult` (`polymath.extract.v1`, verified:
   ghost_b_local.py wire dicts and all 3 cloud modes `_parse()` to it), but nothing records WHICH
   extractor produced a row. Confidence semantics differ (GLiNER softmax vs LLM self-report), so
   promotion thresholds and audits need the tag. Add `char_start/char_end` spans (local has them;
   cloud may null) and bump the envelope tag to `polymath.extract.v2`.
2. **Promote relations too, not only entities** — add to `RetrievalPayload`:
   `relation_predicates[]`, `relation_families[]`, `fact_types[]`, `has_relations` (aggregated
   per chunk, indexed). Lets the vector layer pre-filter "chunks carrying Causal/Operational
   links" for the Graph tier without a live Neo4j hop.
3. **`entity_ids[]` alongside `concepts[]`** — `concepts[]` (lexical names+aliases) is the RECALL
   field; `entity_ids[]` (`entity:{slug}`, same deterministic ID as Neo4j) is the exact-filter and
   Qdrant↔Neo4j JOIN key. They serve different consumers; promote both.
4. **promote() placement** — per REBUILD_IMPLEMENTATION §8 BLOCKER-1/2: ghosts run in parallel and
   children are written before ghosts finish, so promote() output is applied as an idempotent
   post-ghost promotion write (pure function, re-runnable on resume), not a pre-write barrier.

## Migration (introduce the layer without breaking partial promotions)
1. Define the 5 Pydantic schemas in `models/` (extraction, chunk_meta, retrieval_payload,
   graph_write, reranker_input).
2. Write `services/ingestion/promote.py::promote()` — pure, unit-tested, deterministic.
3. Backfill `concepts[]` from existing `ghost_b_extractions` → chunk payload (like the domain +
   mechanisms backfills — no re-extraction).
4. Route worker.py writes through `promote()`; delete the scattered per-field mapping.
5. Wire retriever to filter/rank on the newly-promoted fields (concepts recall lane; semantic
   chunk_type + mechanisms as diversity/curation signals — never score multipliers).
Each step A/B-gated on the golden + habits-NN probes; the cross-encoder stays the sole ranking
authority throughout.
