# GOTCHAS.md — Polymath RAG v3.3
# READ THIS BEFORE WRITING ANY CODE. No exceptions.
# Common traps, pitfalls, and non-obvious behaviors.

---

## 🔴 Critical (Will Break Things)

### 1. Never Hardcode Service URLs
All service URLs come from `backend/config.py` via `get_settings()`. Never write:
```python
client = QdrantClient("http://localhost:6333")   # WRONG
client = QdrantClient(url=settings.QDRANT_URL)    # RIGHT
```
Docker services use hostnames (`mongodb`, `qdrant`, `neo4j`, `litellm`, `ollama`), NOT `localhost`.

### 2. Never Hardcode Model Names
Model names are fetched at runtime:
- Ollama → `GET http://ollama:11434/api/tags`
- Cloud → `GET http://litellm:4000/models`
- Merged → `GET /api/models` (what frontend calls)

If you see a hardcoded model name outside `.env` or user-facing settings UI, remove it.

### 3. Embedding Dimension Lock
`EMBEDDING_DIMENSION=1024` (Qwen3-Embedding-0.6B). Changing this requires:
- Recreating ALL Qdrant collections
- Re-embedding ALL documents
- Qdrant collections created with wrong dim are permanently incompatible

### 4. All LLM Calls → LiteLLM
No direct provider SDK calls. Model names use `provider/model` format:
- `ollama/qwen2.5:0.5b`
- `openai/gpt-4o`
- `deepseek/deepseek-chat`

### 5. Embedder is Authoritative for Dimension
The embedder's `/info` endpoint is the source of truth for dimension. `config.py` `EMBEDDING_DIMENSION` must match. If they disagree, Qdrant writes will fail silently or corrupt vectors.

### 6. TEI is Rejected for Qwen3
Do NOT use `ghcr.io/huggingface/text-embeddings-inference` with Qwen3. The BPE tokenizer panics TEI. Use the custom sentence-transformers embedder instead.

---

## 🟡 Backend Patterns

### 7. Routers Are Thin
Validate input → call service → return response. No business logic in routers.
```python
# WRONG — business logic in router
@router.post("/corpora")
async def create_corpus(body: CorpusCreate):
    doc = {"corpus_id": str(uuid.uuid4()), ...}  # NO
    await db["corpora"].insert_one(doc)            # NO

# RIGHT — delegate to service
@router.post("/corpora")
async def create_corpus(body: CorpusCreate):
    doc = await ingestion_service.create_corpus(...)  # YES
```

### 8. Every Router → Registered in main.py
If you create a new router file, add `app.include_router(your_router)` in `main.py`. Unregistered routers return 404.

### 9. Every Import → Listed in requirements.txt
Python dependencies go in `backend/requirements.txt` before use. If `pip install X` works locally but the Docker build fails, the package is missing from requirements.txt.

### 10. Async Everywhere
Use async clients: `Motor` (MongoDB), `AsyncQdrantClient`, `AsyncDriver` (Neo4j), `httpx.AsyncClient`. Never use synchronous `pymongo.MongoClient` or `requests` in async endpoints.

### 11. API Keys Never Logged, Never Returned, Never Stored Unencrypted
If you see `logger.info(f"API key: {key}")` or `return {"api_key": ...}`, fix it immediately.

### 12. Neo4j is Optional
Every graph code path must guard:
```python
if settings.NEO4J_ENABLED:
    # Neo4j code here
else:
    skip / return empty
```

---

## 🟢 Frontend Patterns

### 13. All API Calls → `lib/api.ts`
Never use raw `fetch()` in components. Import from `api.ts`:
```typescript
import * as api from "../../lib/api";
const data = await api.listCorpora();  // RIGHT
const res = await fetch("/api/corpora");  // WRONG
```

### 14. Zustand for Global State
- `chatStore` — conversations, messages, streaming
- `settingsStore` — model prefs, RAG toggles, corpus selection (persisted)
- `authStore` — JWT token, user info (persisted)

No Context API for global state. No Redux.

### 15. Corpus Selection Uses settingsStore
`settingsStore.selectedCorpusIds` is the source of truth for query scoping in chat requests (`App.tsx` reads it). `chatStore.selectedCorpusIds` is used by CorpusManager UI only.

### 16. CorpusMultiSelect Uses settingsStore
The header dropdown (`CorpusMultiSelect.tsx`) uses `settingsStore.toggleCorpus` for selection. Do NOT switch it to chatStore — the selection must persist across page reloads.

### 17. No `any` Types
TypeScript code must be typed. If you're tempted to use `any`, use `unknown` and narrow with type guards instead.

### 18. Tailwind Utilities Only
No custom CSS classes unless absolutely necessary. Use Tailwind utility classes. The theme tokens are defined in `index.css` via `@theme`.

---

## 🟣 Docker & Infra

### 19. Docker Service Hostnames
These are the ONLY valid hostnames inside Docker:
```
mongodb://mongodb:27017/polymath
http://qdrant:6333
bolt://neo4j:7687
http://litellm:4000
http://ollama:11434
redis://redis:6379
http://embedder:80
http://reranker:8080
```
NEVER use `localhost` in Docker context.

### 20. Embedder Depends on GPU
The embedder container needs GPU access (`deploy.resources.reservations.devices`). If embedder is unhealthy, backend won't start (depends_on).

### 21. Reranker Model Path
Reranker weights live at `C:/Users/Sammb/Downloads/polymath_rag/models/ms-marco-MiniLM-L6-v2/`, NOT in `./download/`. Check `docker-compose.yml` volume mounts.

---

## 🔵 Corpus Management (Phase 7)

### 23. Corpus CRUD Lives in ingestion_service.py
Not a separate `corpus.py`. This is intentional per Refactoring Trigger rule (split at 300 lines / 4+ unrelated classes). Don't split prematurely.

### 24. DELETE Cascade Order
Corpus deletion follows this order:
1. Qdrant vectors (naive, hrag, graph collections)
2. Neo4j nodes (DETACH DELETE)
3. MongoDB chunks
4. MongoDB documents
5. MongoDB corpus record

Reversing this order could leave orphaned data.

### 25. delete_points_by_corpus Takes Short Keys
The function expects `"naive"`, `"hrag"`, `"graph"` — NOT full collection names like `"polymath_naive"`. Maps via `_col()` in `qdrant_writer.py`.

### 26. Single Doc Delete Not Yet Implemented
CorpusDetail has a delete button but it's a placeholder (reloads list). Need `DELETE /api/documents/{doc_id}` endpoint for full implementation.

### 27. update_write_state Type Hint
`**flags: Any` (not `**flags: bool`) because `update_corpus` may pass datetime for `updated_at`. Don't "fix" this back to `bool`.

---

## 🟠 Ingestion Pipeline

### 28. Write Order (Critical)
MongoDB documents → Qdrant vectors → update write_state flags. Never reverse this order. If Qdrant write fails, the document can be re-ingested (idempotent by doc_id).

### 29. parent_chunks Stored Inline
Parent chunks are stored as an inline array in the document record. If any document approaches the 16MB BSON limit, refactor to a dedicated `parent_chunks` collection.

### 30. Idempotent Re-ingest by chunk_id
Child chunks use `chunk_id = doc_id_XXXX` with deterministic MD5-derived UUIDs in Qdrant. Re-ingesting the same document produces the same chunk_ids → upsert replaces, never duplicates.

### 31. Corpus ID in Every Qdrant Payload
Every Qdrant point has `corpus_id` in its payload. This is defense-in-depth — even if per-corpus Qdrant collections are used, the payload filter prevents cross-corpus bleed.

---

## 📋 Handshake Protocol

### 32. Cross-Boundary Changes Require Agreement
Before writing code that crosses backend ↔ frontend boundaries (new endpoint + its api.ts counterpart), state your plan and wait for confirmation. See CLAUDE.md §Handshake Protocol.

### 33. New API Endpoint + Frontend Call = Two Changes
Every new endpoint needs:
1. Backend: router endpoint + service function
2. Frontend: `api.ts` function + TypeScript type
Don't forget the frontend side.

---

## 🧪 Testing

### 34. Check GOTCHAS.md Before Writing Code
This file exists for a reason. If you skip it and break something, the fix is on you.

### 35. Check graphify_2/GRAPH_REPORT.md for Context
The graph report shows 458 nodes, 872 edges, 58 communities. Use it to understand relationships between components before making changes.

---

### 36. SettingsModal Stub Conflict
SettingsModal.tsx may contain local stub functions (e.g., `function InfrastructureTab()`) that conflict with imported external components of the same name. When creating new tab components as separate files, check for existing local declarations and remove them before adding imports. Error: "Import declaration conflicts with local declaration."

### 37. DEFAULT_INGESTION_CONFIG Must Be Complete
The `IngestionConfig` model has 18+ fields with defaults. A local `DEFAULT_INGESTION_CONFIG` constant with only 5 fields will cause TypeScript errors when expanded form components reference `parent_chunk_tokens.min_tokens` etc. Always import the complete version from `types/corpus.ts`.

### 38. Chunking Lives in tier_chunker.py (Not chunker.py)
The ingestion pipeline uses `backend/services/ingestion/tier_chunker.py` for ALL chunking — NOT `services/chunker.py`. The `tier_chunker` implements Plan_V3_2 tier-specific strategies: heading-bound (Tier A/B+), paragraph-grouped (Tier C), page-based (OCR AST). It also uses `b_plus_normalizer` for synthetic header injection on Tier B+. Do not create a parallel chunker — extend `tier_chunker.py` if needed.

### 39. SSE Pipeline Stage Inference
The ingestion SSE endpoint (`GET /api/ingestion/jobs/{doc_id}/stream`) infers pipeline stages from `write_state` flags (mongo_written, qdrant_written, neo4j_written) rather than explicit stage tracking. Stages: ingesting → embedding → graph_extracting → finalized.

---

## 🟤 Phase 14 — Schema-Conditioned Extraction (Ontology-Lite)

### 40. `schema_strict` is a 3-Way Enum, Not a Boolean
`IngestionConfig.schema_strict` is `Literal["off", "soft", "hard"]` (default `"soft"`).

- `"off"` — schema is a hint; LLM may emit anything (mirrors pre-14.1 behavior).
- `"soft"` (default) — out-of-schema entity_types remap to `"other"`; out-of-schema predicates remap to `"related_to"`. Edge / node preserved.
- `"hard"` — out-of-schema entities and relations are dropped entirely. For precision-critical corpora.

If you treat it as a boolean (`if schema_strict:`) it'll be truthy for ALL three values including `"off"`. Always compare to literal strings.

### 41. Phase 14.3 entity_id Format Change — Wipe Neo4j on Upgrade
The entity_id format changed from UUID5 (e.g. `6ba7b810-9dad-11d1-80b4-00c04fd430c8`) to type-discriminated string `"{type_slug}:{name_slug}"` (e.g. `"organization:apple-inc"`). NO migration script ships — this was decided when Neo4j was empty (2026-04-18 handshake).

**If old UUID5 entities exist in Neo4j when you boot v14_3 code:** new ingests will create separate type-discriminated nodes; old UUID5 nodes become orphaned (still queryable, but never updated again). Wipe Neo4j first:
```cypher
MATCH (n) DETACH DELETE n
```
Then re-ingest all corpora.

### 42. SCHEMA_INLINE_LIMIT Triggers Per-Chunk Retrieval
`config.SCHEMA_INLINE_LIMIT=30` is the threshold. When `entity_schema` or `relation_schema` length is:
- **≤ 30 terms** → full sentinel-augmented vocab is inlined into every ghost_b prompt (no Qdrant lookup).
- **> 30 terms** → ghost_b calls `retrieve_schema_for_chunk()` on `polymath_schemas` per chunk; only top-K (`SCHEMA_RETRIEVAL_TOP_K=10`) terms + sentinel are injected.

**Resume corner case:** when Leg 2 (Qdrant) is already done and only Leg 3 (Neo4j) re-runs, `vec_map` is empty → degraded fallback inlines the first `inline_limit` terms (logged at WARNING). To avoid degraded extraction on resume, wipe `write_state.qdrant_written=false` to force re-embed.

### 43. Corpus Delete Cascade Now Includes `polymath_schemas`
GOTCHAS #24 cascade order is extended:
1. Qdrant chunk collections (naive, hrag, graph) — `delete_points_by_corpus`
2. **Qdrant schema collection** — `delete_schema_terms(corpus_id, kind=None)` *(NEW in 14.2)*
3. Neo4j nodes (DETACH DELETE)
4. MongoDB chunks
5. MongoDB documents
6. MongoDB corpus record

Step 2 must run before the Mongo deletes so a Qdrant failure aborts the cascade with the corpus still intact and recoverable.

### 44. Sentinels Are Reserved — User Cannot Remove Them
`SchemaContext.ENTITY_SENTINEL = "other"` and `SchemaContext.RELATION_SENTINEL = "related_to"` are class-level constants. The `entity_vocab` / `relation_vocab` properties auto-append them after deduplicating against the user's list.

If a user puts `"other"` in `entity_schema`, dedup keeps it (no duplicate "other" in the prompt). If they OMIT it, the property still appends it. The LLM ALWAYS sees the sentinel as a fallback option in the prompt's vocabulary constraint block.

Never special-case sentinel values in downstream code — let them flow through as ordinary entity_types / predicates. The Neo4j node for `"organization:other"` is fine; the relation `predicate="related_to"` is fine.

### 45. Schema Update Is Diff-Based — Don't Re-Embed Identically
`IngestionService.update_corpus` compares `entity_schema` / `relation_schema` to the existing config and only re-embeds when changed. If you bypass that path (e.g. `mongo_writer.update_corpus` directly), the Qdrant `polymath_schemas` collection drifts out of sync with MongoDB and ghost_b retrieves stale terms. Always go through `IngestionService.update_corpus`.

---

## 🔴 Phase 19.3 — Per-Corpus Ghost Model Profiles

### 46. Per-Corpus GHOST Overrides Land in `IngestionConfig`, NOT Global Settings
As of Phase 19.3, `IngestionConfig` gained six per-ghost fields:

```
summary_base_url, summary_api_key, summary_extra_params
extraction_base_url, extraction_api_key, extraction_extra_params
```

Plus `models_linked: bool` (UI-only flag; worker ignores it).

- **API keys are Fernet-encrypted at rest** — stored as ciphertext in `corpora.default_ingestion_config` and each document's snapshot. Worker calls `services.secrets.decrypt()` before injecting into LiteLLM.
- **`None` / empty string** means "fall back to env" at every level (model, api_base, api_key, extra_params).
- **`extra_params` is merge-style** — keys `model`, `messages`, and (in ghost_b) `response_format` are reserved and never overwritten by extra_params.

### 47. Dead Config Fixed — `summary_model` / `extraction_model` + Concurrency Caps Now Live
Before Phase 19.3, `IngestionConfig.summary_model`, `extraction_model`, `summary_max_concurrent`, and `extraction_max_concurrent` were declared but **never read** — worker passed a single top-level `model` to both ghosts and both ghosts hardcoded `settings.{SUMMARY,EXTRACTION}_MAX_CONCURRENT`.

Now:
- `worker.py` resolves `ingestion_config.{summary,extraction}_model or <top-level model arg>` per ghost.
- `ghost_a.py` / `ghost_b.py` accept `max_concurrent` kwarg → `asyncio.Semaphore(max_concurrent or settings.X_MAX_CONCURRENT)`.

If you see old config edited pre-19.3 with these fields — they were no-ops. After upgrade, they take effect on the NEXT ingest. Existing documents' extraction is not retroactively re-run.

### 48. Schema Changes Do Not Backfill
Setting `entity_schema` / `relation_schema` / `schema_strict` on a corpus only affects **future** ingests. Old documents keep their original extraction. To re-run a document against the new pool, wipe `write_state.neo4j_written = false` on that document and re-ingest. The Corpus Manager UI tooltips warn about this.

### 49. `profile:<id>` Is a Reserved Model Prefix
As of Phase 19.3, `chat_orchestrator` treats any `model` string starting with `profile:` as a reference to the `model_profiles` MongoDB collection. Do NOT register an Ollama or LiteLLM model whose prefix is literally `profile` — it will be hijacked by profile resolution and 404 when the lookup fails.

The resolution flow:
1. `chat_orchestrator.process_chat_request(request, user_id=...)` detects the prefix.
2. Calls `model_profiles_service.get_resolved(user_id, profile_id)` → plaintext creds or `None`.
3. If found: substitutes `model = f"openai/{profile.model_name}"` and spreads `api_base` + `api_key` + `extra_params` as kwargs into `llm_service.stream_chat()`.
4. If missing: logs warning + falls back to `settings.DEFAULT_COMPLETION_MODEL`. User's request does NOT fail silently — the fallback model is used and the warning is visible in backend logs.

### 50. LiteLLM Per-Request `api_base` / `api_key` Overrides
`llm_service.stream_chat()` gained three kwargs in Phase 19.3: `api_base`, `api_key`, `extra_params`. When present they're injected into the LiteLLM request body — LiteLLM honors per-request `api_base` / `api_key` as overrides on the matched wildcard route. This is why custom profiles work against arbitrary URLs without editing `litellm/config.yaml`.

Reserved keys that `extra_params` CANNOT clobber in a chat call: `model`, `messages`, `stream`, `tools`. In ghost_b extraction, `response_format` is also reserved.

### 51. `IngestionConfig` API Keys Are Masked On GET As `"[set]"`
As of Phase 19.3 Phase-C, `ingestion_service.list_corpora()` and `get_corpus()` replace the stored Fernet ciphertext for `summary_api_key` / `extraction_api_key` with the literal string `"[set]"` (or `None`) before returning. The frontend never sees ciphertext or plaintext.

**On round-trip write**: `_encrypt_ingestion_keys_in_place()` recognizes `"[set]"` as a "preserve-existing" sentinel alongside empty/None — it does NOT treat the sentinel as plaintext to encrypt. If you bypass this helper and write to the Mongo collection directly, you'll clobber the real ciphertext with `encrypt("[set]")`. Always go through `IngestionService.create_corpus` / `IngestionService.update_corpus`.

### 52. Corpus Manager Uses Font Scale 9 / 11 / 12
After the Phase 19.3 Phase-C font bump, the size ladder is:
- `text-[9px]` — micro labels (corpus_id excerpts, stats counts)
- `text-[11px]` — secondary content (descriptions, field labels)
- `text-[12px]` — primary content (names, inputs, form fields)

`text-[10px]` is no longer used anywhere in `CorpusManager.tsx` or `IngestionModelCard.tsx`. Future additions should pick from the 9/11/12 ladder to stay visually consistent.

### 53. `models_linked` Is UI-Only
`IngestionConfig.models_linked` controls whether the Corpus Manager renders a single shared card or two split cards. It has **zero runtime effect** — the worker always reads `summary_*` and `extraction_*` fields independently. When linked in the UI, both sets hold identical values because the shared card's onChange writes to both in parallel.

Concurrency caps (`summary_max_concurrent` / `extraction_max_concurrent`) **always split** regardless of `models_linked`, because summary calls are slow (long output) and extraction calls are fast (short JSON) — a shared semaphore would throttle extraction to summary's cadence. See `IngestionModelsSection` in `CorpusManager.tsx`.

*Last updated: 2026-04-18 — Phase 19.3 Corpus Manager UX session*

---

## 🟢 Phase 7.5 — Per-Corpus Qdrant Collections

### 54. Per-Corpus Collection Naming Is Resolved by `_col_for_corpus`
Every corpus owns 4 Qdrant collections, named `{QDRANT_COLLECTION_PREFIX}{corpus_id[:8]}_{kind}` where kind ∈ {`naive`, `hrag`, `graph`, `schemas`}. The single source of truth is `services.storage.qdrant_writer._col_for_corpus(corpus_id, kind)`. Do NOT format the name yourself in callers — always go through the helper so the prefix and slicing rules can change in one place.

The legacy `_col(key)` helper is retained for the migration script ONLY (reads from `polymath_naive/hrag/graph/schemas`). Hot-path writers and readers must NOT use it.

**Why 8 chars:** UUID4's first 8 hex chars give ~2^32 distinct values — collision odds are ~1 in 4 billion per pair. The first-write code calls `_assert_collection_owner` which scrolls one point and verifies `payload.corpus_id` matches the expected owner; on mismatch it raises `RuntimeError` so a colliding ingest aborts BEFORE polluting another corpus's vectors.

### 55. Corpus Lifecycle Owns Collection Lifecycle
- `IngestionService.create_corpus` calls `ensure_collections_for_corpus(qdrant, corpus_id, dim)` after the Mongo insert. If Qdrant fails, the corpus creation raises (we don't want a corpus that can't be ingested into).
- `IngestionService.delete_corpus` calls `drop_collections_for_corpus(qdrant, corpus_id)` as cascade step 1 — this is O(1) per collection and atomic, replacing the old filter-delete on shared collections.

The boot-time `ensure_collections()` call in `IngestionService.connect` was REMOVED. The global polymath_* collections are no longer auto-created on container start.

### 56. Migration `001_per_corpus_qdrant.py` Is Idempotent
Point IDs are deterministic MD5-derived UUIDs (`_child_point_id` / `_summary_point_id` / `_schema_point_id`). Re-running the migration upserts the same IDs (no duplicates), and the source-side delete-by-ID becomes a no-op for already-migrated points.

The migration HALTS on any per-corpus failure (does NOT drop the legacy globals). The system stays runnable on the old data; a fixed re-run picks up where the failure happened. Globals are only dropped after `_verify_globals_empty` confirms 0 remaining points across all 4 legacy collections.

### 57. Retriever Multi-Corpus Now Scopes Per-Corpus
`RetrieverOrchestrator._resolve_collections(tier, corpus_ids, collections)` (note the new `corpus_ids` arg) expands to per-corpus collection names. The multi-corpus loop scopes each `funnel_b.search(...)` call to its own corpus's collections — passing all b_cols to every per-corpus call would still be correct (the payload filter on `corpus_id` would zero out the cross-corpus hits) but wastes Qdrant work.

The corpus_id payload filter is RETAINED in every search call as defense-in-depth, even though per-corpus collections already isolate data physically.

---

## ⚪ Phase 7.6 — Docling Sidecar Replaces format_router + source_classifier

### 60. Docling Is the Parser
The ingestion pipeline no longer uses `format_router` (pypdf / BeautifulSoup / unstructured) or the regex `source_classifier`. Both are replaced by a CPU-only **docling sidecar** at `docling_svc/` (image: `polymath_v33-docling`, internal hostname `http://docling:8500`). The backend talks to it through `services/ingestion/docling_adapter.py` — that adapter is the ONLY caller; nothing else in the codebase imports from `docling` or `docling-core`.

**Why a sidecar instead of in-backend:** docling pulls torch + torchvision + accelerate (~2 GB) and downloads layout/OCR model weights on first run (~1.5 GB more). It also requires `httpx>=0.28` which would force a major bump for the 45 backend httpx call sites. Sidecar keeps backend image small and isolates ML model lifecycle. Mirrors the embedder/reranker pattern.

**Pipeline flow now:**
1. `worker.py` calls `docling_adapter.parse_document(bytes, filename, mime, do_ocr=...)`.
2. The adapter pre-augments **plain-text uploads** with `inject_synthetic_headers` BEFORE handing bytes to docling — this is how `Onboarding.txt` / `Product Overview.txt` keep classifying as `tier_b_plus` (docling itself treats raw .txt as unstructured).
3. Docling returns markdown + per-section walk + per-page (PDF only) + structure stats.
4. `docling_adapter._classify_tier(...)` maps to `SourceTier` (HTML→tier_b, multi-page PDF→ocr_ast, augmented .txt with structure→tier_b_plus, has_structure→tier_a, else tier_c).
5. `tier_chunker.chunk(parse_result, doc_id, corpus_id)` consumes the section walk to build parents — no more re-parsing.

### 61. `inject_synthetic_headers` Is Pre-Parse, Not Post-Parse
`b_plus_normalizer._likely_structured` and `looks_like_b_plus` are GONE — classification is docling's job now. What survives: `_PATTERNS`, `InjectedHeader`, and `inject_synthetic_headers`. The injector runs INSIDE the adapter, BEFORE the bytes reach docling — so docling sees real `#`/`##` markers and produces proper `section_header` items. The audit list rides through the response into the document record (same shape as before).

### 62. `IngestionConfig.docling_ocr_enabled` Default True
New per-corpus field controls the docling sidecar's OCR pass on PDFs / images. Default True. Set False for text-only PDFs to cut ingest latency by 50-60%. Other formats ignore the flag — docling's PdfFormatOption is the only place it's wired.

### 63. Model Weights Persist via Named Volumes
`docling_models` and `docling_hf_cache` are named docker volumes mounted into the sidecar (`/app/docling_models` and `/app/hf_cache`). Without these, every `docker compose down` would force a 1.5 GB re-download on next boot. Pre-fetched at image build time via the Dockerfile's `download_models()` step — failure is tolerated and runtime fetch retries.

### 64. Sidecar Auth Surface
The docling sidecar exposes port 8500 on the **host** (`ports: - "8500:8500"`) for debugging. If you don't want it externally reachable, switch `ports:` to `expose:` in `docker-compose.yml` — backend reaches it on the docker network either way via `http://docling:8500`. There is no auth on `/parse` itself; rely on network isolation.