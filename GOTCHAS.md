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
Reranker weights live under `${POLYMATH_MODELS_ROOT}/ms-marco-MiniLM-L6-v2/`, NOT in `./download/`. Check `docker-compose.yml` volume mounts.

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

### 28. Write Order (Critical) — *superseded by §67*
*Historical note (pre-Phase-2-refactor):* MongoDB documents → Qdrant vectors → update write_state flags. The modern locked order is **parse → chunk → [ghost_a ∥ ghost_b] → mongo → embed → qdrant → neo4j**, see §67 for the authoritative contract. Idempotency by content-hashed `doc_id` still applies.

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

**Pipeline-order note (post-Phase-2-refactor):** under the new locked order (§67) ghost_b runs BEFORE the embed phase, so `chunk_vectors=None` is the norm on **every fresh ingest** — not just on a resume. `resolve_chunk_vocab` falls back to the first `inline_limit` terms + sentinel whenever `chunk_vectors` is None. With the universal schema (29 terms < 30 inline limit, see §66), the full vocab is inlined and the Qdrant retrieval path is never exercised in practice. The retrieval path is retained for future per-corpus overrides above the 30-term threshold.

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
- `worker.py` resolves the pool entries at dispatch time (each `ModelProfileRef` on `summary_models` / `extraction_models` carries its own `model`, `base_url`, `api_key`, `extra_params`, and `max_concurrent`).
- `ghost_a.py` / `ghost_b.py` accept `pool: list[dict]` and build **one `asyncio.Semaphore(entry.max_concurrent)` per pool entry** (see `ghost_a.summarize_parents:106` / `ghost_b.extract_entities:491`). There is no top-level `max_concurrent` kwarg — the pre-19.3 single-scalar API was removed. Overall throughput = sum of per-entry `max_concurrent`.

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
The ingestion pipeline no longer uses `format_router` (pypdf / BeautifulSoup / unstructured) or the regex `source_classifier`. Both are replaced by a GPU-capable **docling sidecar** at `docling_svc/` (image: `polymath_v33-docling`, internal hostname `http://docling:8500`). The backend talks to it through `services/ingestion/docling_adapter.py` — that adapter is the ONLY caller; nothing else in the codebase imports from `docling` or `docling-core`.

**Why a sidecar instead of in-backend:** docling pulls torch / transformers dependencies and layout artifacts. It also requires `httpx>=0.28` which would force a major bump for the 45 backend httpx call sites. Sidecar keeps backend image small and isolates ML model lifecycle. Mirrors the embedder/reranker pattern. GPU layout parsing is allowed; OCR is disabled by policy.

**Pipeline flow now:**
1. `worker.py` calls `docling_adapter.parse_document(bytes, filename, mime, do_ocr=...)`.
2. The adapter pre-augments **plain-text uploads** with `inject_synthetic_headers` BEFORE handing bytes to docling — this is how `Onboarding.txt` / `Product Overview.txt` keep classifying as `tier_b_plus` (docling itself treats raw .txt as unstructured).
3. Docling returns markdown + per-section walk + per-page (PDF only) + structure stats.
4. `docling_adapter._classify_tier(...)` maps to `SourceTier` (HTML→tier_b, multi-page PDF→ocr_ast, augmented .txt with structure→tier_b_plus, has_structure→tier_a, else tier_c).
5. `tier_chunker.chunk(parse_result, doc_id, corpus_id)` consumes the section walk to build parents — no more re-parsing.

### 61. `inject_synthetic_headers` Is Pre-Parse, Not Post-Parse
`b_plus_normalizer._likely_structured` and `looks_like_b_plus` are GONE — classification is docling's job now. What survives: `_PATTERNS`, `InjectedHeader`, and `inject_synthetic_headers`. The injector runs INSIDE the adapter, BEFORE the bytes reach docling — so docling sees real `#`/`##` markers and produces proper `section_header` items. The audit list rides through the response into the document record (same shape as before).

### 62. OCR Is Disabled
`IngestionConfig.docling_ocr_enabled` is retained only for legacy row compatibility and now defaults to `False`. The worker and preflight paths pass `do_ocr=False` unconditionally, the adapter ignores `do_ocr=True`, and the docling sidecar forces `PdfPipelineOptions.do_ocr=False`. Scanned PDFs return whatever text the fast local PDF path can extract; they must not silently launch OCR. Docling may still use GPU for non-OCR layout parsing.

### 63. Model Weights Persist via Host Bind Mounts
Docling artifacts and HuggingFace caches are bind-mounted to host storage under `E:/PolymathRuntime` by default (`/app/docling_models` and `/app/hf_cache`). The Dockerfile must not pre-fetch model weights into image layers. A Docker Desktop VHD wipe should not erase host-mounted Polymath data/caches, and a rebuild should not bake multi-GB model weights into BuildKit cache.

Do **not** set `POLYMATH_DOCKER_DATA_ROOT` inside Docker Desktop's own `DataFolder`. If Docker Desktop stores its VHD at `E:/Docker`, then app data belongs somewhere like `E:/PolymathRuntime`; using `E:/Docker/volumes` for bind mounts can make containers see a tiny Docker-managed device and fail with misleading `No space left on device` errors.

### 64. Sidecar Auth Surface (Internal-Only Now)
The docling sidecar uses `expose: - "8500"` (NOT `ports:`) — it is reachable only on the docker network at `http://docling:8500`. There is no auth on `/parse` itself; isolation comes from the network boundary. Don't switch back to `ports:` without thinking about who can hit `/parse` from the host. Also: the sidecar enforces a 150 MB upload cap (HTTP 413) and the backend adapter uses a 600s read timeout — both env-overridable but tuned for typical PDF/DOCX sizes.

### 65. GPU Policy — Docling GPU Layout, No OCR, Conservative Embedder
Docling is GPU-enabled for layout parsing and OCR-disabled. Compose pins it to `CUDA_VISIBLE_DEVICES="0"` by default and sets `DOCLING_IDLE_UNLOAD_SECONDS=300`, so the converter is released after five idle minutes and `torch.cuda.empty_cache()` is called.

The embedder also maps to the same visible GPU by default. Keep `LOCAL_EMBED_BATCH_SIZE` conservative (`8` by default) so large ingests do not make the desktop unusable. Raise it only while watching `nvidia-smi`.

**The pin lives at the torch layer, not the docker layer.** On Docker Desktop for Windows, both `device_ids` and `NVIDIA_VISIBLE_DEVICES` can be silently ignored. The load-bearing pin is `CUDA_VISIBLE_DEVICES` on the service:

```yaml
docling:
  environment:
    CUDA_DEVICE_ORDER: PCI_BUS_ID
    CUDA_VISIBLE_DEVICES: "0"
    DOCLING_OCR_ENABLED: "false"
    DOCLING_IDLE_UNLOAD_SECONDS: ${DOCLING_IDLE_UNLOAD_SECONDS:-300}
embedder:
  environment:
    CUDA_DEVICE_ORDER: PCI_BUS_ID
    CUDA_VISIBLE_DEVICES: "0"
    EMBED_BATCH_SIZE: ${LOCAL_EMBED_BATCH_SIZE:-8}
```

`CUDA_VISIBLE_DEVICES` filters at torch import time; this is the one that actually works. Keep a visible `nvidia-smi -l 1` during first large ingest after a rebuild.

**Do NOT add OCR back to docling.** If OCR is ever revived, it should be an explicit separate service/profile, not the default parser path.

Verify after any compose edit:

```bash
docker exec polymath_v33-docling-1 curl -s http://localhost:8500/health
docker exec polymath_v33-embedder-1 python -c "import torch; print(torch.cuda.get_device_name(0))"
```

`torch.cuda.device_count()` should be **1** in both GPU containers, but `/health` on docling must report `ocr_available: false`. After `DOCLING_IDLE_UNLOAD_SECONDS`, docling `/health` should show `converter_loaded: false`.

### 65.1 TODO — Embedding VRAM Guardrail
Partial guard is live: compose defaults `LOCAL_EMBED_BATCH_SIZE` to `8`, and the embedder serializes local `model.encode(...)` calls inside one process while exposing `gpu_free_mb` / `gpu_total_mb` on `/health` and `/info`.

Before 500-file ingestion, finish the adaptive guard:

- enforce a low local batch cap when free VRAM drops below a configured threshold
- prefer API/cloud embedding for large batch ingest when local VRAM is tight
- surface a UI warning before ingest when local embedding would leave less than ~2 GB free VRAM
- add backend backpressure so many files cannot enqueue enough embed work to starve the machine

---

## 🟪 Universal Extraction Schema

### 66. Universal schema is baked into `ghost_b.UNIVERSAL_*_SCHEMA`
GHOST B now runs against a fixed vocabulary: **12 entity types** (Person, Organization, Location, Event, Concept, Method, Product, Document, Rule, Law, Artifact, TimeReference) and **17 relation predicates** (part_of, member_of, located_in, works_for, created_by, uses, references, implements, depends_on, produces, preceded_by, causes, derived_from, contradicts, excepts, overrides, related_to — sentinel must stay last).

`IngestionConfig.entity_schema` / `relation_schema` / `schema_strict` still exist as fields for future per-corpus overrides, but the Corpus Manager UI no longer exposes them. `schema_strict` is narrowed to `Literal["soft"]` — legacy `"off"` / `"hard"` values are coerced to `"soft"` by the pre-validator and permanently rewritten to `"soft"` by the lifespan migration.

**To change the schema:** edit the two constants in `backend/services/ghost_b.py`, then re-ingest affected corpora (wipe `write_state.neo4j_written=false` on each document to force re-extraction — existing entities keep their old types until re-extracted).

**Size rule:** the combined vocab (entities + relations) MUST stay under `SCHEMA_INLINE_LIMIT` (30). Currently 12 + 17 = 29, leaving one slot of headroom. Crossing 30 flips ghost_b into per-chunk Qdrant retrieval mode (GOTCHA #42); under the Phase-20 locked pipeline order that means degraded fallback on fresh ingest because embeddings don't exist yet when ghost_b runs.

**Admin lever — `FORCE_UNIVERSAL_SCHEMA`:** env-driven bool in `config.py`. Default `False` preserves corpora that explicitly customized their schema; `True` overwrites every corpus with the universal vocab on startup. Migration runs unconditionally in lifespan; `force` only changes whether custom schemas survive. The lifespan log records every patched `corpus_id` with the reason (`null_entity_schema`, `legacy_strict=off`, `force`, etc.) so there's a paper trail when someone asks "why did my corpus's schema change?".

**Mixed-vocab post-migration audit.** The migration rewrites `default_ingestion_config` on each corpus, but existing Neo4j entities keep the `entity_type` values the original extraction assigned. A query filtering on `entity_type="Rule"` misses pre-migration entities that were tagged with older/lowercase/custom types. Audit with:
```cypher
MATCH (n:Entity) RETURN DISTINCT n.entity_type ORDER BY n.entity_type
```
Anything outside the 12-type universal list is pre-migration data — wipe `write_state.neo4j_written=false` on the affected documents and re-ingest to relabel. The `entity_id` slugify is lowercase-first (`neo4j_writer._slugify_type`), so `"Organization"` and `"organization"` collapse to the same `organization:...` node — no duplicate-entity risk from the TitleCase switch, only stale `entity_type` labels on pre-migration rows.

---

## ⚫ Phase 20 — Worker Refactor (Locked Pipeline)

### 67. Locked pipeline order (authoritative)
`backend/services/ingestion/worker.py` runs every ingest through seven phases, in this order, no exceptions:

```
parse → chunk → [ghost_a ∥ ghost_b] → mongo → embed → qdrant → neo4j
```

- **parse** — `docling_adapter.parse_document`
- **chunk** — `tier_chunker.chunk` → `(parents, children, injected_headers)`
- **ghosts_parallel** — `asyncio.gather(_a_branch(), _b_branch())` — either branch is a no-op when its feature flag is off
- **mongo** — ONE `_write_mongo_all` pass (documents + chunks), summaries AND `ghost_b_staging` inline; flips `mongo_written` once
- **embed** — single `embed_batch` over `[*child_texts, *summary_texts]`
- **qdrant** — children → naive / hrag (tier-filtered) / graph; summaries → naive + hrag only; flips `qdrant_written`
- **neo4j** — `write_document_graph(extraction_results)`; flips `neo4j_written`; skipped entirely when `use_neo4j=False`

Every phase emits a structured log line: `phase=<name> duration=<s> doc=<id12> corpus=<cid8> …`. Supersedes §28's historical write-order.

### 68. Ghost failure policy — hard abort
`summarize_parents` / `extract_entities` filter per-task exceptions and return whatever succeeded. The worker compares `len(results) < len(tasks)` and raises `GhostAFailure` / `GhostBFailure`; `asyncio.gather` cancels the sibling branch. No partial writes are flushed — `mongo_written`, `qdrant_written`, `neo4j_written` stay at their pre-job values. Retry picks up from the same phase next run.

### 69. Ghost A skip-on-retry — Mongo read-back
When `ws.mongo_written=True` and every `parent_chunks[].summary` is non-empty, `_run_ghosts_parallel` reconstructs the `SummaryResult` list from the stored strings via `_reconstruct_summaries_from_mongo` and skips the LLM call. Partial coverage (any parent with a blank summary) falls back to re-running Ghost A. No separate "summaries done" flag — the inline field is authoritative.

### 70. Ghost B skip-on-retry — `ghost_b_staging`
`_write_mongo_all` serializes Ghost B output as `documents.ghost_b_staging` (list of `dataclasses.asdict(ExtractionResult)`) in the same atomic write that flips `mongo_written`. On resume the worker calls `mongo_reader.read_ghost_b_staging(db, doc_id, corpus_id)` — if the list is present, `_rehydrate_ghost_b_staging` rebuilds the `ExtractionResult` dataclasses (nested `EntityItem` / `RelationItem` need manual construction, not `**r`) and feeds them straight into `write_document_graph`. The former `_neo4j_has_mentions` probe is gone. Staging is **left as provenance after `neo4j_written` flips**, never cleared.

Three log reasons distinguish the paths — grep for `phase=ghost_b_`:

- `phase=ghost_b_skip reason=staging_found doc=… entries=N` — resume hit staging, LLM not called
- `phase=ghost_b_run reason=fresh_ingest doc=…` — first ingest
- `phase=ghost_b_run reason=staging_missing_legacy_doc doc=…` — `qdrant_written=True` but no staging field (pre-feature document); Ghost B re-runs

### 71. Preset modes — Fast / Balanced / Deep / Custom
`IngestionConfig.preset: Literal["fast","balanced","deep","custom"] = "balanced"`. Backend normalization in `services.ingestion_service.apply_preset()` overwrites `use_neo4j` + `chunk_summarization` + `target_qdrant_collections` to match the preset on create/update. `"custom"` returns config unchanged — toggles flow through verbatim.

| preset | use_neo4j | chunk_summarization | target_qdrant_collections |
|---|---|---|---|
| fast | False | False | naive, hrag |
| balanced | True | False | naive, hrag, graph |
| deep | True | True | naive, hrag, graph |
| custom | — caller's values — | — | — |

Frontend (`CorpusManager.tsx → PresetModeSelector`) renders a 4-way radio group; Custom reveals the two raw checkboxes. Open-time `inferPreset` compares stored preset against toggles and falls through to Custom when they disagree — preserves user intent over the Pydantic `"balanced"` default on legacy rows.

### 72. Mixed-vocab Neo4j audit (post-universal-schema)
Same Cypher as §66's audit block — flagging here for discoverability. After the universal schema lands, run:
```cypher
MATCH (n:Entity) RETURN DISTINCT n.entity_type ORDER BY n.entity_type
```
Anything outside the 12 Title-Case types is pre-migration data; wipe `write_state.neo4j_written=false` on the affected documents to force re-extraction.

### 73. Deprecated: `format_router.py` and `source_classifier.py`
Both modules were replaced by the docling sidecar + `docling_adapter` in Phase 7.6 (§60). They remain on disk with **no live importers** — `grep -rn "format_router\|source_classifier\|DecodeResult" backend/` returns only the dead file itself, a comment in `docling_adapter.py`, and a header comment in `worker.py`. Scheduled for removal in a future cleanup sweep. Do not add new callers.

---

## 🟫 Ghost B Extraction — Operator Nuances

### 74. DeepSeek v4-flash defaults to thinking-mode ON — Ghost B disables it
DeepSeek v4-flash (and v4-pro) ship with `thinking` enabled by default. Every output token goes to `reasoning_content` until the chain-of-thought finishes. With `EXTRACTION_MAX_TOKENS=1024`, reasoning consumes the entire budget and `message.content` stays empty — extraction reports 99%+ failure rate even though `finish_reason=length` looks like a token-cap problem.

[`backend/services/ghost_b.py`](backend/services/ghost_b.py) sends `"thinking": {"type": "disabled"}` for any model whose name starts with `deepseek/`. Operators can override per-corpus via `extra_params` on the model entry if they actually want reasoning. If you swap to another reasoning provider (Claude extended thinking, OpenAI o-series, Qwen QwQ, etc.), wire its own disable param the same way — generic `reasoning_effort: "low"` is silently ignored by DeepSeek, and `thinking_mode: "disabled"` is the wrong key. Only `thinking: {"type": "disabled"}` works for DeepSeek.

### 75. `EXTRACTION_MAX_TOTAL_LINES` must sit above the per-type theoretical max
Per-type caps are `14 entities + 20 relations + 5 facts + 1 sentinel = 40 lines max possible`. The parser line-cap was historically `20`, well below that ceiling, so dense chunks emitted 21–30 valid JSONL lines, the parser truncated the tail, and the audit logged `error_type=line_cap_exceeded` even though extraction succeeded. Default is now `55` — the prompt no longer carries an explicit line-cap rule, so the parser is the only gate. Don't drop this below ~45 unless you've also reduced the per-type caps. The rescue profile (`EXTRACTION_RESCUE_MAX_TOTAL_LINES`, default `16`) has the same below-per-type-max issue on smaller scale (rescue caps 8/8/5 + sentinel = 22), worth bumping to ~30 if you see frequent rescue line_cap_exceeded events.

### 76. The failure-budget circuit breaker protects you from runaway provider spend
[`backend/services/ghost_b.py`](backend/services/ghost_b.py) opens a per-document failure circuit when `(failed/processed) × 100 ≥ EXTRACTION_FAILURE_PAUSE_PERCENT` (default `25.0`) after at least `EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS` chunks (default `20`) have been processed. Remaining queued chunks are marked `not_processed` — no more API calls fire for that doc. The doc still proceeds to Mongo + Qdrant write; only the graph extraction is skipped for the unprocessed chunks. A `ghost_b_failure_budget_tripped` audit event records the trip. **This is what saves you from burning $5 of tokens on a doc whose first 20 chunks all fail** — leave the threshold at 25% unless you have a reason.

### 77. Audit log is sampled per-doc, not exhaustive
`ghost_b_error_events` caps at `EXTRACTION_ERROR_AUDIT_MAX_FAILED_ATTEMPTS_PER_DOC=25` failed events plus `EXTRACTION_ERROR_AUDIT_MAX_SUCCESS_ATTEMPTS_PER_DOC=2` success events per document, plus all budget-trip events. So if you see exactly 25 failures in the audit, the actual failure count is **at least** 25 — possibly more. The authoritative per-doc tally lives on `documents.ghost_b_metrics` (`requested_chunks`, `extracted_chunks`, `failed_chunks`, `success_rate`); use that for hard counts. Audit rows are for forensic classification, not for arithmetic.

### 78. `error_type=line_cap_exceeded` is not a true failure post-fix
Pre-§75, this was a real failure mode. Post-fix (default 55), the model can no longer emit more lines than the parser accepts in normal cases. If you see `line_cap_exceeded` in the audit log on the new default, check: (a) did someone lower `EXTRACTION_MAX_TOTAL_LINES`, (b) did someone raise `EXTRACTION_MAX_ENTITIES_PER_CHUNK` / `..._RELATIONS_PER_CHUNK` / `..._FACTS_PER_CHUNK` above the budgeted slack, or (c) is the model emitting duplicates despite the prompt forbidding them (rare; usually a model regression).

---

## 🍎 Apple Silicon Hybrid Profile

### 79. Apple GPUs are not accessible from Docker Desktop
Docker Desktop on macOS cannot pass through the Apple GPU. A containerized embedder/reranker would silently fall back to CPU and run 10–20× slower than MLX on Metal. So Polymath ships a **hybrid profile** for Darwin/arm64:

- **Core stack** (Mongo, Qdrant, Neo4j, Redis, LiteLLM, backend, frontend, MCP) → Docker, unchanged
- **Embedder, reranker, docling** → host-native FastAPI sidecars under `launchd`, reached over `host.docker.internal`
- Compose override `docker-compose.apple-mlx.yml` rewrites `EMBEDDER_URL` / `RERANKER_URL` / `DOCLING_URL` to point at the host

Bootstrap: `bash scripts/install_apple_mlx_runtime.sh` (idempotent — runs the platform gate, stages code into `~/PolymathRuntime/apple_ml_services/`, sets up the uv venv, pre-pulls the MLX model weights, installs the LaunchAgent, smokes the endpoints). Bring up Docker with both files: `docker compose -f docker-compose.yml -f docker-compose.apple-mlx.yml up -d --build`.

### 80. `RERANKER_SCORE_SCALE=cosine` is mandatory for Jina v3
`mlx-community/jina-reranker-v3-4bit-mxfp4` returns **cosine scores in [0, 1]**, not logits. The retriever's negative-logit "low confidence" guard ([`config.py`](backend/config.py)) treats anything ≤ 0 as throwaway — every result looks broken. The override sets `RERANKER_SCORE_SCALE=cosine`. If you fork the override or write your own compose file for Mac, copy this env var or your retrieval will quietly fail.

### 81. The Jina v3 reranker projector is hand-built
Current `mlx-embeddings` cannot load Jina v3's quantized 2-layer MLP projector into the Qwen3 trunk automatically. The verified Mac Studio implementation builds an `MLPProjector(mlx.nn.Module)` by hand and loads only the trunk through `mlx-embeddings.utils.load`. The repo ships a scaffold at `scripts/apple_ml_services/reranker_mlx/main.py` with explicit `REPLACE THIS BODY` markers — drop the verified implementation in there before relying on the rerank scores. While the scaffold is in place, `/rerank` returns zeroes (intentional: signals the misconfiguration loudly rather than randomising relevance). The smoke script asserts ordering, so a missing projector fails fast.

### 82. Models live in HF cache at `~/PolymathRuntime/volumes/hf-cache`
`scripts/pull_apple_mlx_models.py` pre-warms the cache during install. Re-running is cheap. If you switch to a different MLX quantization, edit the `MODELS` list at the top of that script — and remember a different embedding repo means re-ingesting every corpus (Qdrant collections are dimension-locked, see §3).

### 83. The LaunchAgent is sticky — `KeepAlive=true` + `RunAtLoad=true`
The plist at `~/Library/LaunchAgents/com.polymath.apple-ml.plist` will restart the sidecars on crash AND on reboot. To stop them cleanly: `launchctl bootout gui/$(id -u)/com.polymath.apple-ml`. To restart: `launchctl kickstart -k gui/$(id -u)/com.polymath.apple-ml`. To wipe: delete the plist, run `bootout` once. Logs are at `~/PolymathRuntime/logs/apple_ml_services.{log,err.log}`.

### 84. The agent prompt for Apple setup lives at `docs/agent-prompts/mlx-setup.md`
When handing the repo to Claude Code or another coding agent for Apple bring-up, paste the prompt at `docs/agent-prompts/mlx-setup.md`. It enforces the order-of-operations (bootstrap → install MLX → docker up with both files → smoke → verify env wiring), names the constraints the agent must respect (no NVIDIA path, no embedding-dim changes, no PR bypass), and defines what "done" looks like.
