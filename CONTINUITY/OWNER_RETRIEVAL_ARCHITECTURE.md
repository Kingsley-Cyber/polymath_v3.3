# Owner Retrieval Architecture — Deterministic Hydration Waterfall + Two-Lane Anchoring

**Author: King (owner-designed and planned).** Recorded + repo-validated 2026-07-02.
This is the governing retrieval/assembly design for the re-architecture. It composes with
SCHEMA_METADATA_MAP.md (the gap map) and supersedes the parts of REBUILD_IMPLEMENTATION §3 and
recent shipped work named in §3 below.

---

## 1. The design

### 1.1 Layer contract
- **Search happens in Qdrant only** (child dense vectors + BM25 sparse + cross-encoder rerank).
  **MongoDB is a fetch layer, never a search layer** — parent text and parent summaries are pulled
  from Mongo *after* ranking. (Labeling hybrid "(MongoDB)" misattributes it.)
- **Fast (Qdrant):** retrieve 12–20 child chunks cross-domain. Summaries are NOT a fixed top-8 —
  it's **one summary per unique parent of the retrieved children (deduped), so the count varies.**
- **Hybrid:** full parents ranks 1–4 (depth) + parent summaries ranks 5–8 (enumeration/breadth,
  summaries of the RERANKED parents 5–8, diversity-capped — not of the orphans' parents) +
  5–8 **orphan children** from other books/domains fed as raw fragments (the cross-domain
  fragments fast mode finds). Dedupe rule: never feed a child whose parent is already a top-4
  full text. Optional refinement: if an orphan's parent happens to sit in ranks 5–8 its summary is
  already present; if not, optionally attach that orphan's parent summary (token cost vs fragment
  context — a knob).
- **Graph (Neo4j):** entity seeds from included parent summaries + selected child chunks;
  relationship expansion **scoped to the chosen domains only**.

### 1.2 The deterministic hydration waterfall (assembly)
Fixed budget, fixed priority order, fixed downgrade rules — no LLM judgment in assembly:
1. **Fix budget** (e.g. 4k context tokens). Rerank produces ONE ranked parent list.
2. **Walk ranks top-down**, hydrating each parent at the richest form that fits the remaining
   budget: **full text → summary → skip**. Ranks 1–4 naturally land as full texts (~60% of
   budget); 5–8 fall to summaries automatically.
3. **Fill leftovers in fixed order:** orphan children (cross-domain, deduped against included
   parents) → shared entities last.
4. **Overflow rule:** if a parent's full text doesn't fit, swap to its summary — **never truncate
   mid-text.**
5. **Surplus rule:** budget remaining after all slots → promote the next summary up to full text.
6. **Dedupe rules:** drop any child whose parent is included; drop any summary whose parent is
   full-texted.
- "Exhausted parents" is the same ladder: when full texts stop fitting, the allocator degrades
  gracefully through summaries and children instead of cutting off.
- **Deterministic:** same query + same candidates + same budget ⇒ identical context. Rules, not
  LLM judgment. (Matches REBUILD's packet_hash determinism acceptance.)

### 1.3 Two-lane anchored retrieval
1. **Anchor detection:** the preprocessing LLM call also extracts named sources/authors ("Robert
   Greene", "Art of Seduction") → matched against doc metadata (author/title fields) → matching
   doc_ids tagged as anchors. Routing is a METADATA MATCH, not LLM judgment.
2. **Anchor lane:** retrieval hard-filtered to anchor doc_ids with guaranteed slots — ranks 1–4
   full parents must come from the anchor. That is the grounding.
3. **Expansion lane:** parallel retrieval over the corpus EXCLUDING anchors — fills summaries +
   orphan-children slots (the elusive cross-domain hydration).
4. **Quota'd waterfall:** fixed budget split (e.g. 60% anchor / 40% expansion); each lane hydrated
   by the same waterfall rules. No anchor detected → collapse to single-lane normal retrieval.
5. **Spillover rule:** anchor slots are filled only while candidates clear a fixed rerank-score
   threshold. If only 2 anchor parents qualify, the leftover anchor budget spills to the
   expansion lane, promoting cross-domain parents/summaries up the waterfall. The threshold — not
   an LLM — decides, so determinism holds.

### 1.4 Summary embedding policy
- **Parent summaries: never embedded, never searched.** They are MongoDB-only, OUTPUT-side
  artifacts pulled after rerank to compress context (ranks 5–8).
- **Doc-level summaries are the exception:** embedded in a small separate Qdrant collection
  (`doc_summaries`) and searched ONLY for Tier-0 routing / anchor filtering — never fed as
  evidence unless the query is a broad "what is this corpus about."
- **Why separated:** child embeddings already represent parent content in vector space; embedding
  parent summaries would create duplicate hits competing with children and muddy rerank
  precision. Doc summaries are embedded because they do a job nothing else covers:
  routing/anchoring at corpus scale.

### 1.5 Multi-corpus Qdrant (multitenancy)
- **No collection-per-corpus.** Two shared collections (`children`, `doc_summaries`) + an INDEXED
  `corpus_id` payload field. Multi-select = one search with a `corpus_id` match-any [a,b,c]
  filter; ranking merges natively — no cross-collection score stitching, less HNSW/memory
  overhead.
- **Repo mapping:** each `corpora/<name>/` folder holds source files + an ingestion config
  (corpus_id, domain labels, chunking params); ingestion writes into the shared collections
  tagged with that corpus_id. The SAME field is mirrored in MongoDB docs and as a Neo4j node
  property so all three stores filter identically.

---

## 2. Repo validation — real warts this fixes (verified)
- **Per-corpus collections exist today** (`ensure_collections_for_corpus`,
  qdrant_writer.py:422–489) → multi-corpus queries stitch scores across collections whose scores
  are not comparable. Multitenancy removes a live bug class.
- **Parent summaries ARE embedded today** (`upsert_summaries` qdrant_writer.py:663–746;
  Funnel A searches `chunk_type="summary"`). The double-representation critique is correct
  against the running system.
- **Assembly is a binary mode today** (`HYDRATION_MODE` parent|child_summary). The waterfall
  subsumes both as rungs and adds budget-determinism.
- **Anchoring exists only as heuristics today** (document_anchor retriever + evidence-plan
  lanes + per-side allocation). The two-lane design formalizes them with cleaner, deterministic
  rules (lanes → anchor/expansion; protected slots → guaranteed ranks; spillover threshold).

## 3. What this supersedes (explicit, incl. recently shipped work)
| Superseded | By |
|---|---|
| `HYDRATION_MODE=child_summary` default (blessed 2026-06-30) | a rung of the waterfall ladder |
| Funnel A parent-summary vector search | `doc_summaries` Tier-0 routing (retire behind A/B gate) |
| B2 query-guided parent excerpt (flagged) | optional middle rung between full text and summary — owner's call |
| evidence-plan per-side allocation mechanics | anchor/expansion lanes + quota'd waterfall |
| per-corpus Qdrant collections | shared collections + corpus_id multitenancy |

## 4. New hard dependencies this creates
1. **M2 metadata is now a PREREQUISITE, not a nice-to-have:** anchor detection matches on
   author/title/source_book — fields that do not exist yet (SCHEMA_METADATA_MAP "MISSING" row).
   Parse-time capture (REBUILD S1) must land before two-lane anchoring can route.
2. **Doc-level summaries are new capture:** only parent-level summaries exist today; Ghost A
   needs a doc-level pass + the `doc_summaries` collection.
3. **Multitenancy migration:** re-ingest into shared collections or a collection-merge script;
   payload index on corpus_id created in the same migration.
4. **Parent-summary coverage:** every parent needs a summary for the waterfall's rung-2
   (authentic_library backfilled; other corpora need verification).

## 5. Hardening notes (scribe's two additions — the only edits)
- **Pin anchor extraction** so determinism holds end-to-end: lexical author/title match FIRST,
  LLM extraction as fallback at temp 0, cached by normalized query. Routing is already
  deterministic; this makes the extraction input deterministic too.
- **Retire Funnel A behind the probe battery** (golden + habits-NN + seducer), not a hard cut —
  breadth regressions must be caught before the summary-vector path is deleted.

## 6. Composition with the rest of the re-architecture
- The waterfall is the **P4 consumption contract** the gap map calls for; the identity spine
  (corpus/doc/parent/chunk) is what makes its dedupe rules O(1).
- promote() (gap 2) feeds it: `concepts[]`/`entity_ids[]` sharpen orphan-children cross-domain
  selection and the graph layer's domain-scoped expansion.
- Cross-cutting guardrails carry over unchanged: cross-encoder is the sole ranking authority;
  domain is a soft boost never a gate; determinism (packet_hash) is an acceptance test.
