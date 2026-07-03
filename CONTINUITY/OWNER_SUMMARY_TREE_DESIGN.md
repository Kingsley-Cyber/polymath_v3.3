# Owner Design — Summary Tree + Compact Retrieval Schema (next phase)

**Author: King (owner-designed research, recorded 2026-07-03).** Composes with
POLYMATH_ARCHITECTURE.md (§2 contracts, §4 doc_summaries, §5 Tier-0 routing). This fleshes out
the summary/metadata layer and solves the huge-document case.

## 1. Two schema shapes
- **Initial/full schema = canonical source of truth** (Mongo chunk/summary record):
  `chunk_id, parent_id, doc_id, corpus_id, source_title, source_type
  (standard|proposal|compiler_doc|book|blog|paper), domain, topic_key, summary_type,
  semantic_chunk_type, concepts[], mechanisms[], abstract_patterns[], document_date,
  ingested_at, effective_start, effective_end, document_status, is_latest, supersedes[],
  superseded_by`
- **Compact schema = retrieval payload** (Qdrant payload / summary index / graph evidence
  metadata): same identity + `title, source_type, domain, topic_key, summary_type, chunk_type,
  concepts[], mechanisms[], patterns[], doc_date, valid_from, valid_to, status, latest`.

## 2. Three summary levels (jobs)
- **Document-level summary** = map of the whole source → "what is this about, what
  domains/topics, when valid, should this doc be searched for this query?"
  → `query → document summaries → pick likely documents/domains/topics` (Tier-0 routing).
  Schema: `summary_id(docsum_*), doc_id, summary_type=document, title, source_type, domain,
  topic_keys[], concepts[], mechanisms[], patterns[], doc_date, valid_from/to, status, latest,
  summary, parent_ids[]`.
- **Parent-chunk summary** = retrievable section-level meaning → "what does this section argue,
  which child chunks support it, what concepts/mechanisms live here?"
  Schema: `summary_id(parentsum_*), doc_id, parent_id, summary_type=parent, title, section
  (heading path), domain, topic_key, chunk_type(principle|definition|claim|example|procedure),
  concepts[], mechanisms[], patterns[], child_chunk_ids[], summary`.
  → `query → parent summaries → find relevant sections → retrieve child evidence`.
- **Child chunk** = precise evidence.

## 3. Pipeline order (owner-specified)
1. Parse document → 2. create child chunks → 3. extract entities/concepts/relations from
children → 4. group children into parent chunks → 5. parent summaries from children →
6. document summary from parent summaries → 7. promote metadata into Qdrant/Mongo/Neo4j.
*(Scribe flag: steps 2→4 imply BOTTOM-UP parent formation — children first, then grouped into
parents. Current code is top-down (parents → children). Semantic-parents (f554fe4) is a step
toward this; full inversion is a design decision to settle at build time.)*

## 4. HUGE documents — the summary TREE (the core insight)
Real case: `Charles_F_Goldfarb_definitive_XML_series` = **1,727 parent chunks** → 1,727×60 tok
≈ **103k tokens** — a single document-summary LLM call is the wrong shape. **This is a
document-map / source-routing problem, not a summary problem.**

```
child chunks → parent summaries → rollup summaries → section summaries → document PROFILE
```
- **L1 parent summaries** (exists): `{type:parent, parent_id, section, summary, concepts[],
  mechanisms[], evidence[chunk_ids]}` — precise retrieval units.
- **L2 rollups**: group every 12–20 parent summaries (1,727/20 ≈ 87 rollups):
  `{type:rollup, rollup_id, doc_id, parent_ids[], section_range, summary, concepts[],
  mechanisms[]}` — Hybrid/broad section discovery.
- **L3 section summaries**: group rollups by heading_path/chapter/topic:
  `{type:section, section_id, doc_id, title, rollup_ids[], summary, concepts[], mechanisms[]}`
  — the document-internal map.
- **L4 document PROFILE** (the only "document summary"): compact source card:
  `{type:document, doc_id, title, source_type, domain, topic_keys[], concepts[], summary
  ("...best used for questions about..."), section_ids[]}` — enough for routing, nothing more.

**Cheap pipeline for 1,727 parents:** parent summaries → group by heading_path/chapter →
windows of 12–20 → rollup summaries → section summaries → final profile from ONLY
{title, TOC/heading list, detected_domains w/ counts, top_concepts, top_sections+summaries}
≈ **1–2k tokens LLM input, never 100k+**. Do NOT feed all parent summaries into the final call.

## 5. Fit with canonical architecture (scribe validation, initial)
- Compact-vs-full split = §2.3 RetrievalPayload vs §2.2 ChunkMetadata — ALIGNED; adds
  `topic_key`, effective dating (`valid_from/to`), `summary_type`, and names `patterns[]`.
- Document profile = the §4 `doc_summaries` Tier-0 point — this specifies its schema + the
  scalable way to produce it.
- Rollup/section levels are NEW artifacts (Mongo; optionally section summaries as additional
  routing vectors — decide at build).
- Temporal fields (`valid_from/to`, `supersedes`) = M2 versioning, now with concrete shape.
- Build order fit: lands with M2 capture + Ghost A doc-level pass (B3) + promote() (B2).
