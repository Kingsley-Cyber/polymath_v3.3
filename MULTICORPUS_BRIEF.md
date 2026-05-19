# Polymath v3.3 — Multi-Corpus Refactor Brief

**Audience:** an LLM planning Items #4 (Mission Control multi-corpus), #5 (Legacy graph viewer multi-corpus), #6 (Schema Lens refresh) on the real Polymath v3.3 backend.

**Source of truth:** `github.com/Kingsley-Cyber/polymath_v3.3` @ origin/main HEAD = `6077dc4` (post Mac Studio handoff prep).

**Why this brief exists:** the original "Phased Rollout Plan" was written against a generic Node/Express + BullMQ + PostgreSQL stack. Polymath is FastAPI/Python + Qdrant + Neo4j + MongoDB + LiteLLM. The conceptual structure (normalize at boundary, batch reads, partial-failure maps, source_corpus attribution, dangling-edge marking, latest-updated merge) is sound — but **every file path, ORM call, queue invocation, and SQL fragment in the original plan is wrong for this repo**. This document re-grounds those concepts on the actual code.

## 0. End-state goal (locked)

**User-facing target:** select one or many corpora in the UI, graph viewer instantly merges and renders. Query in graph query expands into a query-unique animated view. New corpora ingested over time can be added to any selection without invalidating prior caches.

**Locked design decisions:**
- **Item #6 (Schema Lens refresh) — DEFERRED.** Not on the critical path for the multi-corpus query goal. Revisit later if extraction quality drift becomes a real problem.
- **PR order:** Item #5 first (additive multi-corpus endpoints, lowest risk) → Item #4 (Mission Control fan-out + merger + Mongo migration) → Phase 4 viewer rewrite. Frontend cutover is the final step.
- **Graph LLM synthesis logic stays untouched.** `_call_llm_synthesis`, `_build_insight_packet`, `_compact_packet_for_prompt`, `_render_packet_user_prompt`, the `_legacy.discover` scoping, `discover()` wrapper — all preserved as-is. Multi-corpus extends the wrapper at the input boundary and merges at the output boundary; the synthesis call path in the middle is unchanged.
- **Brain view color-by:** community.
- **Query view animation:** one-shot, fast, gravity/settle feel. No replay button, skip toggle in settings.
- **Dimensionality:** 2D default. No 3D toggle in v1.
- **Drill interaction:** in-place replace (click supernode → same view re-renders with that cluster's full detail). No side panels.
- **Concurrency cap on multi-corpus fan-out:** `asyncio.Semaphore(4)` to bound LLM/DB pressure.
- **Per-corpus quota for LLM packet slots:** floor model (every selected corpus contributes ≥1 entity/edge/evidence slot, then global top fills the rest).
- **Cache warming UX:** partial render + `cache_warming_corpora: [cid]` chip. Never block.
- **Entity canonicalization across corpora:** out of scope for v1. Same `entity_id` merges (already does); lexically-similar but distinct-id entities stay separate.
- **`max_corpora_per_query: 32`** (existing setting respected). Above 32 → 400.

**Latent risk to address in parallel:** `_orchestrator_legacy.cpython-311.pyc` is a sourceless compiled module. If it ever breaks or is lost, all of Mission Control breaks. **Recommend a parallel track to reconstruct it as tracked Python source** — independent of multi-corpus, just for survivability.

**Cross-doc reconciliation status (2026-05-10):** This brief, [Phased Rollout Plan — Single corpus.txt](../../Phased Rollout Plan — Single corpus.txt), and [GRAPH_VIEWER_BRIDGE.md](../../GRAPH_VIEWER_BRIDGE.md) are now consistent on:
- Schema Lens deferred (active phases skip it; technical content preserved as "Phase A — [DEFERRED]" in the rollout plan)
- Library = `react-force-graph-2d` (sigma.js / graphology retired in Bridge §10 Phase F)
- Endpoints added in PR 2: `POST /api/graph/cluster/{concept_id}`, `GET /api/corpora/{cid}/cache-status`, plus the `top_entities` cap bump
- `POST /api/graph/query` multi-corpus fan-out lands in PR 3 alongside `/discover`
- Zero-corpora behavior: empty-state prompt, no auto-fallback (legacy [App.tsx:67-88](../../frontend/src/App.tsx) auto-fallback retired in Phase F)
- Phase F cleanup retires GraphView, DiscoveryPanel, BooksClusterView, RelationGraph, the `mission-control-context-graph` CustomEvent, and sigma.js/graphology dependencies

---

## 1. Stack reality

| Layer | What the original plan assumed | What Polymath actually uses |
|---|---|---|
| HTTP | Node/Express + TypeScript | **FastAPI 0.x + Python 3.11** (`backend/main.py`) |
| Orchestration | `MissionControl` TS class with ~30 methods | **`services/graph/orchestrator.py`** + a sourceless legacy `.pyc` loaded at import time |
| Workers | BullMQ | **In-process `asyncio` background tasks**, kept alive by `_INGEST_BG_TASKS: set[asyncio.Task]` (`routers/ingestion.py`). No queue. |
| Vector store | (n/a in plan) | **Qdrant**, per-corpus collections named `corpus_{cid8}_{naive\|hrag\|graph\|schemas}` (`services/storage/qdrant_writer.py:_col_for_corpus`). Migration `migrations/001_per_corpus_qdrant.py` already split global → per-corpus. |
| Graph store | (n/a) | **Neo4j**. Chunks have `c.corpus_id`. Relations carry `r.corpus_ids: list` so a single edge can already span corpora (see `services/graph/analytics.py:_RELATION_CYPHER`, line 1611). |
| Document store | PostgreSQL with `WHERE corpus_id = ANY($1)` | **MongoDB via Motor**. Collections: `corpora`, `documents`, `chunks` (sometimes), `graph_sessions`, `graph_domain_cache`, `graph_metrics_cache`, `analytics_audit_log` (does NOT exist — has to be created if needed). |
| Audit table | `analytics_audit_log` (Postgres DDL) | **Does not exist**. Schema lens versioning lives on `corpora.schema_lens.version` ("polymath.schema_lens.v1"). |
| Cache invalidation | (n/a) | **`corpus_change_signature`** = sha256 of sorted `(doc_id, updated_at)` pairs (`services/graph/analytics.py:543`). All caches keyed by `(corpus_id, signature)`. |
| Auth | (n/a) | Custom Mongo-backed bearer-token auth in `services/auth.py`. `get_current_user` from `routers/auth.py`. **Users have no `email` field** — only `{_id, username, hashed_password, created_at}`. |
| Frontend | (n/a) | React/Vite at `https://kingsleylab.xyz`. CORS allows localhost:3000/5173 + the prod hosts. |

**Models everywhere:** Qwen3-Embedding-0.6B at 1024 dims (ingest + query). Reranker `ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF` via llama.cpp. LLMs via LiteLLM proxy (`settings.LITELLM_URL`, `settings.LITELLM_MASTER_KEY`).

**Hard rule from CLAUDE.md:** never mix 0.6B and 4B vectors in one Qdrant collection. Multi-corpus retrieval = querying multiple per-corpus collections, NOT merging them at the storage layer.

---

## 2. The three items, mapped to real files

### Item #6 — Schema Lens refresh

**Original plan called this:** "Analytics Lens Drift" — a Python `label_clusters()` function that needed multi-corpus support, dry-run mode, idempotency keys, and a Postgres `analytics_audit_log` table.

**Reality:** the Polymath analogue is the **Schema Lens** — `services/ingestion/schema_lens.py`. It's already per-corpus and is invoked PER DOCUMENT during Ghost-B ingestion (the entity/relation extractor). Stored on `db.corpora.<doc>.schema_lens` keyed by corpus.

**Key surface:**
- `build_deterministic_schema_lens(*, corpus_id, filename, parents, children, entity_schema, relation_schema) -> SchemaLens` (line 220) — pure function, no I/O
- `sanitize_schema_lens(payload, *, base, entity_schema, relation_schema, source) -> SchemaLens` (line 281) — clamps LLM output to approved vocab
- `merge_schema_lenses(stored, doc_lens, *, entity_schema, relation_schema) -> SchemaLens` (line 344)
- `_profile_with_llm(*, sample, base, entity_schema, relation_schema, pool, model)` (line 376) — optional one LLM call per first-doc-in-corpus
- `get_or_create_schema_lens(*, db, corpus_id, filename, parents, children, entity_schema, relation_schema, pool, model) -> SchemaLens` (line 461) — top-level entry, persists to `corpora.schema_lens`

**Storage:**
```python
db["corpora"].update_one(
    {"corpus_id": corpus_id},
    {"$set": {
        "schema_lens": lens.to_dict(),
        "schema_lens_updated_at": datetime.utcnow(),
    }},
)
```

**Schema Lens shape** (`SchemaLens` dataclass in `services/ghost_b.py:723`):
```
lens_id, version="polymath.schema_lens.v1", status, source,
corpus_domains: list[str],
preferred_entity_types: list[str],
preferred_relations: list[str],
relation_aliases: dict[str, str],     # phrase → approved predicate
object_kinds: list[str],
canonical_families: list[str],
confidence: float
```

**What "Schema Lens refresh" actually means in Polymath terms:**
1. The corpus accumulates documents over time. The lens was generated from the FIRST doc and merged from each subsequent doc via `merge_schema_lenses` in `get_or_create_schema_lens`. After 100+ docs, the lens may have drifted or missed important domains.
2. **Refresh = re-derive the lens from all docs in a corpus** (or many corpora) without re-running ingestion.
3. There is currently NO endpoint, NO callable, and NO admin UI for this. The lens is only ever updated as a side-effect of ingesting a new doc.

**What the original plan's Item #6 maps to in this context:**
- "Accept `corpus_ids[]`" → support refreshing N corpora in one call
- "Dry-run mode" → compute the new lens, return diff vs stored, do not persist
- "Idempotency key" → `audit_id` keyed lookup; same `(corpus_ids, schema_version)` → cached result
- "Audit log" → new Mongo collection `schema_lens_refresh_audit` (no Postgres). Document shape:
  ```
  { audit_id, corpus_ids, checksum, status, created_at, result }
  ```
- "Scope validation" → verify each corpus_id exists in `corpora` collection
- "Scope enforcement" → after re-derive, assert lens references only entities sampled from the requested corpora

**Why this is a small refactor, not a big one:** the existing functions are pure-ish. The work is mostly:
- A new orchestrating function `refresh_schema_lens_for_corpora(db, corpus_ids: list[str], *, dry_run, audit_id, force_llm) -> RefreshResult`
- A new endpoint `POST /api/admin/schema-lens/refresh`
- A new Mongo collection + tiny audit helper
- Tests (mirror `tests/test_schema_lens.py`)

---

### Item #4 — Mission Control multi-corpus

**Original plan called this:** "MissionControl TypeScript class" with ~30 methods that take `corpus_id: string` — refactor each to `corpus_ids: string[]`.

**Reality in Polymath:** Mission Control is the **graph synthesis feature** — `POST /api/graph/discover` → `services/graph/orchestrator.discover()`. It's currently strictly single-corpus. It has SEVEN sibling functions on the same module:

| Function | Signature today | What it does |
|---|---|---|
| `discover` | `(*, qdrant, neo4j_driver, db, corpus_id: str, query, mode, session_id, user_id, model_override, agentic) -> Result` (line 3260) | Auto-Synthesis wrapper around `_legacy.discover`. Builds a `GraphInsightPacket`, calls one LLM, persists turn |
| `list_sessions` | `(db, *, corpus_id: str, user_id) -> list[dict]` | Mongo `graph_sessions` filter |
| `get_session` | `(db, session_id, *, user_id) -> dict` | Single session detail |
| `find_resume_candidate` | `(db, *, corpus_id: str, query, user_id, threshold) -> dict \| None` | Embedding cosine similarity over prior session queries |
| `build_corpus_suggestions` | `(*, qdrant, neo4j_driver, db, corpus_id: str, user_id) -> dict` | Domain-map seeded prompt suggestions |
| `delete_session` | `(db, session_id, *, user_id) -> bool` | Single session delete |
| `_qdrant_collections_for_packet` | `(corpus_id) -> dict[str,str]` (line 694) | Returns `{naive, hrag, graph, schemas}` per-corpus collection names |

**The orchestrator.py module is ~3400 lines.** Most of it is helpers for evidence curation, prompt building, source labelling, and packet construction. The `corpus_id` parameter threads through:
- Mongo queries against `documents`, `chunks`, `graph_sessions`, `graph_domain_cache`, `graph_metrics_cache`
- Qdrant filters via `_qdrant_collections_for_packet(corpus_id)`
- Neo4j Cypher (`WHERE c.corpus_id = $corpus_id`)
- The legacy `.pyc` shim — `_legacy.discover(... corpus_id=corpus_id ...)`

**The legacy `.pyc` shim is the architectural blocker.** `services/graph/orchestrator.py:34-53` loads `_orchestrator_legacy.cpython-311.pyc` (a sourceless compiled module). The new `discover()` wrapper still calls `_legacy.discover(corpus_id=corpus_id, ...)` (line 3282). **You cannot change the legacy signature without restoring its source.** The repo notes this explicitly:
```python
_LEGACY_MISSING_MESSAGE = (
    "Graph discovery legacy scope module is unavailable. Reconstruct "
    "services.graph._orchestrator_legacy as tracked Python source, or restore "
    "_orchestrator_legacy.cpython-311.pyc for legacy graph discovery."
)
```

**Two real options for Item #4:**

**Option A (recommended): Wrapper-fan-out.** Keep the legacy `.pyc` single-corpus. Make the new `discover()` accept `corpus_ids: list[str]`, call the legacy fan-out N times in parallel (`asyncio.gather`), then merge the results in the new wrapper. The merger lives outside the legacy code.

**Option B (heavier): Reconstruct the legacy as Python source.** Real cost is unknown — it's compiled bytecode without source. Plan would need an exploratory step to decompile or rewrite.

**Mongo session storage shape** (`graph_sessions` collection):
```
{
  session_id: str,
  corpus_id: str,            # SINGLE today — needs to become corpus_ids: list[str]
  user_id: str,
  title, created_at, updated_at, turn_count,
  first_query: str,
  turns: [GraphDiscoverTurn, ...]
}
```

A multi-corpus session must decide: do existing single-corpus sessions auto-upgrade to `corpus_ids: [<that_one>]` on read? Or do you keep both fields and a normaliser? **Recommend: keep `corpus_id` as a deprecated alias on the response model, switch internal storage to `corpus_ids: list[str]`, and write a one-shot Mongo migration in `lifespan` like the existing `migrate_universal_schema` and `migrate_bare_model_names` patterns.** That migration sets `corpus_ids = [corpus_id]` and deletes `corpus_id` on every existing session.

**Pydantic shape changes (`backend/models/schemas.py`):**
- `GraphDiscoverRequest` (line 447): `corpus_id: str` → `corpus_ids: list[str]` plus a `model_validator(mode="before")` that wraps a legacy `corpus_id` string into a 1-element list. Same for `GraphResumeCandidateRequest`.
- `GraphDiscoverResponse` (line 458) and `GraphDiscoverSession` (line 500): include both `corpus_id: str = ""` (deprecated, set to first id for legacy clients) AND `corpus_ids: list[str] = []`.
- `GraphInsightPacket` (line 639): same dual-field treatment.
- Every node/edge in `ContextGraphNode/Link/Payload` and `GraphInsightPacketEntity/Edge` should grow a `source_corpus: str = ""` field (per the original plan's nuance #2).

**The `_legacy` orchestrator's emergent caches need careful handling:**
- `graph_domain_cache`, keyed by `(corpus_id, corpus_change_signature)` (`services/graph/analytics.py:561`)
- `graph_metrics_cache`, keyed by `(corpus_id, corpus_change_signature)` (line 576)
- `graph_anchor_cache`

These are **per-corpus by design** because they hold expensive Louvain/PageRank/concept-community results. Multi-corpus discover should NOT try to compute a unified domain map across corpora at request time. Instead: load each corpus's cached domain map + metrics, then merge in the wrapper. If the cache is cold for any corpus, return a `cache_warming` flag so the UI can show partial results.

---

### Item #5 — Legacy graph viewer multi-corpus

**Original plan called this:** "Graph Full Endpoint" — add `POST /v2/graph/full` taking `corpus_ids[]`, leave `GET /:corpus_id/graph/full` untouched.

**Reality in Polymath:** there are TWO legacy single-corpus endpoints in `routers/graph.py`:

| Endpoint | Backed by | Returns |
|---|---|---|
| `GET /api/corpora/{corpus_id}/graph/overview` (line 57) | `services.graph.overview.get_cached_graph_overview(db, corpus_id, ...)` | Cached supernode graph (domain × concept-community), 80 concepts, 220 edges |
| `GET /api/corpora/{corpus_id}/graph/full` (line 86) | `services.graph.neo4j_reader.get_full_corpus_graph(driver, corpus_id, ...)` | Full entity graph for WebGL viewer (sigma.js + graphology), 20k nodes / 60k edges cap |

**The new multi-corpus template already exists.** `POST /api/graph/by-document` (line 425, commit 55f759b yesterday) accepts `body.corpus_ids: list[str]` and routes through `services.graph.neo4j_reader.get_documents_as_clusters(driver, corpus_ids, ...)` which uses the canonical Cypher pattern:
```cypher
MATCH (c:Chunk)-[:MENTIONS]->(e:Entity)
WHERE c.corpus_id IN $corpus_ids
WITH c.doc_id AS doc_id, c.corpus_id AS corpus_id, e, count(c) AS mention_count
...
```
And for relations:
```cypher
MATCH (a)-[r:RELATES_TO]->(b)
WHERE any(cid IN $corpus_ids WHERE cid IN coalesce(r.corpus_ids, []))
```

**This pattern is the contract.** Item #5 should mirror it exactly.

**What "Item #5: legacy graph viewer multi-corpus" means concretely:**
1. New endpoints (do not modify the legacy ones — the React viewer still calls them):
   - `POST /api/graph/full` → `body: {corpus_ids: list[str], max_nodes, max_edges, min_entity_mentions}` → returns the same shape `{nodes, edges, truncated, _meta: {source_corpora, errors}}`
   - `POST /api/graph/overview` → `body: {corpus_ids, max_concepts, max_edges}` → merged supernode graph
2. New Cypher functions in `services/graph/neo4j_reader.py`:
   - `get_full_corpora_graph(driver, corpus_ids: list[str], *, max_nodes, max_edges) -> dict`
   - Mirrors `get_full_corpus_graph` but with `WHERE c.corpus_id IN $corpus_ids` and `r.corpus_ids` filter
3. New `services/graph/overview.py` function:
   - `get_cached_graph_overview_multi(db, corpus_ids: list[str], *, max_concepts, max_edges) -> dict`
   - Loads each corpus's cached `DomainMap` + `CorpusMetrics` and runs the existing `build_overview_graph` over a merged structure (or returns per-corpus results with cross-corpus bridges added at the merger layer)
4. **Per-corpus `cache_warming` semantics:** if any corpus's cache is missing, return `_meta.cache_warming_corpora: [cid, ...]` rather than failing. The caller decides whether to render partial.

**Frontend impact:** the React graph viewer (`frontend/...`) currently calls `GET /api/corpora/{corpus_id}/graph/full`. The new endpoint is additive — no breaking change. Frontend migration can land in a separate PR.

---

## 3. Existing multi-corpus patterns to mirror (don't reinvent)

The repo already has THREE working multi-corpus patterns. The plan should extend these, not invent new ones.

### Pattern 1: `POST /api/graph/by-document` (`routers/graph.py:425-524`)
Body: `{corpus_ids: list[str], mode: "overview"|"drill"|"full", drill_doc_id?, min_entity_mentions, max_nodes, max_edges}`. Validation:
```python
corpus_ids = body.get("corpus_ids") or []
if not isinstance(corpus_ids, list) or not corpus_ids:
    raise HTTPException(status_code=400, detail="corpus_ids must be a non-empty list")
corpus_ids = [str(c) for c in corpus_ids]
```

### Pattern 2: `POST /api/graph/entity-search` (`routers/graph.py:385-417`)
Already accepts `body.corpus_ids: list[str]`. Routes to `services/retriever/mode_b.py:ModeBExpansion.search(query, corpus_ids, limit)` and `services/retriever/hydrate.py:hydrate_chunks(chunks, corpus_ids)`.

The Cypher in `mode_b.py:50-51`:
```python
if corpus_ids:
    cypher += "  AND c.corpus_id IN $corpus_ids\n"
```

### Pattern 3: Neo4j Cypher `WHERE … IN $corpus_ids`
Two variants used throughout `services/graph/neo4j_reader.py`:
- For chunk-anchored queries: `WHERE c.corpus_id IN $corpus_ids`
- For relation-anchored queries (relations carry their own multi-corpus list): `WHERE any(cid IN $corpus_ids WHERE cid IN coalesce(r.corpus_ids, []))`

**Use these exact phrasings in any new Cypher.** They handle the dangling-edge case correctly because the inner `coalesce` defaults to `[]`.

---

## 4. Storage shapes & corpus keying

### MongoDB collections (all on `conversation_service._db`)
| Collection | Key fields | Notes |
|---|---|---|
| `corpora` | `corpus_id` (UUID4 string) | Holds `name, default_ingestion_config, schema_lens, schema_lens_updated_at`. Multi-corpus refactor does NOT touch the doc shape. |
| `documents` | `corpus_id, doc_id` | Has `filename, ghost_b_metrics, updated_at`. The signature key. |
| `graph_sessions` | `session_id` (PK), `corpus_id` (today), `user_id` | Will become `corpus_ids: list[str]` (with migration). |
| `graph_domain_cache` | `corpus_id, corpus_change_signature` | Per-corpus emerged domain clusters. |
| `graph_metrics_cache` | `corpus_id, corpus_change_signature, schema_version` | Per-corpus structural metrics. **Schema version bumps invalidate cache** — current `METRICS_CACHE_SCHEMA_VERSION = 11`. |
| `graph_anchor_cache` | corpus_id + anchor terms | Query-anchor reuse cache. |
| `analytics_audit_log` | **DOES NOT EXIST** | Original plan invented a Postgres table. If Item #6 needs an audit log, create a new Mongo collection. |
| `users` | `_id, username, hashed_password, created_at` | **No `email` field**. Auth is bearer tokens. Source of truth: `services/auth.py`. |

### Qdrant collections (all on `ingestion_service.qdrant_client`)
- Naming: `corpus_{cid8}_{naive|hrag|graph|schemas}` via `services/storage/qdrant_writer.py:_col_for_corpus`
- The 8-char prefix derives from `corpus_id[:8]`. There's a deliberate guard against prefix collisions in `_assert_collection_owner` (line 171).
- Multi-corpus retrieval = parallel queries against `[_col_for_corpus(cid, kind) for cid in corpus_ids]`, then merge by score.

### Neo4j node properties
- `Chunk` has `corpus_id` (single value).
- `Document` has `corpus_id, doc_id, filename`.
- `Entity` has `entity_id, display_name, normalized_name, primary_entity_type, object_kind, canonical_family, ontology_version`. **No corpus_id on Entity** — entities can be referenced from multiple corpora.
- `RELATES_TO` edge has `corpus_ids: list[str]` (already multi-corpus!), `evidence_chunk_ids: list[str]`, `predicate, relation_family, edge_strength, eligible_for_synthesis, confidence`.

This means the graph layer was DESIGNED for multi-corpus from the start at the storage level — only the API and orchestrator are stuck on single corpus.

---

## 5. Auth, sessions, ownership

- All Mission Control endpoints already use `current_user: dict = Depends(get_current_user)` from `routers/auth.py`.
- Session ownership check pattern:
  ```python
  if body.session_id:
      owner = await db["graph_sessions"].find_one(
          {"session_id": body.session_id},
          {"user_id": 1, "_id": 0},
      )
      if owner and owner.get("user_id") and owner["user_id"] != current_user["user_id"]:
          raise HTTPException(status_code=404, detail="Session not found")
  ```
- For multi-corpus, **add a per-user authorization check that the user owns ALL requested corpora**. There is no per-corpus ACL today (every authed user can read every corpus), but if that ever changes, this is the chokepoint.

---

## 6. Cache & idempotency — the `corpus_change_signature` pattern

`services/graph/analytics.py:543`:
```python
async def compute_corpus_change_signature(db, corpus_id: str) -> str:
    cursor = db["documents"].find(
        {"corpus_id": corpus_id},
        {"doc_id": 1, "updated_at": 1, "_id": 0},
    ).sort("doc_id", 1)
    docs = await cursor.to_list(length=None)
    parts = [f"{d['doc_id']}:{(d['updated_at'].isoformat() if isinstance(d['updated_at'], datetime) else str(d.get('updated_at') or ''))}" for d in docs]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
```

**For multi-corpus caching, define:**
```python
async def compute_multi_corpus_signature(db, corpus_ids: list[str]) -> str:
    sigs = []
    for cid in sorted(corpus_ids):
        sigs.append(f"{cid}:{await compute_corpus_change_signature(db, cid)}")
    return hashlib.sha256("|".join(sigs).encode("utf-8")).hexdigest()
```

Use this signature as the cache key for any merged-graph cache or Schema Lens refresh audit entry.

---

## 7. Test patterns

Tests live in `backend/tests/`. The graph subdir (`backend/tests/graph/`) has the closest patterns:
- `test_orchestrator_payload.py` — builds a `CorpusMetrics` fixture, calls `_build_insight_packet` etc. directly, no DB.
- `test_metrics_cache_compaction.py`, `test_metrics_detectors.py`, `test_domain_emergence.py` — async tests with `pytest.mark.asyncio` and Motor mocks.
- `test_schema_lens.py` — pure-function tests for `build_deterministic_schema_lens` and `sanitize_schema_lens`.

**Test layout for the multi-corpus refactor:**
```
backend/tests/
  graph/
    test_discover_multi_corpus.py        # Mission Control fan-out
    test_full_graph_multi_corpus.py      # Item #5
    test_overview_multi_corpus.py        # Item #5 cached overview merge
    test_corpus_signature.py             # multi-corpus signature determinism
  ingestion/
    test_schema_lens_refresh.py          # Item #6
```

**Key invariants every test must check:**
1. **Single-element equivalence:** calling `discover(corpus_ids=[X])` produces a result that is structurally identical to the legacy `discover(corpus_id=X)` modulo the new `source_corpus` fields. This is the regression gate.
2. **Order independence:** `discover(corpus_ids=[A,B])` and `discover(corpus_ids=[B,A])` produce the same merged graph (modulo ordering of node/edge lists, which the merger sorts).
3. **Idempotency under duplicates:** `discover(corpus_ids=[A,A,B])` == `discover(corpus_ids=[A,B])`.
4. **Partial failure:** if one corpus's cache is missing, the response carries `_meta.cache_warming_corpora: [cid]` and still returns merged data for the others.

---

## 8. Refined plan, mapped to real files

### Phase 1 — Item #6 (Schema Lens refresh) — 1.5h on the real code

| Step | Action | Files | Checkpoint |
|---|---|---|---|
| 1.1 | Add `refresh_schema_lens_for_corpora(db, corpus_ids: list[str], *, dry_run: bool = False, audit_id: str \| None = None, force_llm: bool = False, pool: list[dict], model: str \| None = None) -> RefreshResult` | new function in `services/ingestion/schema_lens.py` | Returns `{audit_id, status, results: {cid: {old, new, diff}}, errors: {cid: msg}}` |
| 1.2 | Validate every corpus exists | same file | Bad IDs raise 400 with the list |
| 1.3 | Compute `multi_corpus_signature` and check `schema_lens_refresh_audit` for existing `audit_id` | new Mongo collection | Cached returns short-circuit |
| 1.4 | Fan-out per corpus: load all docs from `documents`, build merged deterministic lens, optionally call `_profile_with_llm` once per corpus, sanitize | reuse existing helpers | `dry_run=True` returns diffs without persisting |
| 1.5 | Persist via `db.corpora.update_one({"corpus_id": cid}, {"$set": {"schema_lens": lens.to_dict(), ...}})` only when `dry_run=False` | same file | Storage matches existing `get_or_create_schema_lens` write |
| 1.6 | Write audit entry on success | new Mongo collection | One entry per `audit_id` |
| 1.7 | New endpoint `POST /api/admin/schema-lens/refresh` → `body: {corpus_ids: list[str], dry_run, audit_id, force_llm}` | new router or extend `routers/ingestion.py` | Auth required |
| 1.8 | Tests | `backend/tests/test_schema_lens_refresh.py` | Single-corpus equivalence + dry-run no-write + idempotency |

**Hard rule:** the LLM profiling step (`_profile_with_llm`) makes a network call. Cap concurrency at `min(len(corpus_ids), 4)` with `asyncio.Semaphore` so a 50-corpus refresh doesn't hammer LiteLLM.

### Phase 2 — Item #4 (Mission Control multi-corpus) — 3h on the real code

| Step | Action | Files | Checkpoint |
|---|---|---|---|
| 2.1 | Add `from utils.corpus_ids import normalize_corpus_ids, multi_corpus_signature` (new utility) | new `utils/corpus_ids.py` | One module imported everywhere |
| 2.2 | Update Pydantic models — `GraphDiscoverRequest`, `GraphDiscoverSession`, `GraphResumeCandidateRequest` get `corpus_ids: list[str]` AND keep `corpus_id: str` deprecated | `models/schemas.py` | `model_validator(mode="before")` wraps legacy `corpus_id` string into `corpus_ids=[corpus_id]` |
| 2.3 | **Decide: wrapper-fan-out vs. legacy reconstruction.** Recommend wrapper-fan-out: change new `discover()` to accept `corpus_ids`, run `await asyncio.gather(*[_legacy.discover(corpus_id=cid, ...) for cid in corpus_ids], return_exceptions=True)` | `services/graph/orchestrator.py` line 3260 | Per-corpus errors collected, never crash the whole call |
| 2.4 | Build a new merger `merge_discover_results(results: list[Result]) -> Result` that union-dedups graph nodes (key: `entity_id`), unions edges (key: `(source, target, predicate)`), concatenates `frontier/analogies/bridges/weak_links/transfers/questions` with `source_corpus` attribution, picks the highest-confidence `interpretation`/`headline` | new helper in `orchestrator.py` | Single-corpus call → identical output (modulo new fields) |
| 2.5 | Update `_qdrant_collections_for_packet` to a list version: `_qdrant_collections_for_packet_multi(corpus_ids) -> dict[str, dict[str, str]]` (per-corpus inner dict) | line 694 | Each retrieval routes to per-corpus collections in parallel |
| 2.6 | Add Mongo migration in `lifespan` that rewrites `graph_sessions.corpus_id` → `corpus_ids: [corpus_id]` | `main.py` near other migrations | Idempotent: skip docs that already have `corpus_ids` |
| 2.7 | Update `list_sessions`, `get_session`, `find_resume_candidate`, `delete_session`, `build_corpus_suggestions` signatures to `corpus_ids: list[str] \| None` | `orchestrator.py` | Single-corpus listing still works |
| 2.8 | Update router (`routers/graph.py:527+`) to pass `corpus_ids=normalize_corpus_ids(body.corpus_id, body.corpus_ids)` | router | Backward compatible |
| 2.9 | Add `source_corpus` field to every node/edge/entity in `models/schemas.py` (`ContextGraphNode`, `ContextGraphLink`, `GraphInsightPacketEntity`, `GraphInsightPacketEdge`) | `models/schemas.py` | Default `""` so existing clients don't break |
| 2.10 | Tests — single-element equivalence, order independence, idempotency under duplicates, partial failure (one corpus has no Neo4j data) | `tests/graph/test_discover_multi_corpus.py` | All four invariants pass |

**Critical gotcha:** the `_legacy.discover` call inside `discover()` builds a `GraphInsightPacket` with `corpus_id: str` baked in (`schemas.py:646`). If you fan out, each fan-out call produces a separate packet. The merger needs to either (a) build a meta-packet that embeds per-corpus packets, or (b) return one merged packet with `source_corpus` on every entity. **Recommend (b)** — the LLM synthesis prompt is already cap-limited and adding meta-packet structure breaks the LLM contract. Update `GraphInsightPacket.corpus_id: str` → `corpus_ids: list[str]` and add `source_corpus` on the entity-level subtypes.

### Phase 3 — Item #5 (Legacy graph viewer multi-corpus) — 1.5h on the real code

| Step | Action | Files | Checkpoint |
|---|---|---|---|
| 3.1 | Add `get_full_corpora_graph(driver, corpus_ids: list[str], *, max_nodes, max_edges) -> dict` | `services/graph/neo4j_reader.py` | Uses `WHERE c.corpus_id IN $corpus_ids` for nodes, `WHERE any(cid IN $corpus_ids WHERE cid IN coalesce(r.corpus_ids, []))` for edges |
| 3.2 | Add `get_cached_graph_overview_multi(db, corpus_ids: list[str], *, max_concepts, max_edges) -> dict` — loads each corpus's `DomainMap` + `CorpusMetrics`, runs existing `build_overview_graph` on a merged structure | `services/graph/overview.py` + new merger in `services/graph/analytics.py` | Single-corpus call returns identical shape to current `get_cached_graph_overview` |
| 3.3 | Two new endpoints: `POST /api/graph/full` and `POST /api/graph/overview` (NOT replacing the GET ones) | `routers/graph.py` after the by-document endpoint | Keep legacy GETs; new POSTs accept `corpus_ids` body |
| 3.4 | Dangling-edge marking: when an edge references a node not in the loaded set (because that corpus's graph isn't in `corpus_ids`), set `edge.dangling = true` rather than dropping | merger logic | Test asserts dangling edges are kept and flagged |
| 3.5 | Per-corpus cache_warming flag in response: `{nodes, edges, truncated, cache_warming_corpora: [cid, ...]}` | both new endpoints | Test simulates one missing cache |
| 3.6 | Performance test: 5 corpora × 10k entities, total time < 2s, memory < 500 MB | `tests/graph/test_full_corpora_graph_perf.py` | Skipped in CI by default, runs locally |

**Deferred-to-frontend:** the React viewer migration to call the new POST endpoint. That should be a separate PR after backend merges.

---

## 9. What to keep from the original plan

Concepts that survive verbatim:
- **Normalize-at-boundary** — single `normalize_corpus_ids(input: str | list[str]) -> list[str]` function. Eliminates feature flag.
- **Source-corpus attribution** on every returned entity/node/edge.
- **Call-site classification (read/mutate/serialize)** — for Polymath this is mostly read because mutation lives in the ingestion path which is already per-corpus. The Mission Control fan-out is read-only against Mongo/Neo4j/Qdrant.
- **Dangling-edge `dangling: true` flag** — applies directly to Item #5's graph merger.
- **Latest-updated wins for node attribute conflicts** — Polymath's `Entity` nodes don't have `updated_at`, but `Document.updated_at` is the proxy. For Item #5, when the same entity appears in multiple corpora with different display names, prefer the one whose source document is newest.
- **Sharper contract tests** — directly applicable as the four invariants in §7.

## 10. What to discard from the original plan

- All Postgres DDL (`analytics_audit_log` etc.). Use Mongo.
- All BullMQ patterns. Use `asyncio.Semaphore` + `asyncio.gather`.
- All `MissionControl` TypeScript class refactor instructions. The Polymath analogue is one ~3400-line Python module + a sourceless `.pyc`.
- The `ENABLE_MULTI_CORPUS` feature flag. Use input normalization (per the plan's own nuance #1).
- The "create new endpoint at `/v2/graph/full`" path-versioning. Polymath uses `POST /api/graph/...` for body-param endpoints (see by-document and entity-search).
- The Phase 3 "memory blow-up" guard for streaming JSON parsers. Polymath caps at `max_nodes=20000, max_edges=60000` and returns `truncated: true` instead.

## 11. Polymath-specific risks

| Risk | Detection | Mitigation |
|---|---|---|
| **Legacy `.pyc` does not exist on a fresh clone.** `_orchestrator_legacy.cpython-311.pyc` is gitignored; without it, `discover()` raises `RuntimeError(_LEGACY_MISSING_MESSAGE)`. | First-time deploy fails | Document as a known precondition. The `.pyc` ships separately. |
| **Per-corpus Qdrant collections may not exist for newly ingested corpora.** If `discover(corpus_ids=[fresh_cid])` runs before ingestion finishes, the Qdrant queries return empty. | Empty packet for one of N corpora | Already handled by `_legacy.discover` returning empty; merger should treat as `cache_warming` |
| **`graph_metrics_cache.schema_version` (currently 11) bumps invalidate ALL cached metrics.** A schema bump during the multi-corpus refactor will leave every corpus cold. | Long first-discover after deploy | Bake the cache warm into `lifespan` for active corpora, or accept the warm-up cost |
| **Schema Lens version is `polymath.schema_lens.v1`.** A v2 lens means any cached lens is stale and `merge_schema_lenses` returns the new doc lens unchanged. | Lens regenerates from scratch on first new doc | Keep the version constant unless the lens shape changes |
| **`migrate_universal_schema` runs at every startup** — non-fatal, but if it patches a corpus during a multi-corpus refresh that's also reading lens data, a write race is possible. Low probability. | Logs show patched count > 0 alongside refresh activity | Migration runs at startup only; refresh runs on demand. Order is fine in practice. |
| **No queue means an OOM kills the whole API.** Multi-corpus discover with 10 cold corpora may try to load 10 `DomainMap`s + 10 `CorpusMetrics` simultaneously. | Container OOM kill | Cap concurrency in the fan-out: `asyncio.Semaphore(4)` |
| **The 8-char Qdrant prefix collision** (`corpus_id[:8]`). Two corpora with first-8-chars colliding would currently raise from `_assert_collection_owner`. Multi-corpus retrieval inherits this assertion. | Startup of ingestion fails | Already guarded; not introduced by this refactor |
| **`max_corpora_per_query` setting.** `RetrievalSettings.max_corpora_per_query` exists in `models/schemas.py:320` (default 32, cap 100). Multi-corpus discover/full/overview should respect this cap and 400 above it. | Test asserts 400 at 33 corpora when limit is 32 | Read setting in router, validate before fan-out |

---

## 11.5. Phase 4 — Graph viewer rewrite (frontend)

**Goal:** one viewer component, two modes — Brain View (whole graph, supernode, scales without rendering everything) and Query View (focused subgraph from a Mission Control or graph_query turn, with a one-shot gravity entry animation). Aesthetic reference: VOSviewer-style dense colored network on black background, communities tightly clustered, edges thin and translucent, labels only on prominent nodes.

**Library choice — react-force-graph-2d.** Built on three.js + d3-force, WebGL-rendered, supports custom node/edge canvas drawing, particle effects, and large graphs (10k+ nodes). Cost: ~200KB bundle. Drop sigma.js for the new viewer. Keep the existing graphology data prep utilities — they're reusable.

**Backend contracts the viewer consumes (all already exist or land in Items #4/#5):**

| View mode | Endpoint | Returned shape |
|---|---|---|
| Brain (single corpus) | `GET /api/corpora/{cid}/graph/overview` | Cached supernode graph: domains × concept communities. Already capped at 80 concepts / 220 edges. |
| Brain (multi corpus) | `POST /api/graph/overview` (Item #5, new) | Same shape, merged across `corpus_ids`. `cache_warming_corpora` flag on partial. |
| Brain drill (one cluster) | `POST /api/graph/full` (Item #5, new, filtered) | Full entity graph scoped to the clicked cluster's `entity_ids`. Same node/edge shape as `get_full_corpus_graph`. |
| Query | `POST /api/graph/discover` `context_graph` field | Already query-shaped. Nodes have `kind`, `role`, `topic_id`. Links have `kind`, `role`, `weight`, `evidence`. |
| Query (entity expansion) | `POST /api/graph/query` | `nodes`, `links`, `seed_entities`, `bridges`, `hubs`, `gaps`. |

**Visual contract:**

- **Black background** (`#0a0a0a`).
- **Community color palette** — d3.schemeCategory10 or a curated 12-color palette. Backend already returns `concept_id` / `domain` per node; frontend maps to color. For multi-corpus brain view with the floor-model merge, communities span corpora — color stays community-driven (per locked decision), not corpus-driven.
- **Node size** by `mention_count` (brain) or `degree` (query view) — log-scaled, clamped to `[3px, 18px]`.
- **Edge style** — translucent grey (`rgba(180,180,180,0.15)`) by default; cross-cluster edges use the source cluster's color at higher alpha (`0.35`). Edges are visual whisper, color does the structural work.
- **Labels** — render only above zoom threshold OR for top-N nodes by degree (default top 30). Use a label-collision avoidance pass; cheap version is rendering at lower opacity until hovered.
- **Hover** — node + 1-hop neighbors brighten, all others fade to `0.15` opacity.
- **Hub glow** — top-3 nodes by degree get a subtle radial gradient halo. No animation, just static glow.

**Brain view interaction:**
- Pan + zoom (mouse / pinch).
- Click a supernode → in-place re-render. Fetches `POST /api/graph/full` filtered to that cluster's `entity_ids`. Animation: current view zooms into the clicked supernode (~600ms ease-out), then crossfades to the drilled subgraph.
- Breadcrumb at top-left: `Overview › Concept: Generative AI`. Click breadcrumb to pop back.
- LOD already comes free from the backend (`overview` endpoint never returns full graph).

**Query view interaction:**
- One-shot entry animation, ~1200ms total:
  1. **t=0–200ms:** seed entities (`seed_entities` from response) fade in at center, slight scale-up.
  2. **t=200–600ms:** bridge nodes appear, edges draw from seeds outward to bridges (stroke-dashoffset animation).
  3. **t=600–1000ms:** neighborhood nodes settle in via d3-force (`alpha: 1.0 → 0.05`, link strength ramps).
  4. **t=1000–1200ms:** synthesis prose fades in alongside (existing `auto_synthesis.markdown`).
- After settle: static, pan/zoom only. No replay.
- Skip toggle: `settings.graph.skip_animations: bool` (new, default false). If true, render at final state immediately.
- "Re-run query" button preserves the existing query LLM call path — viewer just re-receives the new `context_graph` and runs the entry animation again.

**Animation engine:** `react-spring` for orchestration (timeline + easing), `d3-force` for layout (provided by react-force-graph). Don't use framer-motion — react-spring composes better with imperative graph libraries.

**Component shape:**
```
<GraphViewer
  mode="brain" | "query"
  data={overviewResponse | discoverResponse.context_graph}
  corpusIds={string[]}
  onClusterDrill={(clusterId) => void}    // brain mode only
  loading={boolean}
  cacheWarmingCorpora={string[]}
/>
```
One component, two modes, ~600 LOC including styles.

**What stays untouched (per locked decision):**
- `services/graph/orchestrator.py` LLM synthesis path — `_call_llm_synthesis`, `_build_insight_packet`, `_compact_packet_for_prompt`, `_legacy.discover` scoping. The viewer rewrite does NOT change how queries reach the LLM or how synthesis is composed.
- `routers/graph.py` `/discover` and `/query` endpoint contracts — frontend consumes the same response shapes that exist today (plus the multi-corpus fields added in Items #4/#5).
- The `GraphInsightPacket` builder. The viewer reads `context_graph`, not the packet directly.

**Scope guardrails (the tar-pit avoidance list):**
1. **Two animations total** — brain drill zoom-crossfade, query entry sequence. No idle animations, no on-hover ripples, no edge-pulse-along-path.
2. **No 3D toggle in v1.** Even if react-force-graph-3d is one import away.
3. **No timeline scrubber for query replay.** One-shot, done.
4. **No custom WebGL shaders.** Default canvas rendering is enough for the VOSviewer aesthetic.
5. **No graph editing.** Read-only viewer. Drill-in is the only interaction beyond pan/zoom/hover.
6. **No saved views / bookmarks** in v1. Selection state lives in URL query params (already a Polymath pattern).

**Time estimate:** 4–5 focused days of frontend work after Items #4 + #5 backend lands. Most of that is getting the entry animation to feel right.

**Risks specific to the viewer:**
- **Label readability at scale.** The VOSviewer reference image has labels that overlap heavily. Mitigation: dynamic label visibility based on zoom level + degree threshold. Test with a real 5000-entity overview before declaring done.
- **Color collisions when N communities > palette size.** With 12-color palette and 50+ concept communities, colors repeat. Mitigation: cycle palette but vary lightness by community size (large = saturated, small = muted).
- **Multi-corpus brain view edge bundling.** When 10 corpora overlap on shared entities, edges between communities multiply. May need d3-force-bundle or an edge-aggregation pass on the frontend.

---

## 12. Suggested commit ordering

1. **PR 1 — Pydantic dual-field models + normalize utility + `backend/utils/corpus_ids.py`.** Ships new `corpus_ids: list[str]` fields on `GraphDiscoverRequest/Response/Session/Packet` AND `GraphQueryRequest`, with backward-compat `model_validator(mode="before")`. Adds `source_corpus` to every node/edge subtype. Adds `DISABLE_MULTI_CORPUS` env-var kill switch. No behavior change. Lowest possible risk; unlocks all subsequent work.
2. **PR 2 — Item #5 (Graph viewer multi-corpus).** Additive endpoints:
   - `POST /api/graph/full` (corpus_ids body)
   - `POST /api/graph/overview` (corpus_ids body)
   - `POST /api/graph/cluster/{concept_id}` (single-cluster drill — closes the gap flagged in GRAPH_VIEWER_BRIDGE.md §2.4)
   - `GET /api/corpora/{corpus_id}/cache-status` (lightweight warming poll target for the frontend chip)
   - Bumps `top_entities` cap in overview response from 6 to 50 so the interim drill workaround has enough data
   Legacy GETs untouched. Backend testable independently of frontend.
3. **PR 3 — Item #4 (Mission Control + Agent Query multi-corpus).** Wrapper-fan-out + merger for BOTH `POST /api/graph/discover` AND `POST /api/graph/query` via the same pattern. Includes Mongo `graph_sessions` migration. Largest PR. LLM synthesis path stays untouched per locked decision; multi-corpus only extends the wrapper input/output boundary.
4. **PR 4 — Frontend rewrite (per [GRAPH_VIEWER_BRIDGE.md](../../GRAPH_VIEWER_BRIDGE.md), all phases A-F).** New `<GraphViewer>` component using react-force-graph-2d. Brain + Query modes. Phase F retires the legacy GraphView / DiscoveryPanel / BooksClusterView / RelationGraph and removes sigma.js/graphology dependencies.
5. **(Parallel track — independent of all above) — `_orchestrator_legacy.cpython-311.pyc` recovery.** Reconstruct the sourceless legacy module as tracked Python source. Survivability fix; not gated on multi-corpus work.
6. **(Deferred) Item #6 — Schema Lens refresh.** Revisit only if extraction quality drift becomes a real observed problem on existing corpora. Technical content preserved in [Phased Rollout Plan — Single corpus.txt](../../Phased Rollout Plan — Single corpus.txt) under "Phase A — [DEFERRED]" for future execution.

---

## 13. Files this brief grounds itself in

For verification, every claim in this document is grounded in one of:
- `backend/main.py`, `backend/config.py`
- `backend/routers/graph.py`, `backend/routers/ingestion.py`
- `backend/services/graph/orchestrator.py`, `analytics.py`, `neo4j_reader.py`, `overview.py`
- `backend/services/ingestion/schema_lens.py`
- `backend/services/retriever/mode_b.py`
- `backend/services/storage/qdrant_writer.py`
- `backend/models/schemas.py`
- `backend/migrations/001_per_corpus_qdrant.py`
- `backend/tests/graph/test_orchestrator_payload.py`, `backend/tests/test_schema_lens.py`
- Today's commits 6077dc4 → 55f759b (handoff prep + by-document endpoint).

If something here doesn't match what `git show <commit>` returns, this document is wrong and the code wins.
