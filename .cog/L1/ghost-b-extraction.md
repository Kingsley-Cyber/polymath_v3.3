# Ghost B (entity/relation extraction) — durable knowledge

## The root-cause hierarchy (in order of likelihood)

When Ghost B fails or under-extracts, check in this order:

1. **Reasoning-mode model is on**. If model is `deepseek/deepseek-v4-flash`
   (or any *-flash / *-pro reasoning variant) and `thinking` isn't
   disabled, reasoning tokens eat the entire output budget. Verify via
   `usage.completion_tokens_details.reasoning_tokens` in the LiteLLM
   response. Fix: ensure `payload["thinking"] = {"type": "disabled"}` is
   set for `deepseek/*` models (see `backend/services/ghost_b.py` ~2666).

2. **`max_tokens` too small**. Realistic output for dense chunks is
   ~600–1500 tokens; rescue path needs >= normal. Defaults now:
   `EXTRACTION_MAX_TOKENS=6144`, `EXTRACTION_RESCUE_MAX_TOKENS=4096`.

3. **Line cap below per-type theoretical max**. Per-type caps total
   `entities + relations + facts + sentinel`. Parser line cap must
   exceed this or dense chunks truncate. Defaults now:
   `EXTRACTION_MAX_TOTAL_LINES=55` (above normal max 40),
   `EXTRACTION_RESCUE_MAX_TOTAL_LINES=30` (above rescue max 22).

4. **Failure budget circuit breaker tripped**. After 20 chunks
   processed, if `failed/processed >= 25%`, remaining queued chunks
   are marked `not_processed`. Authoritative count: `documents.
   ghost_b_metrics.failed_chunks`. Audit collection is sampled
   (cap = 200/doc post-fix).

5. **tier_chunker stall** on docs with no sentence boundaries
   (code/math/tables). `_split_at_boundary` falls back to sentence
   splitting which O(N)-tokenises each "sentence", then
   `_hard_split_oversize` re-encodes. Fix already in place: short-
   circuit when `_split_at_sentences()` returns ≤1 entry, plus a
   `TIER_CHUNKER_DOC_TIMEOUT_SECONDS=600` wall-clock cap in worker.

## The extraction prompt shape (for reference)

Output is JSONL, one item per line, terminated by `{"t":"x"}`:

```
{"t":"e","cn":"<canonical_name>","sf":"<surface_form>","et":"<entity_type>","cf":0.0}
{"t":"r","sub":"<canonical_name>","pred":"<predicate>","obj":"<canonical|literal>","ok":"entity|literal","cf":0.0,"ev":"<evidence_phrase>"}
{"t":"f","sub":"<canonical_name>","ft":"<fact_type>","pn":"<property>","val":"<value>","cf":0.0,"ev":"<evidence_phrase>"}
{"t":"x"}
```

The system prompt + schema vocab + corpus schema_lens add ~1400 tokens
of scaffolding before the chunk text.

## Key code paths

- `backend/services/ghost_b.py` — main extraction service
  - `_SYSTEM` (line ~52) — strict JSONL system prompt
  - `build_user_prompt` (line ~876) — assembles the per-chunk prompt
  - `payload_base` block (line ~2659) — where `thinking` disable wires in
  - `extract_entities` — the main entry point
- `backend/services/ingestion/worker.py:1286` — `tier_chunker.chunk` call
  (wrapped in `asyncio.wait_for` with `TIER_CHUNKER_DOC_TIMEOUT_SECONDS`)
- `backend/services/ingestion/tier_chunker.py:248` — `_split_at_boundary`
  with the pathological-paragraph bailout
- `backend/services/ingestion/section_classifier.py` — chunk_kind
  decision; `_is_partial_index` catches mid-section indices

## Audit + observability

- `db.ghost_b_error_events` — per-attempt audit rows, capped at
  `EXTRACTION_ERROR_AUDIT_MAX_FAILED_ATTEMPTS_PER_DOC=200` failures and
  2 successes per doc
- `db.documents.{doc_id}.ghost_b_metrics` — authoritative per-doc tally
  (not capped)
- `db.documents.{doc_id}.write_state.warnings` — partial-extraction
  warnings ("Ghost B graph extraction partial: N/M chunks…")

## What NEVER to change without a re-ingest plan

- `EMBEDDING_DIMENSION` (1024) — Qdrant collections are dim-locked
- The embedding model identity (Qwen3-Embedding-0.6B family)
- `entity_id_from_name` derivation — entities are globally identified
  by canonical name slug, NOT scoped to corpus_id (intentional — same
  entity in multiple corpora = one node = natural bridges)
- `UNIVERSAL_ENTITY_SCHEMA` / `UNIVERSAL_RELATION_SCHEMA` vocab
- Chunking strategy (parent/child boundaries)

## Replay rig for testing extraction changes

`scripts/replay_ghost_b_chunks.py` replays a list of chunk_ids against
LiteLLM with full production scaffolding. Use to verify a model swap
or prompt change before committing.
