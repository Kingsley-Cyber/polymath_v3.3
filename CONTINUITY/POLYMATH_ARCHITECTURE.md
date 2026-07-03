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
**+ SEMANTIC PARENTS (tier_c) SHIPPED 2026-07-03:** structureless text draws PARENT boundaries
at embedding-deviation dips between paragraph units (budget-clamped, `CHUNKER_SEMANTIC_PARENTS`,
deterministic: fixed model+text → identical boundaries, latched token-window fallback). Live:
3360-token 3-topic dump → exactly 3 single-topic parents, byte-identical twice. Structured tiers
(headings/AST/pages) unchanged; OCR page-grouping keeps physical pages (follow-up can reuse the
same helper). 23 router tests green.

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

## 4.5 Host/Container topology (owner-ratified 2026-07-03)

**Docker Desktop for infrastructure ONLY; MLX models run directly on macOS.**
Apple Silicon unified memory (24GB shared CPU/GPU) is MLX's native model —
containerizing Metal loses acceleration. Apple's `container` tool REJECTED
(VM-per-container, immature compose, no host.docker.internal ergonomics).

```
macOS host                          Docker Desktop (infra only)
├── MLX embedder      :8082        ├── backend API + frontend
├── reranker (llama)  :8081        ├── Qdrant / MongoDB / Neo4j / Redis
├── GLiNER/GLiREL     :8084        └── (caps: ~8GB mem · 4.5 CPU · 3.5 swap)
└── inference discipline            macOS + MLX reserve ≈ 12GB
```

**Metal-contention rules (the "biggest thing"):**
1. ONE model inference at a time per GPU-bound service class — the existing
   rule (never rerank parallel support passes; RERANK_EVIDENCE_SUPPORT opt-in)
   generalizes to an inference semaphore across embed/rerank/LLM when a local
   answer model lands.
2. Every MLX service caps its buffer cache: `mx.set_cache_limit`
   (`MLX_CACHE_LIMIT_GB`, default 1.0) + startup metrics (active/peak/cache)
   — shipped in embedder_mlx; propagate on next sidecar re-install.
3. **GLiNER/GLiREL are INGESTION-ONLY** — enrich chunks, then idle/stop; they
   must never contend with query-time embed/rerank. (LaunchAgent lifecycle
   wiring = pending chip.)
4. Verification targets: memory_pressure normal · Docker <8-10GB ·
   32-doc rerank under timeout · no circuit-breaker opens · no raw-score
   fallback.

**Qdrant: payload indexes BEFORE data, only on filtered fields** (owner list —
create in the same migration as the field, CI-asserted): corpus_id ·
document_status · source_book · domain · chunk_type · chunk_kind ·
mechanisms · abstract_patterns · is_latest · document_date · security_scope ·
parent_id · concepts · entity_ids · relation_families · fact_types.

**Mac Studio vs RTX box gaps (identify, don't assume):** CUDA-only tooling
(Unsloth etc.) never on Mac; quantization differs (fp16/mxfp8 MLX vs CUDA
fp16); extraction endpoint list spans both machines with health-probe
preference order — a powered-off RTX box must degrade silently to the Mac
sidecar, never fail the batch.

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


---

## 9. IMPLEMENTATION STATUS LEDGER (code-verified 2026-07-03, 3-agent sweep + live 15/15 ingest receipt)

Legend: **LIVE** = wired + proven · **GATED** = built/tested, zero query-path call sites (flag flip
alone does nothing — wiring is the remaining work) · **PARTIAL** · **NOT BUILT**.

### §3 Ingestion — ~85% (the strong column; everything the 5-doc A/B exercises)
| Item | Status |
|---|---|
| S1 M2 capture (title/author/date/source_type) + routing_trace persisted | **LIVE** (docling_adapter finalize_source_meta; worker source_meta) |
| S2 chunker: semantic_split + routers 1–5 + escalation + semantic tier_c parents | **LIVE** (tier_chunker; probe-verified) |
| S3 Ghost A prose summary + domain | **LIVE** · `topics` still written (retirement pending) |
| S3 semantic schema (§10.1: semantic_chunk_type/key_terms/mechanisms/topic_key via Ghost A JSON + heal; topics RETIRED; promote lifts onto children) | **LIVE** (2026-07-03 receipt: healed parent → full shape; 28 children lifted) |
| S4/S5 promote-at-ingest (concepts/entity_ids/relation aggregates + stamps) | **LIVE** — after ws.qdrant_written (placement bug caught+fixed by live receipt) |
| S5 summary tree + heal guard + documents.doc_profile | **LIVE** (heal proven: blanked parent → healed → tree) |
| S6 write barrier | **PARTIAL** — flags tracked (mongo/qdrant_written, qdrant_failed); no complete-enum; **retriever does NOT skip incomplete docs** |
| Version stamps | **PARTIAL** — children get them via promote(); initial upsert + parent_chunks lack them |
| M2 versioning population (document_status/is_latest/supersedes) | **NOT BUILT** (schema defaults only) |

### §2 Contracts — ~50%
| Item | Status |
|---|---|
| 5 typed models + Stage-Contract CI test | **LIVE** (contracts.py, test_contracts.py) |
| Writers accept ONLY typed models | **NOT BUILT** — _build_parent/child_dicts, upsert_children, stash_ghost_b still untyped dicts |
| extract.v2 + `extractor` provenance ON THE WIRE | **NOT BUILT** — local emits v1; cloud defaults v1; ExtractionResult dataclass has no extractor field (contract exists on paper only) |
| RerankerInput consumed | **NOT BUILT** — reranker sends bare excerpt, no "Book › Section" prefix |

### §1 Identity spine — ~60%
| Item | Status |
|---|---|
| corpus/doc/parent/chunk/entity/fact keys across stores | **LIVE** (pre-existing + entity_ids payload join) |
| Neo4j Chunk.parent_id (ADD) | **NOT BUILT** |
| Entity.corpus_ids[] (ADD) | **PARTIAL** — RELATES_TO edges only, not Entity nodes |

### §4 Storage & summary policy — ~70%
| Item | Status |
|---|---|
| Host/container topology (§4.5), MLX guardrails, engine select (local/cloud/fallback/dual) | **LIVE** |
| Shared collections populated (children + doc_summaries w/ 7 profiles) | **GATED** — migration script only; ingest doesn't auto-embed profiles; QDRANT_SHARED_COLLECTIONS read by NOTHING |
| Parent summaries NEVER embedded (Funnel A retirement) | **NOT BUILT** — funnel_a still searches chunk_type='summary' unconditionally |

### §5 Retrieval (owner assembly design) — ~25% wired
| Item | Status |
|---|---|
| Waterfall allocate() (all 6 rules, byte-identical, 10/10) | **WIRED-GATED** 2026-07-03 (0bb421c) — W2 call sites live end-to-end behind WATERFALL_ASSEMBLY=false: assembly.py (parent grouping/orphans/entity lines, ONE parent \$in read) → RetrievalResult.packet → context_manager renders packet in allocator order → packet_hash in diagnostics. Probe: legacy vs waterfall side-by-side, hash-identical across runs (scripts_probe_waterfall.py); entity rung fed by P2 graph provenance. Legacy path bit-for-bit when off. A/B gate (golden battery + 5-doc probes) before flip; anchor lanes activate with TWO_LANE_ANCHORING |
| Two-lane anchoring (detector 6/6 + lane-aware packer) | **GATED** — detect_anchor_doc_ids never called; TWO_LANE_ANCHORING read by nothing |
| Tier-0 doc_summaries routing | **PARTIAL** 2026-07-03 — W1a LIVE: ingest auto-embeds doc_profile per doc (phase=tier0 hook, TIER0_AUTO_EMBED=true; real-ingest receipt, probe ranks fresh card #1 cross-corpus). W1b query-time routing still GATED (TIER0_ROUTING=false, zero call sites; probe = scripts_probe_tier0.py). Stale cards from deleted corpora persist (delete-cleanup TODO); tiny-doc batch verifier mismatch chipped |
| Promoted payload consumption | **PARTIAL** — entity_ids LIVE at graph tier (the vector↔graph join works); concepts[]/relation_families[] indexed-but-never-filtered |
| C3/B4 rank signals, answerability gates, stream retry, HyDE toggle | **LIVE** (earlier arcs) |
| G-PACK P1 offline fields (related_entities/graph_neighbors/neighbor_chunks/graph_degree at promote, integer+keyword Qdrant indexes) | **LIVE** 2026-07-03 (94c87eb+55d5b19; receipt 27/28 children, range filter 27 pts) |
| G-PACK P2 Mode A serving (§12.6 ladder: G3 TTL cache → G1 seed pref → A1 query entity linking → payload-first mentions hop, Cypher only under floor) | **LIVE** 2026-07-03 (b40da18; /api/chat log payload=13 linked=4 mentions_cypher=skipped, 32ms; 6 kill-switch knobs) |
| ENTITY-ID LAW: canonical = neo4j_writer.entity_id_from_name (HYPHENS) — underscore-slug fallback bug fixed; multi-word entities now join | **LIVE** 2026-07-03 (b40da18) |

### VERDICT: ~60–65% of the full plan. Ingestion half ≈ complete and live-receipted;
retrieval-consumption half is tested groundwork awaiting WIRING (not just flag flips).

### Close-out order (the Wire Phase, W1→W9)
W1 auto-embed doc_profile at ingest + Tier-0 doc_summaries query search · W2 waterfall wired
behind flag (replaces hydrate assembly when on) · W3 two-lane anchoring wired (detector →
lane tags → allocate) · W4 extract.v2 + extractor provenance on the wire · W5 write-barrier
enforcement in retriever · W6 Ghost A minimal schema (mechanisms/key_terms/semantic_chunk_type)
· W7 writers → typed contracts · W8 Neo4j Chunk.parent_id + Entity.corpus_ids[] · W9 facets-as-
projection + Funnel A retirement behind probes (+ topics migration, temporal fields with M2
versioning logic).


---

## 10. SEMANTIC SUMMARY PLAN + INITIAL WATERFALL INTEGRATION (owner-steered 2026-07-03)
**CONTINUATION DIRECTIVE: on session resume/compaction, execute THIS section in order.
It supersedes the generic W1–W6 ordering in §9 where they overlap.**

### 10.1 Parent summary — SEMANTIC STRUCTURE (replaces prose-only Ghost A output)
A parent summary is a structured object, not a blob. Stored FLAT on parent_chunks
(prose stays in `summary` for back-compat — it is the embeddable/waterfall "summary" rung;
the gist-risk gate keeps prose as the vector text):

```
summary                : str   — 2-3 dense sentences ("gist"; the waterfall summary rung)
semantic_chunk_type    : enum  — definition|claim|procedure|principle|framework|example|
                                 comparison|warning|narrative   (clamped; unknown → narrative)
key_terms              : list[str]  — <=8 proper nouns / defined terms found IN the parent
mechanisms             : list[str]  — <=5 transferable snake_case mechanisms
topic_key              : str   — f"{domain}.{slug(top_heading)}" (derived, deterministic)
domain                 : str   — existing taxonomy field (unchanged)
```

**Generation (one JSON call per BODY parent):** upgrade Ghost A prompt (services/ghost_a.py)
to return `{summary, semantic_chunk_type, key_terms, mechanisms}` as JSON; parse defensively
(clamp enum, snake_case+dedupe mechanisms, cap counts). The summary_tree HEAL guard produces
the SAME shape (shared prompt/parse helper — one implementation, two callers).
**Determinism guards:** extractive fallback fills `summary` only and leaves semantic fields
empty — never fabricate structure; `topic_key` computed in code, not by the LLM.
**Storage:** worker `_build_parent_dicts` + heal `$set` write the new fields; `topics` field:
STOP WRITING in the same change (subsumed by key_terms — the §3.S3 retirement step 1).
**Promotion:** promote() lifts `semantic_chunk_type`, `mechanisms[]`, `key_terms[]`,
`topic_key` from the chunk's PARENT onto the child Qdrant payload + Mongo child (closing the
§9 NOT-BUILT RetrievalPayload fields). Indexes ship in the same change.
**Consumers (stage contract):** summary rung text → waterfall; semantic_chunk_type → operator
match + diversity (rank-only, never a score multiplier); mechanisms → bridge/diversity lanes;
key_terms → concept recall; topic_key → routing + dedupe.

### 10.2 Document summary — USAGE CONTRACT
The doc profile (documents.doc_profile, L4 of the tree) is a ROUTING CARD, never evidence
(exception: explicit corpus-overview queries). Jobs, in order:
1. **Tier-0 routing:** query → vector search over `polymath_doc_summaries` (+ concept/topic_key
   overlap) → top-N candidate doc_ids with routing scores.
2. **Waterfall lane hints:** routed doc_ids bias candidate retrieval (soft boost — never a hard
   gate unless the doc is an ANCHOR from §5.1 title/author match).
3. **Coarse-to-fine descent map:** profile.section_ids → summary_tree sections → rollups →
   parent_ids gives a deterministic drill path for broad queries.
4. **Source card:** profile.summary supplies the "Best used for questions about…" line for
   citations/UI — display only.

**Structure (extend the L4 node + doc_profile mirror; owner card + tree fields):**
```
summary_id, doc_id, corpus_id, summary_type="document"
title, source_type, domain, topic_keys[]           (topic_keys = union of parent topic_keys)
concepts[], mechanisms[], patterns[]                (rolled up from parents, capped 12/8/8)
summary                                             ("what it is; what it covers; Best used for…")
section_ids[], parent_count, doc_date, status, latest
```

### 10.3 INITIAL WATERFALL PLAN (the first wiring — W2', behind WATERFALL_ASSEMBLY flag)
Data flow, deterministic end to end:
```
query ──► Tier-0: doc_summaries search ──► routed doc_ids (soft)      (10.2 job 1)
      └─► anchor_detect vs M2 title/author ──► anchor doc_ids (hard lanes)
existing funnels+rerank ──► ranked CHILDREN ──► group by parent_id:
    parent.score = max(child score) · lane = "anchor" if doc in anchors else ""
    ParentCandidate(full_text=parent.text, summary=parent.summary/gist)
orphan children = top-scored children whose parent didn't place (cross-domain)
entities = graph facts/relations lines (existing Mode A output)
allocate(ranked_parents, budget=CONTEXT_BUDGET_TOKENS(default 4000),
         orphans, entities, anchor_quota=0.6, spillover_threshold=rerank floor)
──► Packet ──► context_manager renders packet items IN ORDER (full/summary/child/entity)
──► packet_hash logged on every response (determinism receipt)
```
Rules already implemented in waterfall.py — this step is PURE WIRING + a renderer.
**Flags:** WATERFALL_ASSEMBLY=false (off until A/B green) · TWO_LANE_ANCHORING=false gates
only the anchor-lane tagging inside the flow. **A/B gate:** golden battery + habits-NN + the
5-doc corpus probes, legacy vs waterfall, before any default flip.

### 10.4 EXECUTION ORDER (do these IN ORDER on continuation)
1. **10.1 semantic parent summaries** — ghost_a JSON schema + shared heal helper + worker
   persist (+stop writing topics) + promote() lift + payload indexes + tests
   (parse-clamp determinism; heal shape; promote lift) + live 1-doc receipt.
2. **W1 Tier-0** — auto-embed doc_profile at ingest (summary_tree hook tail; reuse
   migrate script's embed logic) into polymath_doc_summaries; add retriever Tier-0 probe
   (flagged TIER0_ROUTING=false) returning routed doc_ids + scores; unit + live receipt.
3. **10.3 waterfall wiring** — WATERFALL_ASSEMBLY flag; parent grouping + orphan/entity
   assembly; packet renderer in context_manager; packet_hash in response metadata;
   A/B probe script (same query → legacy vs waterfall packets side by side).
4. Then resume §9 Wire Phase order (W4 v2-on-wire, W5 barrier enforcement, …).
Each step: asserting tests → docker cp iterate → REBUILD (CLAUDE.md stipulations) → live
receipt on the contract_preflight corpus → commit+push with proof → update §9 ledger row.


---

## 11. QUERY-LAYER REVIEW — faster · more precise · deeper · cross-domain (investigated 2026-07-03)
Grounded in a live query-path sweep (funnels/budgets/timings cited from retriever/__init__.py,
mode_a.py, ranking_policy.py). Owner question resolved first:

### 11.0 Universal document-summary collection — YES (ratified)
`polymath_doc_summaries` stays ONE collection across ALL corpora (corpus_id payload, indexed).
Reasons: (1) routing must SEE across corpora to route — per-corpus profile shards would
reintroduce cross-collection score stitching at the routing layer, the worst place; (2) it makes
cross-domain emphasis a single cheap vector search (the entry point, not an afterthought);
(3) enables corpus auto-selection for unscoped queries. Guards: ROUTING-ONLY (never evidence,
§10.2) so a wrong-corpus hit can never leak text; future isolation via `security_scope` payload
(owner index list) — property-based, never identity-splitting.

### 11.1 What the sweep found (the honest baseline)
- All funnels run CONCURRENTLY per tier (good), but every lane searches the FULL corpus scope —
  nothing prunes the universe before the expensive work. Budgets: Fast=A20+B40; Hybrid adds
  lexical 6–18 + anchors 4–8 (2.5s wall); Graph adds fact-seed 16 (5s, overlapped with embed) +
  Mode A (seed 8, limit 8, 4s, 3 parallel passes) → prefilter pool 64 → **rerank pool capped 16**.
- Hot spots: support_profile gap-fill funnels **5–7s vs 2–3.5s solo (cause unknown — investigate
  first)**; document_anchor 1.7–4.2s; Mode A ~7s tamed by concurrency.
- Diversity is doc-count-based MMR (tier-aware λ, graph reserve, sufficiency repair). **NO
  domain/mechanism/family-based diversity exists at query time**; concepts[]/relation_families[]
  are indexed-but-never-filtered; Funnel A still double-represents parents vs children.
- Caches: retrieval-result 120s, embed-config 300s, graph-metrics per-corpus. **Mode A expansion
  and rerank are UNCACHED.**

### 11.2 Universal levers (all tiers)
U1 **Tier-0 routing first** (W1): one doc_summaries search prunes the doc universe before every
   lane — speed (smaller pools), precision (on-topic docs), cross-domain (routes across corpora).
   Soft boost only; hard filter only for §5.1 anchors.
U2 **Promoted-payload soft prefilter with fallback**: query concepts → Qdrant filter on
   concepts[]/entity_ids; DETERMINISTIC fallback = if filtered candidate count < K_min, rerun
   unfiltered (never strands recall). semantic_chunk_type ↔ operator atoms as RANK-ONLY bonus
   (definition query prefers definition/principle chunks) — never a score multiplier.
U3 **Mode A expansion TTL cache** (entity-seed-set keyed, ~180s) + keep rerank uncached (CE is
   the authority; caching it risks staleness for marginal gain).
U4 Per-phase timing report already instrumented — surface it in trace metadata for tuning.

### 11.3 Fast tier
F1 Align to owner §5.5: Fast = CHILDREN only — drop Funnel A from qdrant_only (removes
   double-representation + one vector search). F2 U2 soft prefilter. Expected: sub-second
   retrieval phase.

### 11.4 Hybrid tier (default — biggest wins)
H1 **Replace Funnel A's parent-summary lane with the summary TREE**: rollup/section nodes as the
   breadth/enumeration lane (Tier-0 descent map §10.2-3), retiring per-parent summary vectors
   (the §4 policy) behind the probe battery. Breadth quality goes UP (rollups are already
   deduplicated meaning), candidate competition goes DOWN.
H2 **Waterfall packet** (§10.3) = depth + determinism (full 1–4, summaries 5–8, orphan children).
H3 **RerankerInput prefix** ("Book › Section\n…") — cheap CE precision, contract already exists.
H4 **Cross-domain in MMR**: add distinct-DOMAIN breadth (payload `domain`) next to distinct-doc
   breadth for BROAD/multi-lane queries + a mechanisms[]-overlap bonus (bridge emphasis) — both
   rank-only.
H5 **CLOSED 2026-07-03 (Q1 investigation — no fix needed).** The 5–7s anomaly no longer
   exists: it was killed by three earlier arcs (coverage passes rerank_enabled=False; coverage
   tier downgraded graph→hybrid; RERANK_EVIDENCE_SUPPORT default False after the Metal A/B —
   NOTE the code comment at chat_orchestrator ~4318 claiming "default on" is STALE, config is
   authoritative). Receipts on authentic_library (486 docs): isolated gap-fill shape
   (4 facets × 3 sequential variants, support_profile=True) TOTAL 1.51s, per-call 0.13–0.95s;
   in-situ during a real graph-tier turn: 8 support retrieves at 0.06–1.85s (p50 ~0.6s). Only
   residual: a 3-concurrent support burst right after the main pass showed embed=1.18s each
   (Metal embed queueing; solo=0.05–0.13s) — not on the turn's critical path, not worth fixing.
   **Where main-pass latency ACTUALLY lives now (real turn, main retrieval 10.89s):**
   rerank 3.47s (fp16 CE, pool 16 — Q3 territory) · funnels 2.52s of which anchor=2.50s
   (document_anchor dominates the funnel gather — H6 is the next latency prize) ·
   graph 2.25s (Mode A escalated to live Cypher because authentic_library has NO promoted
   neighbor_chunks yet — payload-first activates there only after re-promote/backfill) ·
   fact_seed 2.01s (overlapped with embed by design).
H6 document_anchor should match M2 title/author via anchor_detect (fast, indexed) before its
   heavy Mongo text path.

### 11.5 Graph tier (the cross-domain flagship)
G1 **Pre-expansion relation prefilter**: filter funnel-B candidates has_relations=true (+
   relation_families matched to query operators: causal→Causal) BEFORE Mode A seeding — less
   junk fan-out, faster Cypher, better seeds. (Sweep confirmed NO prefilter exists today.)
G2 **Raise GRAPH_MLX_RERANK_POOL 16 → 32–40**: the 16 cap is MLX-era conservatism; the fp16
   llama reranker handles 40 docs ≈1s. Depth win; verify Metal headroom (§4.5 targets).
G3 Mode A cache (U3). G4 **CROSS_DOMAIN_EMPHASIS knob (off|balanced|strong)**: scales the Phase
   5b bridge-lane bonus cap (fragile bridges / structural analogies / transfer candidates exist
   TODAY but are capped to limit//4), adds an entity_families/domain diversity reserve (≥1 slot
   from a different domain than the top doc on BROAD queries), and gates 2-hop expansion
   (GRAPH_REL_HOP2 knobs exist, unexposed).
G5 topic_key dedupe: two chunks sharing topic_key are near-siblings — MMR redundancy signal.

### 11.6 Execution order (extends §10.4; do AFTER #31/#32)
Q1 = H5 support-profile latency investigation — **DONE 2026-07-03, closed no-fix (see H5);
     next latency prize per its receipts = H6 document_anchor (2.50s of the 2.52s funnel gather)** ·
Q2 = U2 soft prefilter + G1 relation prefilter ·
Q3 = H3 RerankerInput + G2 pool raise (one A/B) · Q4 = H4+G4 cross-domain emphasis knob ·
Q5 = H1 tree-as-breadth-lane behind flag + Funnel A probes · each step probe-gated
(golden + habits-NN + seducer + packet_hash + latency per §4.5 targets).


---

## 12. TEMPORAL LAYER + GRAPH PRECOMPUTE + EMBEDDING-JOBS DOCTRINE (owner research 2026-07-03)

### 12.0 Confirmations (no change — owner research restates ratified design)
Doc profile answers ONLY: should this doc be searched / which sections / what topics — never a
1,727-parent digest. Final rule stands: parent=evidence map · rollup=local topic map ·
section=chapter map · document=routing card. Shared base contract (plan → fetch IDs+lane ranks →
merge/dedupe → prune/cap → light-hydrate → CE once → diversity curation → full-hydrate winners →
answer) = the §10.3 waterfall pipeline.

### 12.1 ONE TENSION, RECONCILED — what gets embedded
Owner research proposes purpose-built embeddings (child_text / parent_summary / mechanism /
book_concept: "don't make one embedding do every job") while §4 (owner-ratified) retires
per-parent summary vectors as duplicate representation. **Reconciliation (adopted):**
- PER-PARENT summary vectors stay RETIRED (a parent gist ≈ its own children = double hits).
- The embedding-jobs doctrine applies to TREE levels, which aggregate meaning children can't
  duplicate: **rollup/section embeddings = bridge/enumeration recall lane** (replaces Funnel A,
  = §11 H1) · **doc-profile embeddings = routing** (exists) · optional **mechanism-view
  embeddings** (short "mechanisms: compounding, feedback_loop — context…" strings per
  section) = cross-domain bridge recall. Each in its own small shared collection
  (polymath_tree_summaries, polymath_mechanisms) with corpus_id payload — same multitenancy
  doctrine as doc_summaries. Job-specific vectors, zero double representation.

### 12.2 TEMPORAL LAYER (new — closes the §9 'versioning NOT BUILT' row with real machinery)
Retrieval order for temporal queries: **1 time-validity (HARD) → 2 relevance → 3 authority →
4 recency → 5 diversity** — never "newest wins" (newest may be irrelevant).
- Hard filter: document_status=active · document_date<=as_of · effective_start<=as_of ·
  (effective_end null OR >as_of). Then relevance; curation tie-breaks: authority → newer
  document_date → better topic match.
- M2 versioning population becomes REAL: is_latest/supersedes derived at ingest by
  (source_identity, topic_key) grouping; denormalize document_status/is_latest/document_date to
  child payloads (fields already in RetrievalPayload + owner index list).
- **Temporal summary types** (summary_tree extension, embeddable in the tree collection):
  current_state · version_delta · supersession · timeline · **topic_latest** (primary:
  {topic_key, latest_summary, latest_source_date, is_latest, evidence[]}) — generated per
  topic_key from the latest active parents; answers frame as "As of <date>, based on the latest
  active documents…". Every retrieved unit must know: its date · active? · superseded by what ·
  which topic/version.

### 12.3 GRAPH PRECOMPUTE (new — live Cypher only for true traversal)
Precompute graph-derived metadata at INGEST onto summary/chunk records:
`related_entities[] · graph_neighbors[] (1-hop entity ids) · bridge_candidates[]` — extending
what Phase 5b's metrics cache started. Mode A becomes payload-lookup-FIRST (neighbors/bridges
read from promoted metadata), live Neo4j ONLY for genuine path/relationship traversal (2-hop,
path questions). Speed: removes per-query 1-hop Cypher; cross-domain: bridge_candidates are
pre-scored at ingest where compute is free.

### 12.4 ADDITIONS (assistant, answering "anything missing?")
A1 **Query-side entity linking (graph expansion via concepts)**: today Mode A seeds ONLY from
   retrieved chunks' mentions. Add: query terms → concepts[]/entity_ids payload match → DIRECT
   entity seeds even when no chunk surfaced the entity yet — the graph can now start from the
   QUESTION, not just the results. Deterministic (indexed lookup, no LLM).
A2 **Path micro-summaries** (optional, later): precompute embeddable one-liners for strong
   RELATES_TO paths ("A —uses→ B —enables→ C", w/ evidence chunk ids) for relationship recall.
A3 **retrieval_role on summary records** (owner object): tag tree nodes bridge_summary |
   enumeration | routing so lanes select by role, not heuristics.
A4 Authority field: source_type-derived rank (standard>paper>book>blog) for the temporal
   tie-break — deterministic map, owner-tunable.

### 12.5 Order update — OWNER STEER: GRAPH PACKAGE FIRST
Owner intent (2026-07-03): the §12 material exists to improve GRAPH AUGMENTATION — expansions,
seeds, and HOP TIME. Execute the G-PACK before W1/W2:
  P1 precompute-promote: promote() adds per-chunk `related_entities[]` (doc-local, from the
     chunk's extraction relations — endpoints as entity ids) + `graph_neighbors[]` (cross-doc
     1-hop from Neo4j at promote time, capped 12, sorted/deterministic, best-effort) — indexed.
     Hops become payload reads; live Cypher reserved for true multi-hop/path traversal.
  P2 Mode A upgrades — **SHIPPED 2026-07-03 (55d5b19 offline + b40da18 serving)**:
     (a) A1 query entity linking (query n-grams → canonical entity_id existence → DIRECT
     seeds AND direct evidence, `relation_family=query_entity_link`); (b) payload-first
     mentions hop via `neighbor_chunks[]` (doc-local python adjacency + cross-doc MENTIONS,
     cap 8) — live co-mention Cypher ONLY when validated payload candidates <
     GRAPH_PAYLOAD_MIN_CANDIDATES (default 4); (c) G3 TTL cache 180s keyed
     (corpora, seed window, limit, query); (d) G1 has_relations seed preference.
     ONE Mongo $in read serves (b)+(d). Kill-switches: GRAPH_PAYLOAD_FIRST,
     GRAPH_QUERY_ENTITY_LINKING, GRAPH_EXPANSION_CACHE_TTL_SECONDS,
     GRAPH_SEED_PREFER_RELATIONS. Receipt: 32ms/8 chunks, payload=13 linked=4
     mentions_cypher=skipped; cache hit 0ms; same line observed in live /api/chat.
     **ENTITY-ID LAW (bug fixed here): canonical id = neo4j_writer.entity_id_from_name
     (NFKD → strip punctuation → collapse spaces → HYPHENS, alias-map resolved).
     promote/backfill had probed non-existent fn names and silently fell back to an
     underscore slug — every multi-word entity failed the vector↔graph join. Any new
     code minting entity ids MUST import entity_id_from_name (or replicate exactly);
     never underscore slugs. Fallback replicas live in promote._default_entity_id and
     graph_payload._default_entity_id.**
     Still open in expanded P2 scope: bridge_concepts[] promotion, graph_roles[],
     fact-seed cache, deep-2-hop explicit-intent knob.
  P3 G2 rerank-pool raise 16→32 rides the first graph A/B.
Then W1 Tier-0 → W2 waterfall → temporal W-T as previously ordered.
Temporal layer lands as **W-T** after Q2 (needs M2 versioning population + topic_key, which
§10.1 shipped). Graph precompute lands with Q2/G1 (same promote() pass). §11.6 otherwise stands.


### 12.6 GRAPH DOCTRINE (owner, 2026-07-03): monolithic intelligence OFFLINE · waterfall serving ONLINE
**"Do the expensive graph thinking before the query. Use waterfall only during the live query."**

Offline (ingestion/background — the graph may be HEAVY here):
GLiNER/GLiREL → entities → relations → facts → concept bridges → mechanism bridges → entity
neighborhoods → graph cache tables. Cheap graph-derived fields ride the chunk/summary payloads:
```
concepts[] mechanisms[]                       (§10.1 — SHIPPED)
related_entities[] graph_neighbors[]          (P1 — SHIPPED, entity-id level)
neighbor_chunks[]      ← NEW: top-8 graph-adjacent CHUNK ids (precomputed mention-walk;
                          query-time expansion becomes a pure id lookup)
bridge_concepts[]      ← NEW: cross-domain bridge concepts from Phase-5b analogy/gap metrics
graph_degree           ← NEW: connectivity scalar (payload-read graph boost, replaces live
                          PageRank lookup in the hot path)
graph_roles[]          ← NEW: bridge|hub|definition|supporting_fact (graph-derived; merges
                          with §12.4 A3 retrieval_role)
fact_seed cache        ← NEW: per-entity top facts precomputed (today's live 5s fact-seed
                          lane becomes a read)
path sketches          (§12.4 A2 — planned)
```

Online (query time — the ESCALATION LADDER, never pay full graph cost by default):
```
Hybrid lanes ──► cached graph signals (payload reads: neighbors/degree/bridges/facts)
            ──► SHALLOW live expansion ONLY IF: graph-supported candidates < floor OR a
                relationship atom is uncovered by cached signals
            ──► DEEP live traversal (2-hop/paths) ONLY IF: explicit path/multi-hop intent
                or owner knob — one CE rerank once, hydrate winners only
```
This SUBSUMES §12.3 and expands P2: escalation triggers are deterministic (candidate-count
floor + atom coverage), never LLM judgment. P2 scope now includes neighbor_chunks,
bridge_concepts, graph_degree promotion, fact-seed cache, and the ladder in Mode A.
