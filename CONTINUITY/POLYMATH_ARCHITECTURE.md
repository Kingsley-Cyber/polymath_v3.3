# Polymath — Unified Architecture (ingestion → retrieval, canonical)

**Status: CANONICAL as of 2026-07-03.** One file, the whole stack.
**Authorship:** retrieval/assembly design (waterfall, two-lane anchoring, summary policy,
multitenancy) — **King, owner-designed**. Schema/metadata/promotion layer + gap analysis —
assistant passes, owner-approved. Consolidates and supersedes as working reference:
`OWNER_RETRIEVAL_ARCHITECTURE.md` · `SCHEMA_METADATA_MAP.md` · `EXTRACTION_VS_METADATA.md`
(+amendments) · surviving parts of `REBUILD_IMPLEMENTATION.md` (originals retained as history).

---

## 0. Governing principles (non-negotiables)

1. **Stage Contract** — every produced field names its downstream consumer; every consumer's
   field is asserted populated. Enforced by CI test, not convention.
2. **Deterministic assembly** — no LLM judgment in context assembly. Same query + candidates +
   budget ⇒ byte-identical packet (`packet_hash` is an acceptance test).
3. **Search in Qdrant only.** MongoDB is a fetch layer, never a search layer. Neo4j is the
   relationship layer.
4. **Cross-encoder is the sole ranking authority.** Everything else (domain, facets, mechanisms,
   concepts) feeds recall, diversity, or filtering — never score multipliers.
5. **Domain is a SOFT boost, never a gate** (cross-domain synthesis guardrail).
6. **Corpus supplies the facts; the LLM supplies the bridge** (answerability honesty floor:
   a side with zero retrieved evidence refuses; a missing cross-doc link is synthesized).
7. **promote() is the ONLY writer of derived metadata.** No ad-hoc per-field backfills; backfill
   scripts are the forward path only until promote() ships, then they die.
8. **Rebuild, not re-ingest.** ~5-doc proving corpus validates every stage before full re-ingest.

---

## 1. Identity spine + versioning (one set of keys, all three stores)

| Key | Format | Lives in |
|---|---|---|
| `corpus_id` | uuid4 | Mongo + Qdrant payload (indexed) + Neo4j property — identical everywhere |
| `doc_id` | sha256(file) — stable across re-ingest | all three |
| `parent_id` | `{doc_id}_parent_{i:04d}` | Mongo parent+child, Qdrant payload, **Neo4j Chunk (ADD — fixes join asymmetry / hydration Pass-0 repair)** |
| `chunk_id` | `{doc_id}_{i:04d}` | all three |
| `entity_id` | `entity:{name_slug}` (deterministic) | Neo4j key + **Qdrant `entity_ids[]` (ADD — the vector↔graph join)** |
| `fact_id` | `fact:{sha256(doc,chunk,subject,prop,value)}` | Neo4j |

**Version stamps on every written record:** `extract_schema_version` + `promote_version`
(+ existing `facet_schema_version`) → selective, replayable migrations. No more version-blind
storage.

---

## 2. Contracts (P1) — five typed schemas; writers accept ONLY these

Kills the split-brain: today typing stops at the storage boundary (`worker.py:1173/:1222`,
`qdrant_writer.py:597` build untyped dicts). Under this architecture the schema IS the storage
shape.

### 2.1 `ExtractionOutput` — envelope `polymath.extract.v2` (the local/cloud bridge)
Both extractors emit the SAME envelope; `extractor` is the only differing field. (v1 already
converged — verified: ghost_b_local wire dicts and all 3 cloud modes `_parse()` to one
`ExtractionResult`, keyed `(corpus_id, doc_id, chunk_id)` in `ghost_b_extractions`.)

```python
class ChunkExtraction(BaseModel):
    schema_version: Literal["polymath.extract.v2"]
    extractor: Literal["gliner_glirel_local", "cloud_llm"]   # provenance — confidence
    corpus_id: str; doc_id: str; chunk_id: str; parent_id: str  # semantics differ per extractor
    text: str
    entities: list[ExtractedEntity]    # canonical_name, surface_form, entity_type(14),
                                       # confidence, query_aliases[<=5], definitional_phrase,
                                       # object_kind, char_start/end (local; cloud may null)
                                       # + promote-time: entity_id, domain_type, canonical_family
    relations: list[ExtractedRelation] # subject, predicate(30), object, object_kind,
                                       # confidence, evidence_phrase(required), relation_cue
                                       # + promote-time: relation_family, source_predicate,
                                       #   validation_status
    facts: list[ExtractedFact]         # subject, fact_type(9), property_name, value, unit,
                                       #   condition, confidence, evidence_phrase (+fact_id)
    schema_lens_id: str | None         # corpus guidance version (audit)
```

### 2.2 `ChunkMetadata` — identity & provenance (Mongo source of truth)
```
{ doc_id, chunk_id, parent_id, corpus_id, user_id,
  source_book(title), author_or_org, source_type, document_date,   # M2 — parse-time capture
  section_path[](heading_path), chunk_kind(structural), token_count,
  ingested_at, document_status, is_latest, supersedes[], superseded_by }
```
*M2 fields are a HARD PREREQUISITE for two-lane anchoring (§5.2) — anchor detection matches
author/title metadata that does not exist today.*

### 2.3 `RetrievalPayload` — the promotion target (Qdrant payload; every field indexed)
```
{ chunk_id, parent_id, doc_id, corpus_id, user_id,                  # identity (exists)
  chunk_type(child|doc_summary), chunk_kind, language,              # exists
  domain,                                                           # soft signal (exists, M1)
  concepts[]        ← entity canonical_names + query_aliases (recall — the #1 quick win),
  entity_ids[]      ← entity:{slug} (exact filter + graph join),
  entity_families[] ← canonical_family;  entity_domains[] ← domain_type,
  relation_predicates[], relation_families[], fact_types[], has_relations,  # graph-tier prefilter
  semantic_chunk_type ← definition|claim|procedure|principle|framework|example|comparison|warning,
  mechanisms[], key_terms[],                                        # Ghost A (bridge/diversity)
  document_status, is_latest, document_date,                        # temporal (M2)
  extract_schema_version, promote_version }                         # migration stamps
```
Payload index created in the SAME migration as the field; CI asserts index presence before any
filter uses it.

### 2.4 `GraphWriteModel` — Neo4j (exists; keep, plus Chunk.parent_id + Entity.corpus_ids[])
Entity (ontology-enriched: object_kind/domain_type/canonical_family/ontology_version, all
indexed) · RELATES_TO (predicate, relation_family, edge_strength, eligible_for_synthesis,
evidence arrays) · Fact (+HAS_FACT/SUPPORTS_FACT) · MENTIONS. Constraints in `graph/schema.py`.
**Multi-corpus isolation: PROPERTY-based, never ID-prefixing.** Entity identity stays GLOBAL
(`entity:{slug}`) — prefixing (`corpus::entity_x`) would split identity and destroy cross-corpus
bridges + the `entity_ids[]` vector↔graph join, which are core goals. Corpus scoping lives on
properties: `corpus_ids[]` arrays on RELATES_TO (exists, indexed), corpus_id on
MENTIONS/Fact/Document/Chunk (exists), **plus ADD an accumulated `Entity.corpus_ids[]` array**
(union at write, like query_aliases) so corpus-scoped traversals filter directly on the node
with prefix-grade ergonomics and zero identity split — same match-any idiom as Qdrant
multitenancy.

### 2.5 `RerankerInput` — short text actually scored
```
f"{source_book} › {section_path[-1]}\n{query_guided_excerpt(child_text)}"
```
No ids/hashes/paths ever reach the reranker or the answer model.

---

## 3. Ingestion flow (P2 → P3)

**S1 Parse** (docling) → clean text + `{title, author, source_type, document_date}` captured AT
PARSE (today: filename only). Consumers: ChunkMetadata, packet prefix, anchor matching, temporal.

**S2 Chunk** (tier_chunker) → parents ≈1200 tok / children ≈128 tok via `semantic_split`.
**Spectrum audit 2026-07-03 (3 sweeps + empirical container probes):** the splitter is
**100% STRUCTURAL — zero embeddings/similarity.** `\n\n+` paragraphs → one child per paragraph;
oversize paragraphs fall to sentence-regex packing; degenerate text falls to a mid-word hard
split at the 256 cap. Everything hinges on blank lines: with them, structure is preserved
exactly (tables/code fences isolate cleanly); without them (bullet lists, non-YouTube
transcripts, chat logs, poetry — single-`\n` text), it collapses to arbitrary sentence packing.
A 40-sentence 3-topic single paragraph stays ONE fused child — no topic detection.
Per-type routers already EXEMPLARY: tables (rows never split, headers repeated per chunk, all 5
formats one linearizer), code (AST-bound, fenced-in-prose routed correctly), YouTube transcripts
(time-grouped, guarded). Confirmed layer-2 gaps: **lists flattened to prose (worst gap)** ·
single-newline text · topic-fused paragraphs · VTT/SRT/speaker-turn transcripts · layout-vs-data
tables · formulas/figures/footnotes dropped · mid-word hard split.
**S2 upgrades (resolved recommendation — deterministic rules first, embeddings last):**
(1) list-aware router — regex bullet/number detection, split at item boundaries, keep items
intact (pure rules); (2) single-newline pseudo-paragraph fallback — a block with no `\n\n` but
many `\n` lines splits on lines before sentence regex (fixes transcripts/lists/poetry shape);
(3) hard-split prefers the nearest sentence/whitespace boundary, never mid-word; (4) VTT/SRT +
speaker-turn detection extending the existing transcript path; (5) ONLY THEN, targeted
embedding-deviation splitting (the TechViz method: consecutive-sentence cosine, boundary at
deviation spikes) as an ESCALATION applied solely to pathological blocks (no blank lines >M
tokens, or single paragraphs >N tokens) — bounded cost on the local embedder, deterministic
given fixed model+text, and it resolves the chicken-egg since only flagged blocks embed at
chunk time. The waterfall depends on none of this.
**STATUS: ALL ROUTERS (1)–(5) SHIPPED 2026-07-03, E2E-verified 7/7.**
(1) list item-boundary splitting + (2) line-grouping (`CHUNKER_STRUCTURED_ROUTERS`) + SaT
sentence engine (wtpsplit-lite sat-3l-sm, `CHUNKER_SENTENCE_ENGINE=sat`, latched regex
fallback) + (3) whitespace-boundary hard split + (4) VTT/SRT subtitle lane (stdlib cue parser →
transcript_block sections w/ time ranges + speakers; `source_format=subtitle_vtt|subtitle_srt`)
+ (5) semantic-deviation escalation (`CHUNKER_SEMANTIC_ESCALATION`: oversize >=8-sentence
paragraphs batch-embed via the local embedder, boundaries at cosine-deviation dips, one chunk
per topic segment, latched greedy fallback). Also: docling sidecar re-prefixes ListItem markers
("- ") joined by single newlines so PDF lists reach router 1; upload DEFAULT_EXTENSIONS aligned
to adapter capability (+vtt/srt/csv/tsv/xlsx/log/code exts). Live E2E through the real upload
API (7 formats): lists item-intact · timestamped chat → transcript lane w/ time metadata ·
SRT speakers ALICE/BOB · VTT voice tags stripped · CSV kind=table rows intact · py AST lane ·
3-topic essay → 3 single-topic children via the REAL MLX embedder. 19 router tests +
43 chunker regressions green.

**S3 Ghost A** → per-parent `{summary(prose), domain(soft), mechanisms[], key_terms[],
semantic_chunk_type}` **+ NEW doc-level summary** (feeds `doc_summaries`, §4). `topics` is
retired by coordinated migration (it is LIVE-written today — stop writing → null → remove
field+parser+tests), subsumed by key_terms+mechanisms.

**S4 Ghost B** (local GLiNER/GLiREL **or** cloud — same envelope §2.1) → entities/relations/facts
→ `ghost_b_extractions` staging + graph write. Extraction now has TWO consumers: graph AND
promote().

**S5 promote()** — the single P2→P3 crossing (`services/ingestion/promote.py`). Pure,
deterministic, versioned, unit-tested. **Applied as an IDEMPOTENT POST-GHOST WRITE** — ghosts run
in parallel and children are written to Mongo before ghosts finish (`worker.py:2541/:2522`), so
"one step before writes" is impossible; idempotent-post-write matches the resume model. Emits:
Mongo chunk record (ChunkMetadata + denormalized RetrievalPayload) · Qdrant child payload ·
Qdrant doc_summary payload · GraphWriteModel. Normalization (lowercase, snake_case, dedup,
taxonomy clamp) lives here, once.

**S6 Write + barrier** — document-level `write_state: writing|qdrant_failed|complete`. The
retriever must never serve a non-`complete` doc; partial cross-store writes are detectable and
replayable (fixes the ghost_b-staging orphan fragility).

---

## 4. Storage topology + summary policy

**MongoDB (fetch layer):** `parent_chunks` (full text + prose summary — the waterfall's rungs),
`chunks` (children + denormalized RetrievalPayload), `documents` (+facet_profile, M2 fields),
`ghost_b_extractions` (staging).

**Qdrant (search layer) — multitenancy, NO per-corpus collections:**
- **`children`** — dense 1024-d (Qwen3-Embedding) + sparse BM25 (IDF), payload = RetrievalPayload.
- **`doc_summaries`** — small collection, ONE point per document (doc-level summary vector).
  Searched ONLY for Tier-0 routing / anchor discovery; never fed as evidence except a broad
  "what is this corpus about" query.
- `corpus_id` is an indexed payload field; multi-corpus = one match-any filter, native score
  merge. Kills cross-collection score stitching (scores across collections are not comparable)
  and HNSW/memory overhead. Repo mapping: `corpora/<name>/` = sources + ingestion config
  (corpus_id, domain labels, chunking params); same corpus_id mirrored in Mongo + Neo4j.

**Summary policy (why separated):**
- **Parent summaries are NEVER embedded, never searched.** They are output-side compression
  artifacts fetched from Mongo after rerank (ranks 5–8). Child embeddings already represent
  parent content in vector space — embedding parent summaries creates duplicate hits competing
  with children and muddies rerank precision. (Today's Funnel A parent-summary vectors are
  retired — behind the probe battery, not a hard cut.)
- **Doc summaries are the only embedded summaries** — they do a job nothing else covers:
  routing/anchoring at corpus scale. Merge-gate: probe doc-summary vectors vs prose baselines;
  if short-text embedding degrades recall >5% on any domain, use the minimal schema for metadata
  only and keep richer prose for the vector.

**Neo4j:** as §2.4; Chunk gains `parent_id`.

---

## 5. Retrieval (P4) — owner design

### 5.1 Tier-0: routing + anchor detection (deterministic)
Preprocessing extracts named sources/authors ("Robert Greene", "Art of Seduction") — **lexical
author/title match FIRST, temp-0 LLM extraction as fallback, cached by normalized query** — then
matched against doc metadata (M2 author/title fields) → matching doc_ids tagged as **anchors**.
`doc_summaries` search assists routing at corpus scale. Routing is a metadata match, not LLM
judgment: same query always routes the same way.

### 5.2 Two-lane retrieval
- **Anchor lane:** hard-filtered to anchor doc_ids, guaranteed slots — ranks 1–4 full parents
  must come from the anchor (grounding).
- **Expansion lane:** parallel retrieval over the corpus EXCLUDING anchors — fills summaries +
  orphan-children slots (cross-domain hydration).
- **Quota:** fixed budget split (e.g. 60% anchor / 40% expansion), each lane hydrated by the same
  waterfall. No anchor detected → collapse to single-lane retrieval.
- **Spillover:** anchor slots fill only while candidates clear a fixed rerank-score threshold;
  shortfall spills to the expansion lane, promoting cross-domain parents/summaries up the
  waterfall. The threshold decides — determinism holds.

### 5.3 Rank
Cross-encoder reranks children per lane → ONE ranked parent list (dedupe children→parents,
diversity-capped).

### 5.4 Deterministic hydration waterfall (assembly)
1. **Fixed budget** (e.g. 4k context tokens); one ranked parent list.
2. **Walk ranks top-down**, hydrating each parent at the richest form that fits remaining budget:
   **full text → summary → skip**. Ranks 1–4 naturally land as full texts (~60% of budget);
   5–8 fall to summaries automatically. Summary count is NOT fixed — one per unique included
   parent (deduped), count varies.
3. **Fill leftovers in fixed order:** orphan children (cross-domain, deduped against included
   parents) → shared entities last. Orphans are fed as raw fragments; optionally attach an
   orphan's parent summary when that parent isn't already ranked 5–8 (token-cost knob).
4. **Overflow rule:** a full text that doesn't fit swaps to its summary — never truncate mid-text.
   (B2 query-guided excerpt may serve as an optional middle rung full→excerpt→summary — owner's
   call, off by default.)
5. **Surplus rule:** remaining budget promotes the next summary up to full text.
6. **Dedupe rules:** drop any child whose parent is included; drop any summary whose parent is
   full-texted.
"Exhausted parents" degrade gracefully down the ladder instead of cutting off. Same inputs ⇒
identical context.

### 5.5 Tier contracts
| Tier | Searches | Consumes (payload) | Contract | Budget |
|---|---|---|---|---|
| **Fast** | children | identity, chunk_kind, domain(soft) | 12–20 children cross-domain + per-parent summaries (deduped); fastest direct evidence | fetch 50 → rerank 32 |
| **Hybrid** (default) | children + BM25 (+doc_summaries Tier-0) | + concepts[], key_terms, mechanisms, semantic_chunk_type | full parents 1–4 + parent summaries 5–8 + orphan children (waterfall) | fetch 70 → rerank 40 |
| **Graph** | + Neo4j Mode A | + entity_ids[], relation_families[], facts | deep synthesis / relationships / cross-domain bridges | fetch 120 → rerank 40 (+facts) |

### 5.6 Graph layer
Entity seeds from **included parent summaries + selected child chunks**; relationship expansion
**scoped to the chosen domains only**. `entity_ids[]` on the payload is the vector↔graph join —
no live name-matching Cypher.

### 5.7 Answerability (existing, keep)
Two mirror gates (retriever `_evaluate_sufficiency` = repair driver, STRICT; chat gate = refusal
arbiter, LENIENT via `answerability_tuning.py`). Relationship queries answer when each side has
≥1 source (LLM bridges); a zero-evidence side refuses honestly.

---

## 6. Gap closure map (SCHEMA_METADATA_MAP matrix → fixed by)

| Structural gap | Fixed by |
|---|---|
| Promotion void (extraction dead-ends at graph) | §2.3 RetrievalPayload + §3.S5 promote() |
| Split-brain contracts (untyped storage dicts) | §2 writers-accept-only-schemas rule |
| Two disconnected taxonomies (facets vs ontology) | facets become a PROJECTION of promoted extraction (entity families/kinds → content_facet_ids) |
| Consumer-less fields / field-less consumers | §0.1 Stage-Contract CI test |
| Identity asymmetry (Neo4j Chunk lacks parent_id) | §1 spine (add parent_id; kill hydration Pass-0 repair) |
| Version-blind storage | §1 version stamps |
| Per-corpus collections / score stitching | §4 multitenancy |
| Parent-summary double-representation | §4 summary policy (Funnel A retired behind probes) |
| Anchoring as heuristics | §5.1–5.2 metadata-matched two-lane |
| Local/cloud extractor divergence | §2.1 one envelope + extractor provenance |

---

## 7. Build order (dependency-driven; each step probe-gated)

- **B0 Contracts** — the 5 schemas as typed models in `models/` + Stage-Contract CI test.
  *No data change.*
- **B1 Waterfall allocator** — pure function (`ranked_parents, budget, lane_quotas → packet`),
  unit-tested to byte-identical output including overflow/surplus/dedupe/spillover cases.
  *No data dependencies — testable against existing rerank output today.*
- **B2 promote() + first backfill** — `concepts[]` + `entity_ids[]` from existing
  `ghost_b_extractions` → child payload + indexes. *No re-extraction; pure mapping.*
- **B3 New capture** — M2 parse fields (author/title/date) + Ghost A doc-level summaries +
  `doc_summaries` collection. *(M2 blocks §5.1 anchoring.)*
- **B4 Multitenancy migration** — shared collections + corpus_id index, proven on the ~5-doc
  corpus before any full re-ingest; write barrier lands here.
- **B5 Two-lane anchoring wired** + Funnel A retirement behind the probe battery.

**Gates for every step:** golden battery (precision) · habits-NN (cross-domain recall+diversity,
bridges CE-scored) · seducer (0 off-topic) · packet_hash determinism · latency per tier.

## 8. Open decisions & carried risks
- **Chunker upgrade — RESOLVED by the 2026-07-03 spectrum audit (owner to ratify):** hybrid.
  Keep the structural paragraph-idea spine (a) — probes show it preserves structure exactly when
  blank lines exist. Add the four deterministic layer-2 routers (lists, single-newline fallback,
  boundary-aware hard split, VTT/SRT) BEFORE any embeddings; then embedding-deviation splitting
  (b) only as a targeted escalation for pathological blocks (see §3.S2). Full evidence:
  CHUNKER audit reports (routing matrix · empirical probes · non-prose lanes).
- **B2 excerpt rung** in the waterfall ladder — optional, owner's call.
- **Doc-summary gist risk** — short-text embedding merge-gate (§4).
- **`topics` retirement** — coordinated migration only (live field).
- Reranker fp16 determinism re-verify before packet_hash gate is enforced.
