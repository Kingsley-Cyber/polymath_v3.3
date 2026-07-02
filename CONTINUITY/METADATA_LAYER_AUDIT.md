# Metadata Layer Audit — Retrieval Precision & Speed

**Date:** 2026-07-02 · **Method:** 4 parallel code auditors (capture / filters / model-input /
temporal), every claim verified with file:line. Companion to RETRIEVAL_LAYER_SPEC.md.
**Verdict in one line:** the metadata layer is *rich at capture, thin at use, and has no
temporal/versioning story at all* — most of the owner's target pipeline is a wiring job on
data already stored, plus one real schema gap (dates/versioning/status).

---

## Q1/Q2 — What is stored where (vs the target schema)

| Target field | Doc level | Chunk level | Qdrant payload | Notes |
|---|---|---|---|---|
| doc_id / chunk_id / parent_chunk_id | ✅ | ✅ (`parent_id`) | ✅ | |
| title | ❌ | ❌ | ❌ | `filename`/`doc_name` proxy only |
| section_path | — | ✅ `heading_path` | ✅ | |
| domain | ❌ | ✅ on **parents** (Ghost A, 19-value taxonomy) | ❌ children | hydrated post-hoc only |
| source_type | ❌ | ❌ | ❌ | `source_tier` is parse-structure, not book/paper/web |
| author_or_org | ❌ (inside filename/source_identity only) | ❌ | ❌ | |
| document_date | ❌ | ❌ | ❌ | only `created_at`=ingest time |
| ingested_at | ✅ (`created_at`) | ❌ | ❌ | |
| effective_start/end | ❌ | ❌ | ❌ | |
| is_latest / supersedes / superseded_by | ❌ | ❌ | ❌ | exists ONLY as a Neo4j *relation predicate* (neo4j_writer.py:1556) — not doc metadata |
| authority_score | ❌ | ❌ | ❌ | ghost_b_success_rate exists per doc, unused |
| chunk_type (semantic: definition/claim/procedure...) | — | ❌ | ❌ | `chunk_kind` is STRUCTURAL (body/toc/code/table) |
| entities / relations | — | ❌ on chunk | ❌ | live in Neo4j only (Ghost B) |
| keywords | — | ✅ `topics` on parents | ❌ | unused at query time |
| security_scope | ✅ `user_id` | ✅ `user_id` | ✅ **indexed** | never filtered (corpus scoping at API layer) |
| document_status | ❌ | — | — | `ingest_stage` is pipeline progress, not lifecycle |

Also stored (beyond target): facet_ids/facet_text/content_facet_* (chunks+parents+Qdrant),
doc_facet_ids, facet_schema_version, page_start/end, token_count, language, source_mime,
schema_lens, is_near_duplicate + near_duplicate_candidates/of, source_identity fields.

## Q3 — Indexed for fast filtering
- **Qdrant payload indexes** (qdrant_writer.py:217-232): corpus_id, doc_id, chunk_id,
  parent_id, chunk_type, source_tier, user_id, chunk_kind, language.
- **Mongo**: chunks (corpus_id+chunk_id uniq, chunk_id, parent_id, doc_id, user_id,
  text+heading_path text index weighted 5:1); parent_chunks (corpus+doc+parent uniq, corpus+doc,
  parent_id); documents (corpus+doc uniq, corpus_id, user_id, doc_id,
  source_identity.source_key+corpus, youtube_video_id+corpus).

## Q4 — Stored but NOT used during retrieval
`domain` (hydrate.py:324 populates it post-hoc; never a filter, never shown to any model) ·
`language` (indexed in Qdrant! zero filters) · `user_id` (indexed! zero filters) ·
`is_near_duplicate`/`near_duplicate_*` (advisory-only; duplicates retrievable forever) ·
`topics` · `source_tier` (indexed; cosmetic only) · `content_facet_*` (fetched during rerank
hydration then discarded) · `provenance` (graph trace, never surfaced) · page ranges ·
`ghost_b_success_rate` · `created_at/updated_at` (UI sort only, mongo_reader.py:26).

## Q5 — Missing entirely
title · source_type · author_or_org · document_date · effective_start/end · is_latest ·
supersedes/superseded_by (as doc metadata) · authority_score · document_status ·
**semantic** chunk_type · entities/keywords on the chunk payload.

## Q6 — Fields slowing retrieval via missing indexes
**None.** This was live-tested earlier today: the `$text` query executes TEXT_MATCH (17k of
561k docs examined), `chunk_kind $nin` is a residual filter a B-tree can't serve (91%
anti-selective), and the anchor's full-document fetch was fixed by the label cache, not an
index. The only defensible addition is `(corpus_id, created_at)` on documents for UI sorts —
irrelevant to retrieval. Slowness here is architectural, not index-shaped.

## Q7 — Filters BEFORE vector/BM25 retrieval (the complete list)
1. `corpus_id ∈ scope` — every lane (funnel_a.py:113, funnel_b.py:53, lexical.py:479, anchor)
2. `chunk_type = child|summary` — funnels (funnel_b.py:45, funnel_a.py:104)
3. `chunk_kind ∉ NOISY_KINDS` — every lane, Qdrant must_not + Mongo $nin
4. `doc_id` — anchor per-doc recall only
That is the entire pre-filter surface. No domain, language, user, date, status, source-type.

## Q8 — Filters AFTER retrieval
`_drop_noisy_retrieval_chunks` (chat_orchestrator.py:2463 — re-validates kind after graph
expansion bypasses Qdrant) · similarity_threshold noise floor (retriever [4a], default off) ·
v4 P1 authority floors (support picks 0.22/0.15) · SPECIFIC trim (0.5×top) ·
low-confidence rerank guard · bounded tail trim · MMR near-duplicate jaccard ≥0.88 ·
per-doc caps + reserves · shingle containment dedup (chat-side).

## Q9 — Metadata in model inputs
- **Reranker input: RAW TEXT ONLY** (reranker.py:496-504 — B2 query-guided excerpt of
  chunk.text; no title/section/domain prefix of any kind).
- **Answer model per-chunk prefix** (chat_orchestrator.py:1701):
  `"{idx}. {label} | kind={source_tier} | score={float}"` — label falls back to **internal
  doc_id/chunk_id when no friendly name exists (ID leak, :1242-1243)**; `domain`,
  `heading_path`, dates, source_type all absent despite being hydrated and available.
- Owner's desired prefix (Title/Section/Domain/Source type/Date/Latest) ≈ **0.5 of 6 fields
  present** (label≈title only).

## Q10 — Deterministic latest/current/as-of retrieval
**Not supported at any layer.** No version chain, no as-of parameter, no status filter;
re-ingesting an edited document mints a new content-hashed doc_id and BOTH versions retrieve
forever (near-dup flag is informational; dedup resolution is a manual admin action;
`ingested_at` is consulted only inside dedup canonical-copy choice, dedup.py:250-267).

---

## Patch plan (M-series; each flag-gated, golden-battery + packet-hash verified)

**M1 — Wire what already exists (days, no schema change)**
1. Evidence-packet prefix: `Title · Section (heading_path tail) · Domain · Kind` — and kill
   the internal-ID fallback leak. Files: chat_orchestrator.py `_source_title`/`_format_evidence_packet_block`.
2. Reranker prefix A/B: prepend `"{title} › {heading_path[-1]}\n"` to the CE excerpt —
   run golden battery + margin probe before/after (changes CE input distribution; calibration
   may need refit).
3. Retrieval-time near-duplicate suppression: prefer canonical copy when `is_near_duplicate`
   (deterministic tie-break in curation) — data already stored, zero new writes.
4. `similarity_threshold`: enforce or delete the knob (currently decorative).

**M2 — Temporal/versioning schema (the real gap)**
- documents += `document_date` (ingest-time extraction: epub/PDF metadata → source_identity →
  filename year; nullable), `document_status` (default `active`), `supersedes[]/superseded_by`
  (wire dedup *resolve* to SET these instead of delete-only), derived `is_latest`.
- Denormalize `{document_status, is_latest, document_date}` onto child payloads at ingest +
  one-shot backfill via Qdrant `set_payload(filter=doc_id)`; add payload indexes (keyword,
  keyword, float-epoch).
- Pre-filters in every lane: `document_status=active`, `is_latest=true` (unless
  `include_superseded`), optional `document_date <= as_of` range.
- This delivers Q10 and the owner's hard-filter block 1:1.

**M3 — Semantic chunk_type + authority (ingest enrichment, not query-time lists)**
- `chunk_type ∈ {definition, claim, procedure, example, comparison, warning}` assigned at
  ingest by the Ghost-A pass (it already reads every parent); payload-indexed; matched against
  the query plan's existing `operators` (it already emits `definition`!) as a curation
  tie-break — never a score multiplier (v4 rule).
- `authority_score` = deterministic product of source_type prior × ghost_b_success_rate ×
  dedup canonicality; curation tie-break only.

**M4 — Source identity completion**: title/author_or_org/source_type extraction at ingest
(epub OPF, PDF XMP, source_identity), payload denormalization, packet prefix completed to the
owner's six-line format.

## Verification (each phase)
- `docker exec -w /app polymath_v33-backend-1 python -m pytest tests/ -q` (1710 green baseline)
- Golden battery: `scratchpad/rag_probe_gate.py` (5/5) + seducer probe (0 off-topic docs)
- Determinism: same query ×5 → identical packet hash (v4 trace field)
- Filter proof: Qdrant `query_points` with `document_status=active` filter → explain latency
  unchanged (payload-indexed); Mongo `explain("executionStats")` on any new predicate
- as-of proof (post-M2): ingest v1+v2 of one doc → default query returns only v2; `as_of`
  before v2's date returns v1; superseded doc never seats.
