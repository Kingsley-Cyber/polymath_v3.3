# Agent Zero Model Provider & Streaming Architecture

Agent Zero uses a **two-layer abstraction** to make every LLM provider work uniformly: **LiteLLM** handles provider-specific API differences, and a **unified chunk parser + thinking tag processor** normalizes streaming output.

---

## 1. How Each Model Provider Is Conducted

### The Provider Resolution Chain

```
User Setting (provider + model name)
    ↓
get_chat_model() in models.py (line 829)
    ↓
_merge_provider_defaults()  ← reads model_providers.yaml + env vars + global kwargs
    ↓
_get_litellm_chat()  ← resolves API key, adjusts args
    ↓
LiteLLMChatWrapper (LangChain SimpleChatModel subclass)
    ↓
litellm.acompletion() or litellm.completion()  ← THE universal API call
```

### Provider Configuration (`conf/model_providers.yaml`)

Each provider entry maps to a **LiteLLM provider name** and optional kwargs:

| Field | Purpose | Example |
|---|---|---|
| `name` | UI display label | `"Anthropic"` |
| `litellm_provider` | LiteLLM routing identifier | `"anthropic"`, `"gemini"`, `"openai"` |
| `kwargs` | Extra params merged into every call | `api_base`, `extra_headers` |
| `models_list` | Model listing endpoint config | URL, format, params |

**Key provider remaps** — the `litellm_provider` field is what LiteLLM actually sees:
- `google` → `gemini` (LiteLLM's name for Google)
- `ollama_cloud`, `venice`, `nebius`, `other` → `openai` (OpenAI-compatible endpoints)
- `azure` → `azure` (Azure OpenAI)
- `bedrock` → `bedrock` (AWS Bedrock)

Plugins can add new providers by dropping a `model_providers.yaml` in their `conf/` directory — `ProviderManager._load_providers()` merges them automatically.

### API Key Resolution (`get_api_key()`, line 202)

Checks three env var patterns in order:
1. `API_KEY_{PROVIDER}` (e.g., `API_KEY_ANTHROPIC`)
2. `{PROVIDER}_API_KEY` (e.g., `ANTHROPIC_API_KEY`)
3. `{PROVIDER}_API_TOKEN`

**Round-robin**: if the value contains commas, Agent Zero rotates through keys sequentially per provider.

### The `_merge_provider_defaults()` Function (line 781)

This is the critical bridge. For every call it:
1. Looks up the provider's config from `model_providers.yaml`
2. Substitutes `litellm_provider` as the actual provider name sent to LiteLLM
3. Merges any provider-level `kwargs` (e.g., `api_base`, `extra_headers`)
4. Injects the API key if still missing
5. Merges `litellm_global_kwargs` from settings (timeouts, etc.)
6. Normalizes string values to ints/floats

### Rate Limiting

Per-provider/per-model rate limiters track requests, input tokens, and output tokens within 60-second windows. Applied before every call via `apply_rate_limiter()`.

### Retry Logic

Transient errors (408, 429, 500, 502, 503, 504, timeouts, connection errors) trigger automatic retry — **only if no chunks were received yet**. Configurable via `a0_retry_attempts` (default 2) and `a0_retry_delay_seconds` (default 1.5s).

---

## 2. How Streaming Works

### The Core Call: `unified_call()` (line 476)

This is the primary entry point used by `agent.py`. The flow:

```
agent.py call_chat_model()
    ↓
unified_call(response_callback, reasoning_callback, ...)
    ↓
litellm.acompletion(model, messages, stream=True)
    ↓  (async generator of chunks)
for each chunk:
    _parse_chunk(chunk)  →  ChatChunk{response_delta, reasoning_delta}
        ↓
    ChatGenerationResult.add_chunk()  →  thinking tag parsing
        ↓
    reasoning_callback(reasoning_delta, full_reasoning)
    response_callback(response_delta, full_response)
```

Streaming is **automatically enabled** when any callback is provided (line 513):
```python
stream = reasoning_callback is not None or response_callback is not None or tokens_callback is not None
```

### Chunk Parsing: `_parse_chunk()` (line 739)

Extracts two deltas from every LiteLLM chunk:

```python
response_delta  = delta.get("content", "")          # main text output
reasoning_delta = delta.get("reasoning_content", "")  # native reasoning/thinking
```

This works because **LiteLLM normalizes** the `reasoning_content` field across providers:
- **OpenAI o-series**: native `reasoning` field → LiteLLM maps to `reasoning_content`
- **DeepSeek R1**: native `reasoning_content` field → passed through
- **Anthropic extended thinking**: LiteLLM maps to `reasoning_content`
- **Other models**: no `reasoning_content` → empty string

### Thinking Tag Fallback: `ChatGenerationResult` (line 95)

For models that output thinking in **XML-like tags** instead of native reasoning fields, a state-machine parser extracts it:

```python
thinking_pairs = [(" Kuala Lumpur", "</thinking>"), ("<reasoning>", "</reasoning>")]
```

The parser:
1. Detects if `reasoning_content` was ever present → sets `native_reasoning = True`
2. If native reasoning: trusts the two-delta split, no tag parsing needed
3. If no native reasoning: scans `response_delta` for opening/closing thinking tags, routing tagged content to `reasoning_delta`
4. Handles partial tags (incomplete chunks at chunk boundaries) by buffering in `unprocessed`

**This dual approach is what makes thinking streaming work regardless of model** — native `reasoning_content` is preferred, with tag-based fallback for models like DeepSeek V3 or local models that embed thinking in the response text.

### Agent-Level Callbacks (`agent.py`, lines 410-475)

Two parallel async callbacks wire model output to the rest of the system:

| Callback | Trigger | Routes to |
|---|---|---|
| `reasoning_callback(chunk, full)` | Every reasoning delta | `reasoning_stream` extension hooks → WebSocket `reasoning_stream` namespace → frontend thinking display |
| `stream_callback(chunk, full)` | Every response delta | `response_stream` extension hooks → JSON early-detection → WebSocket `response_stream` namespace → frontend response display |

The `stream_callback` also performs **early JSON detection** (line 438-453) — it uses `extract_tools.extract_json_root_string()` to find complete JSON tool calls in the partial stream, validates them, and can short-circuit the stream to avoid generating unnecessary tokens.

### Non-Streaming Fallback

If no callbacks are provided, `unified_call()` calls `litellm.acompletion()` without `stream=True`, gets the full response at once, parses it with `_parse_chunk()`, and returns the complete `(response, reasoning)` tuple.

---

## Summary: Why It Works Universally

| Challenge | Agent Zero's Solution |
|---|---|
| Different API formats | **LiteLLM** normalizes all providers to OpenAI-compatible interface |
| Different auth patterns | `get_api_key()` tries 3 env var patterns + round-robin |
| Different provider endpoints | `model_providers.yaml` maps `litellm_provider` + `kwargs.api_base` |
| Different reasoning formats | Dual approach: native `reasoning_content` + XML tag fallback parser |
| Chunk boundary issues | `ChatGenerationResult` buffers partial tags in `unprocessed` |
| Rate limits | Per-provider/model token counting with async waiting |
| Transient failures | Automatic retry with configurable attempts/delay |
| Early tool call detection | `stream_callback` detects complete JSON mid-stream and short-circuits |

---

## Key Source Files

- `/a0/models.py` — provider abstraction, chunk parsing, thinking tag state machine
- `/a0/agent.py` (lines 410-475, 790-831) — streaming callbacks and `call_chat_model`
- `/a0/helpers/providers.py` — `ProviderManager`, YAML loading, plugin merging
- `/a0/conf/model_providers.yaml` — provider definitions with LiteLLM mappings
