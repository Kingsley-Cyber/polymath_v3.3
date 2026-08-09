# EXECUTION BRIEF — Passive Doc-Artifact Layer (`doc_artifact.v1`) + related_to Mitigation
2026-07-06 · ratified in planning with owner (King) · paste-ready handoff for an execution agent

---

## 0. MISSION (BLUF)

Build a **passive, document-level knowledge-artifact layer** so chat synthesis knows *what each retrieved
source is FOR* (model-specific advice vs film theory vs workflow guidance) without changing retrieval at all.
Artifacts **explain retrieved sources; they never choose them.** Secondary package: mitigate the
`related_to` fallback predicate at the graph layer (write-time family fallback + query-time
pointer semantics).

Owner's use case: corpora mixing AI-video-model docs (Kling, Seedance), film/directing/storytelling books,
camera-motion references, and tutorial transcripts. Target query class: *"I need a prompt for Seedance that
tells a story about a product I'm selling"* — the model must take syntax from Seedance docs, structure from
film theory, ordering from tutorials, and must NOT blend Kling syntax into Seedance prompts.

---

## 1. REPO + OPERATIONAL STATE (verify, don't assume)

- Repo: `/Users/king/polymath_v3.3` · branch `codex/ingestion-contract-checkpoint` · Mac M1 Studio 32GB.
- **Ingest runs in the `polymath_v33-ingest-worker-1` container** (offline-ingest overlay). The backend
  container is API/query-only (`INGEST_RUNNERS_ENABLED=false`). Never debug ingest via backend logs.
  Deploy/recreate command (all three files + profile, or services silently drop):
  `docker compose -f docker-compose.yml -f docker-compose.override.yml -f docker-compose.offline-ingest.yml --profile offline-ingest up -d backend ingest-worker`
- A ~460-file batch (corpus `999b5934`, batch `e4676f14`) is mid-flight with `chunk_summarization=false`
  → **most existing docs have NO `doc_profile`** (summary tree deferred). This is why the artifact
  compiler must tolerate `doc_profile=None` (see §5).
- Frontend container serves build-time dist — rebuild the container to see UI edits.
- `.env` is machine-local; **never commit keys or .env**.
- Tests are standalone-runnable (`python3 tests/test_*.py`, non-zero exit on failure). Owner's standing
  rule: **prove changes with an automated asserting test run GREEN before committing; log-greps and manual
  curls are not proof.** Backend containers don't bake `tests/` — assert via `docker exec` self-asserting
  scripts when needed, non-destructively.
- Owner's working rules: BLUF communication; set numeric targets BEFORE building (hypothesis-promotion);
  every fallback must count + surface its rate (no silent fallbacks); commit only deterministic, repeatable
  work.

---

## 2. INTENT + NON-NEGOTIABLE DOCTRINE

Owner's fears, each with its structural guarantee (these are requirements, not aspirations):

| Fear | Guarantee (enforced how) |
|---|---|
| Artifacts steer retrieval | Artifact text is **never embedded**; no retrieval code path reads artifact fields. Enforced by the **retrieval-set equality test** (§8) — identical queries with artifacts present vs stripped must return identical chunk-ID sets. |
| Over-trust of artifacts | Rendered as a labeled curator note, "context only, not evidence". Citation machinery untouched — artifact text can never be a citable span. Child chunks remain sole evidence authority. |
| Docs without artifacts get skipped | Absence is a no-op: null projection → nothing rendered → zero branches on presence. |
| Slows the fast path | Zero new queries (rides the existing per-doc Mongo fetch in hydrate Pass 2). Bounded: ≤8 sources (source_cap) × ~80 tokens. p50 delta target **< 20 ms**. |
| New abstraction muddies design | Extends the existing `documents.doc_profile` subdoc + the existing typed-schema/promote() pattern + existing PacketItem kinds. Zero new collections, zero new pipeline stages. |
| Kills open-endedness | Expansion lane and all scoring untouched. Artifacts change how the LLM *frames* what was already chosen — nothing else. |

**Producer precedence spine: owner > deterministic code > LLM fallback.** Machine never writes or
overwrites owner-authored fields. Every field carries its own provenance (per-field `_source`).

---

## 3. SCHEMA — `doc_artifact.v1` (final, owner-ratified)

Extends `documents.doc_profile` (subdoc on the existing Mongo `documents` record — NOT a new collection):

```jsonc
{
  // human-only; "why this doc is in the corpus"; fallback chain: doc → corpus.description → nothing
  "owner_intent": "shot-language vocabulary for prompt writing",
  "owner_intent_source": "owner",              // only legal value today; slot kept for future importers

  "source_role": ["model_specific_advice"],    // enum: model_specific_advice | technique_theory |
                                               // workflow_guidance | reference_material |
                                               // example_prompts | research
  "source_role_source": "deterministic",       // owner | deterministic | llm | template
  "source_role_confidence": 0.86,              // REQUIRED iff _source=="llm", absent otherwise

  "model_scope": ["seedance"],                 // null = general; from entity registry match
  "model_scope_source": "deterministic",

  "synthesis_hint": "Model-specific syntax reference for Seedance — authoritative for capabilities/constraints.",
  "synthesis_hint_source": "template",         // template-rendered from role+scope; NOT LLM prose (injection surface)

  "artifact_version": "polymath.doc_artifact.v1",
  "generated_at": "<utc>"
}
```

Rules:
- `owner_intent` + any field with `_source=="owner"` are **immutable to all machine passes** (backfill,
  re-stamps, refinement).
- **Quarantine rule (mechanical, not vibes):** `_source=="llm" && confidence < 0.7` → renderer omits that
  field entirely; UI shows "unconfirmed — click to set"; backfill re-visits.
- `synthesis_hint` is template-rendered (deterministic voice, no injection risk). LLM-written hints are a
  later experiment, not v1.

---

## 4. ANCHOR MAP (verified 2026-07-06 — re-verify line numbers before editing; they drift)

Write path / doc-level data that already exists:
- `backend/services/ingestion/summary_tree.py:375-392` — stamps `documents.doc_profile`
  {summary_id, summary, concepts, domains{name:count}, section_ids, schema_version, updated_at}.
  Hook site for artifact generation is immediately after this in worker.
- `backend/services/ingestion/worker.py:3469-3486` — B3 summary-tree call (best-effort try/except,
  never fails ingest). Artifact call goes right after, same tolerance.
- `backend/services/ingestion/worker.py:2149-2182` — documents record assembly (`facet_profile` at ~2171;
  full field list incl. source_meta title/author/document_date/source_type/routing_trace).
- Mongo `ghost_b_extractions` (staging, written by `stash_ghost_b`) — **authoritative per-doc entity store**;
  `chunks.entity_ids` copies are sparse on legacy docs (~53k of 702k chunks promoted). Verify nothing prunes
  this collection (resume rehydration depends on it — believed permanent).
- `backend/services/ghost_a.py:50-56` — `_DOMAIN_TAXONOMY` (19 values, dev/AI-centric). **Gap: no film
  category** — add `film_craft` (one-line vocab change; flows into doc_profile.domains on future ingests).
- `config/ontology.yaml` — entity_types + allowed predicate pairs. Role enum + video-model registry live
  beside this (new small config, e.g. `config/video_models.yaml`: kling/seedance/veo/sora/runway/pika/luma
  + aliases).
- `backend/services/ingestion/enrichment_gate.py` — E1 verdict (`related_to>40%` trigger) +
  `select_enrichment_tasks` priorities (gaps→empty→predicate-ambiguous→fact-thin). Package B rides this.
- Corpus record has user-provided `description` (`backend/services/ingestion_service.py:866-877`) — the
  corpus-level owner_intent fallback, free on day one.
- Pattern twins: `backend/services/ingestion/extraction_contract.py` (pure resolver + standalone test),
  `scripts/summary_backfill.py` (resumable backfill).

Read path (attach + render):
- `backend/services/retriever/hydrate.py:155-167` — Pass 2 fetches the `documents` record per retrieved doc
  for `doc_name`. **THE attach point**: extend this projection with doc_profile artifact fields; hang on
  SourceChunk as optional attr. Zero new queries.
- `backend/services/context_manager.py:494` — `build_augmented_prompt` renders `From "[doc_name]":` per
  chunk, provenance arrows (≤3/chunk, lines ~658-690), and an **answer-policies block** (where
  `task_mode: generate` goes).
- `backend/services/chat_orchestrator.py:2406` — `_build_budgeted_augmented_prompt`; packet injection ~7445.
- `backend/services/retriever/waterfall.py:143` — `allocate()` → PacketItem(kind, doc_id, lane, tokens).
  New kind `doc_note`, **lowest priority — dropped first under token pressure, before any chunk downgrade**.
- `backend/services/retriever/assembly.py:36` — `group_parent_candidates` (doc grouping precedent).
- `backend/services/facets/final_selector.py:48` — source_cap=8 (bounds artifact count per query).
- `backend/services/ingestion/tier0.py` — `polymath_doc_summaries` Qdrant collection (doc_profile.summary
  embedded for Tier-0 routing; **INERT — zero query call sites. DO NOT add artifact text to anything
  embedded. Do not wire Tier-0. Out of scope.**)

Owner-intent capture:
- `backend/routers/ingestion.py:133-147, 1188-1253` — batch request/upload Form (NO per-file metadata today).
- `backend/services/ingestion/batches.py:335-388` — `_file_item_doc` (per-item schema, no intent field).
- `frontend/src/components/corpus/CorpusDetail.tsx:459-485` + `frontend/src/lib/api.ts:519+` — upload flow.
- V1 capture = **annotation-first** (Library panel per-doc editor → PATCH), NOT upload-form-first
  (500 books are already ingested). Upload-form capture is a later increment.

---

## 5. COMPILER DESIGN (Package A core)

New file `backend/services/ingestion/doc_artifact.py` — **pure function**, no I/O, standalone-testable
(mirror `extraction_contract.py`):

```python
build_doc_artifact(
    doc_profile,        # dict | None  ← MUST tolerate None (summaries deferred on current corpus)
    facet_profile,      # dict | None
    source_meta,        # title/author/source_type/routing_trace (parse-time, always present)
    ghost_b_entities,   # from ghost_b_extractions staging — see fallback chain below
    chunk_kind_stats,   # {code: n, prose: n, ...}
    owner_fields,       # existing owner-authored fields — pass through untouched, always win
) -> DocArtifact
```

Deterministic rules first (need NO summaries):
- `source_type == youtube_transcript` → workflow_guidance
- title/parseBookMeta patterns ("documentation", model-name entity in title) → model_specific_advice / reference_material
- chunk_kind mix (code-heavy → reference_material)
- `model_scope`: match ghost_b entities (typed spans, type ∈ {Software, Product}) against the registry
  with aliases; require mentions in **≥2 distinct chunks** before claiming scope. Entity-based matching —
  never raw-text substring (avoids "seedance"/"dance" traps).
- Entity read fallback chain: `ghost_b_extractions(doc_id,corpus_id)` → `chunks.entity_ids` →
  `doc_profile.concepts` + title heuristics → scope stays null (extraction=off docs).

LLM fallback (only when deterministic rules score ambiguous AND `doc_profile.summary` exists):
one cheap call on the profile summary (~200 tokens in), constrained to the role enum, stamped
`_source="llm"` + confidence. If profile absent → leave field empty with pending marker; backfill upgrades
it after summary backfill lands. **The artifact lane must never block or fail ingest** (best-effort, like
summary_tree).

Generation points:
1. Ingest hook: worker.py, right after the B3 summary-tree block, None-tolerant.
2. `scripts/artifact_backfill.py` — twin of summary_backfill.py: resumable, idempotent upsert, filtered by
   `artifact_version`, **skips every `_source=="owner"` field**, re-stamps pending/low-confidence fields.
"Recompile" = re-run backfill after improving rules; owner fields survive every recompile.

---

## 6. READ PATH (Package A render)

1. hydrate.py Pass 2: projection += artifact fields → `SourceChunk.doc_artifact` (optional attr).
2. context_manager.build_augmented_prompt: on the FIRST chunk of each distinct doc, prepend one header:
   `[Source: "Directing the Story" — role: film technique theory; owner note: "shot-language vocabulary" — context about the source, not citable evidence]`
   Apply the quarantine rule (§3). ~80 tokens max per header.
3. waterfall allocate(): PacketItem kind `doc_note`, lowest priority. Legacy (non-waterfall) path renders
   the header inline without a packet item.
4. **`task_mode: generate` answer policy** (same PR — the other half of the fix): deterministic
   query-cue detection (imperative + artifact noun: "write/generate/create … a prompt/script/shot list")
   → inject a policy into the existing policies block: treat sources as *ingredients not evidence*;
   output = final prompt block + one line per element naming which source drove it. **Query-driven,
   default OFF — never always-on.**
5. UI: LibraryPanel role chip + "why is this here?" per-doc editor (CorpusDetail) →
   `PATCH /api/corpora/{corpus_id}/documents/{doc_id}/artifact` (writes owner fields, sets `_source="owner"`).

---

## 7. BUILD ORDER + GATES

- **A0** — Pin `doc_artifact.v1` + passivity doctrine + this build order into
  `CONTINUITY/POLYMATH_ARCHITECTURE.md` (owner's standing rule: ratified designs live in the north-arrow
  doc FIRST). Add `film_craft` to `_DOMAIN_TAXONOMY`. Create `config/video_models.yaml`.
- **A1** — Compiler (`doc_artifact.py` + standalone test: same inputs → same artifact; owner fields
  preserved; ambiguous → safe fallback; None profile → deterministic-only) + hydrate projection +
  renderer header + `task_mode: generate` policy + **retrieval-set equality test**. GATE: equality test
  green + all unit tests green + p50 delta <20ms on a live probe.
- **A2** — waterfall `doc_note` PacketItem kind (drop-first proof: force a tiny budget in a test, assert
  artifacts drop before any chunk downgrades).
- **A3** — PATCH endpoint + Library panel annotation UI (frontend container rebuild to verify).
- **A4** — ingest hook (worker.py) + `artifact_backfill.py`; run backfill on corpus `999b5934`
  AFTER its summary backfill (or run now for deterministic-only fields — safe by design).
- **A5** — A/B receipt: ~10 real owner queries (Seedance/Kling/product-story class), same retrieval,
  blind judge on register separation. Numeric targets fixed BEFORE the run. Only after this receipt may
  anyone even PROPOSE phase-2 "active" mode (model_scope as soft rerank boost) — separate hypothesis,
  separate gate.

Commit style: small deterministic commits, tests green first (owner's assert-before-commit rule).

---

## 8. THE PASSIVITY PROOF (must exist before A1 merges)

Automated test (standalone or docker-exec script, non-zero exit on fail):
1. Fix a query set (≥5 queries incl. one generative-task query).
2. Run retrieval with artifacts present; record final chunk-ID sets + packet composition minus doc_note.
3. Strip/mask artifact fields; run identical queries; assert **identical chunk-ID sets**.
4. Assert p50 retrieval-latency delta < 20ms across the set.
This test IS the definition of "passive". If it ever goes red, the layer is misbuilt — stop and fix,
do not tune around it.

---

## 9. PACKAGE B — `related_to` MITIGATION (graph layer; separable, execute after A1 or in parallel by a second agent)

Context: GLiREL (pure BERT) fallback predicate `related_to` sits at ~50% of edges — that's the ratified
floor on clean entities (100% related_to = junk *entities* → route to entity re-extraction, NOT predicate
refinement). Harm = flat fallback + dead-end edges + unbounded traversal. Fixes by layer:

Write-time (deterministic, in Ghost B post-processing / graph write path):
- **Family fallback**: when GLiREL top label < threshold but top-k score mass concentrates in one predicate
  family → emit family-typed edge (predicates already have families — provenance renders `predicate(family)`).
  `related_to` only when even the family is ambiguous.
- **Ontology-pair promotion**: if (TypeA,TypeB) admits exactly ONE legal predicate in ontology.yaml →
  promote deterministically, stamp `promoted_by=ontology_pair`.
- **Edge properties on every surviving fallback**: `candidates:[{uses:0.41},{implements:0.38}]`,
  `fallback:true`, + **evidence span** (sentence or chunk_id+offsets). The evidence span is the single most
  important addition — it turns dead-end edges into pointers back into text.

Async (existing E1 machinery — enrichment_gate.py already has `predicate-ambiguous` priority + the
`related_to>40%` trigger): refinement task = entity pair + evidence sentence → constrained-choice LLM
(RTX vLLM/Qwen) over the ontology label set (+ "none") → replace-merge edge. Edge states:
`typed | family | fallback | refined`. Budget-bounded: top-N edges by entity degree × query-hit frequency.

Query-time (graph expansion + fusion + prompt):
- `related_to` participates at **depth 1 only** (typed/family edges may go depth 2). Never chain fallbacks.
- Fusion weight ~0.5× typed edges (tunable knob) — discount, never zero.
- **Fan-out cap** per seed on fallback edges: top-k by candidate score × inverse target-entity degree.
- **Evidence hydration**: when a path crosses a fallback edge, surface the stored evidence sentence (or
  seed its chunk) instead of rendering `A related_to B`. Provenance arrows: typed edges always outrank
  bare fallbacks for the ≤3 slots; a fallback arrow renders only if hydrated with its phrase.

Metric (silent-fallback accounting rule): per-corpus `% typed / % family / % fallback / % refined` on the
existing graph-coverage chip. Targets set in advance: write-time fixes → related_to ≤30%; after E1
refinement of top edges → ≤15% effective.

---

## 10. OUT OF SCOPE / DO-NOTS

- Do NOT embed artifact text anywhere (no Qdrant, no Tier-0 payload additions, no BM25).
- Do NOT add filters/boosts reading artifact fields in retrieval (that's phase-2, gated on A5 receipts).
- Do NOT let artifacts count as citations or occupy evidence budget ahead of chunks.
- Do NOT machine-write owner fields, ever (backfill included).
- Do NOT block or fail ingest from the artifact lane (best-effort like summary_tree).
- Do NOT touch: chunking, embedder identity (Qwen3-Embedding-0.6B/1024), extraction engines, batch runner,
  Neo4j write path (except Package B edge properties), answerability gates.
- Do NOT commit .env or any API keys. Do NOT hand-edit Mongo in prod without a receipt query before/after.
