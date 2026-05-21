# Polymath: RAG-First + Deterministic Web Search Architecture

## Design Principles

1. **RAG first, always** — local corpus retrieval runs on every query, unconditionally
2. **Web search is a deterministic toggle** — the LLM does _not_ decide whether to search
3. **Zero utility model calls** — no secondary LLM for query rewriting, optimization, or fallback
4. **One model, one loop** — the chat model executes tools; the toggle decides which tools run

---

## Pipeline Diagram

```
User Query
    │
    ▼
┌───────────────────────────────────────────┐
│           1. QUERY PREPROCESSOR           │
│                                           │
│  (a) Normalize & deduplicate query         │
│  (b) Optional: extract named entities     │
│      for keyword generation               │
│  (c) Output: clean_query, entities[]      │
└──────────────────┬────────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────────┐
│          2. RAG RETRIEVAL                 │
│                                           │
│  Embed query → FAISS search (threshold: 0.5)│
│  Return top_k chunks (k=10 default)       │
│  Always runs — no skip condition          │
│                                           │
│  Output: retrieved_chunks[] + scores[]    │
└──────────────────┬────────────────────────┘
                   │
                   ▼
┌───────────────────────────────────────────┐
│     3. WEB SEARCH TOGGLE CHECK            │
│                                           │
│  Check env/config/runtime flag:           │
│    SEARCH_MODE = "rag_only"               │
│                 | "rag_then_web"          │
│                 | "web_only"              │
│                                           │
│  Or use a function-level flag:            │
│    search_web=True/False per call         │
│                                           │
│  Determined by config + task type,        │
│  NOT by LLM decision.                     │
└──────┬──────────────┬─────────────────────┘
       │              │
       │ (off)        │ (on)
       ▼              ▼
┌──────────────┐   ┌──────────────────────────────────┐
│ SKIP WEB     │   │   4. WEB SEARCH EXECUTION        │
│ SEARCH       │   │                                  │
│              │   │  (a) Query builder:              │
│ Go to step 5 │   │    - Take user query             │
│              │   │    - Optionally inject top-2 RAG │
│              │   │      chunk titles as context      │
│              │   │    - Strip filler words           │
│              │   │    - Extract 3-8 high-signal      │
│              │   │      keywords                     │
│              │   │                                  │
│              │   │  (b) Call search_engine tool     │
│              │   │    → SearXNG meta-search         │
│              │   │    → Top 10 results              │
│              │   │                                  │
│              │   │  (c) Optional: fetch top N pages │
│              │   │    via Obscura (CDP/headless)    │
│              │   │                                  │
│              │   │  Output: web_snippets[]          │
│              │   │          web_pages[] (optional)  │
└──────────────┘   └───────────┬──────────────────────┘
       │                       │
       └───────────┬───────────┘
                   ▼
┌───────────────────────────────────────────┐
│     5. FINAL SYNTHESIS (Chat Model)       │
│                                           │
│  Inputs:                                  │
│    - Original user query                  │
│    - retrieved_chunks[] (from step 2)     │
│    - web_snippets[] + pages (from step 4, │
│      if toggle was on)                   │
│                                           │
│  The chat model synthesizes an answer     │
│  using all available context.             │
│                                           │
│  Output: final response via response()   │
└───────────────────────────────────────────┘
```

---

## Component Specifications

### 1. Query Preprocessor (Rule-Based, No LLM)

```python
def preprocess_query(raw_query: str, rag_chunk_titles: list[str] | None = None) -> dict:
    """
    Normalize and prepare query without any LLM call.
    Returns: {
        "clean_query": str,
        "keywords": list[str],
        "rag_context_terms": list[str],
        "search_query": str  # ready for SearXNG
    }
    """
```

Rules:
- Lowercase, strip punctuation
- Remove common filler words: "explain", "how do I", "can you tell me", "what is", "find information about"
- Extract named entities (detect by capitalization and domain dictionaries)
- If RAG context titles are available, append 2-3 distinguishing terms
- Output `search_query`: 3-8 high-signal keywords, space-separated

No Regex NLP dependencies — simple string ops + domain-specific word lists.

### 2. RAG Retrieval (FAISS)

```python
def retrieve_chunks(query: str, k: int = 10, threshold: float = 0.5) -> list[dict]:
    """
    Always runs. Returns top-k chunks from the local corpus.
    Always injects results into the model's context window
    regardless of relevance score.

    Returns: [{"title": str, "content": str, "score": float, "source": str}, ...]
    """
```

- Uses existing DocumentQueryStore (FAISS with chunk_size=1000, overlap=100)
- threshold=0.5 is the minimum similarity filter
- Even if all scores are low, results are still passed to the model (model can say "my knowledge base doesn't contain this")
- Chunks are injected into system prompt as context before the model runs

### 3. Web Search Toggle (Deterministic)

No LLM decision. Three modes:

| Mode | Behavior | Use Case |
|------|----------|----------|
| `rag_only` | RAG only, no web search | Offline / private data / sensitive queries |
| `rag_then_web` | RAG → web search always | General knowledge, current events |
| `web_only` | Skip RAG, web search only | Live queries, recent events |

Determined by:
1. **Environment variable**: `POLYMATH_SEARCH_MODE=rag_then_web`
2. **Config file**: `settings.yaml` → `search_mode: rag_then_web`
3. **Call-site flag**: Passed as `search_web=True` in the task message
4. **System prompt hint**: Optional per-request directive (not an LLM decision)

### 4. Web Search Query Builder (Rule-Based)

```python
def build_search_query(user_query: str, rag_chunks: list[dict]) -> str:
    """
    Build a keyword query for SearXNG from:
      - user_query (stripped of filler words)
      - top-2 RAG chunk titles (added as context terms)

    No LLM involvement. Returns ready-to-use query string.
    """
    filler_words = [...]  # domain-specific list
    keywords = [w for w in user_query.split() if w.lower() not in filler_words]

    # If RAG chunks exist, add the most informative terms from their titles
    if rag_chunks:
        rag_context = " ".join(
            extract_key_terms(chunk["title"]) for chunk in rag_chunks[:2]
        )
        keywords.extend(rag_context.split())

    # Deduplicate and limit to 8 terms
    keywords = list(dict.fromkeys(keywords))[:8]
    return " ".join(keywords)
```

Then calls the existing `search_engine` tool (SearXNG, 10-result limit).

### 5. Page Fetch (Optional, Configurable)

If `FETCH_FULL_PAGES=true`:
1. Take top 3-5 URLs from SearXNG results
2. Fetch full content via **Obscura** (Rust headless browser via CDP)
3. Convert to markdown text
4. Append to model context as web_pages[]

### 6. Final Synthesis (Chat Model Only)

The chat model receives in a single prompt turn:

```
System:
  You have access to the following retrieved knowledge:
  [RAG chunk 1]
  [RAG chunk 2]
  ...
  [Optional: Search results from the web:]
  [Web snippet 1]
  [Web snippet 2]
  ...
  [Optional: Full page from: https://...]
  [Page content]

User:
  <original_query>
```

The model synthesizes the answer from this context. No tool calls needed — the context is already loaded.

---

## Where the Utility Model Used to Plug In (NOW REMOVED)

```diff
-  Old path (removed):
-  1. RAG → chunks retrieved
-  2. Utility model called to rewrite query:
-     prompt: fw.document_query.optmimize_query.md
-     ⨯ 4s timeout, failure → verbatim fallback
-  3. Web search runs with (possibly bad) rewritten query
-  4. Utility model scores reranked results

+  New path (current):
+  1. RAG → chunks retrieved
+  2. Rule-based query builder produces keywords
+  3. Web search runs with clean keyword query (if toggle is on)
+  4. No scoring model needed (SearXNG ranking used)
+  5. Single chat model synthesizes from all context
```

---

## Implementation Sketch

### Core execution flow (pseudocode)

```python
async def handle_query(user_query: str, *, search_mode: str = "rag_then_web") -> str:
    # Step 1: Preprocess
    clean = preprocess_query(user_query)

    # Step 2: RAG always
    rag_chunks = await retrieve_chunks(clean["clean_query"], k=10)

    # Step 3: Check toggle
    web_context = ""
    if search_mode in ("rag_then_web", "web_only"):
        if search_mode == "web_only":
            rag_chunks = []

        # Step 4: Build search query (rule-based)
        search_query = build_search_query(clean["clean_query"], rag_chunks)

        # Step 4b: Execute web search
        web_results = await search_engine.execute(search_query)

        # Step 4c: Optionally fetch full pages
        if FETCH_FULL_PAGES and web_results:
            top_urls = extract_top_urls(web_results, n=3)
            web_pages = await parallel_fetch(top_urls, engine="obscura")
            web_context = format_web_context(web_results, web_pages)
        else:
            web_context = format_snippets(web_results)

    # Step 5: Build final prompt with all context
    final_context = format_prompt(
        user_query=user_query,
        rag_chunks=rag_chunks,
        web_context=web_context,
    )

    # Step 6: Single chat model call for synthesis
    response = await chat_model(full_context)
    return response
```

### Configuration

```yaml
# settings.yaml
search:
  mode: rag_then_web  # rag_only | rag_then_web | web_only
  fetch_full_pages: true
  max_fetched_pages: 3
  rag_k: 10
  rag_threshold: 0.5
  use_obscura: true  # use Obscura for JS pages, else SearXNG-only
```

---

## Summary: What Changed

| Aspect | Before (Broken) | After (Clean) |
|--------|----------------|---------------|
| **Query rewriting** | Utility/GLM model (timed out) | Rule-based keyword extractor |
| **Web search trigger** | LLM decides (or mandatory in pipeline) | Deterministic toggle in config/env |
| **Utility model calls** | Required for web query rewrite | Zero — fully removed from pipeline |
| **RAG coupling** | RAG chunks fed into utility prompt (bloat+timeout) | RAG chunks injected into final synthesis prompt only |
| **Fallback on failure** | Verbatim original query passed through | Query builder always produces clean output |
| **Search query quality** | "explain the RemoteEvent validation patterns in science marketing" | "RemoteEvent validation patterns Roblox Luau security" |
| **Fetch decisions** | Utility model scored/reranked | Config-driven (max N pages, Obscura on/off) |
| **Model count in pipeline** | 3+ (chat + utility + embedder) | 2 (chat + embedder)** |

**The embedding model is only used for RAG vector search, not for any generative task.

---

## File Structure

```
polymath/
├── search/
│   ├── __init__.py
│   ├── query_preprocessor.py    # Rule-based normalization + keyword extraction
│   ├── query_builder.py         # Build SearXNG query from user input + RAG context
│   ├── web_search_toggle.py     # Read config/env to determine search mode
│   └── page_fetcher.py          # Obscura-based full page fetcher
├── rag/
│   ├── __init__.py
│   ├── retriever.py             # FAISS retrieval (wraps existing DocumentQueryStore)
│   └── corpus_manager.py        # Index management
├── synthesis/
│   ├── __init__.py
│   └── synthesizer.py           # Format context + call chat model
├── config/
│   └── settings.yaml            # search.mode, fetch settings, etc.
└── main.py                      # handle_query() entry point
```
