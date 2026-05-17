# Web Toggle Query Pipeline E2E Checklist - 2026-05-17

Source plan: `A0_2/POLYMATH_A0_IMPLEMENTATION_PLAN.md`

Scope: validate the Web-on query pipeline as one linear run:
UI selector state -> backend model routing -> HyDE/RAG retrieval -> Utility query enrichment -> native `web_search` -> SearXNG/cache/fetch/source metadata -> final answer.

## Test Setup

- Frontend: `http://localhost:3000`
- Backend: `http://localhost:8000`
- Backend image rebuilt and restarted after implementation.
- Local stack healthy: backend, frontend, LiteLLM, MongoDB, Qdrant, Redis, SearXNG.
- Chat dropdown model visible in UI: `deepseek-chat`
- Web/tool auto-route visible in UI: `AUTO: deepseek-v4-flash`
- Query profile: `Thorough`
- Search mode: `auto`
- Reasoning UI: `Meta`
- Utility model used by live run: `openai/glm-5-turbo`
- SearXNG YAML in use: repo-owned `searxng-settings.yml`, mounted read-only to `/etc/searxng/settings.yml`
- SearXNG backend env: `SEARXNG_URL=http://searxng:8080`, `SEARXNG_ENGINES=google,bing,duckduckgo,brave`, `SEARXNG_TIMEOUT_SECONDS=6.0`
- Obscura runtime env: `OBSCURA_COMMAND=""`; live rendering is therefore not configured for this local stack.
- Test queries:
  - Primary: `With Web enabled, run one live web search for Roblox RemoteEvent security server client validation. Use local RAG context plus the web results, then answer in one concise sentence.`
  - Current-image telemetry trigger: `With Web enabled, search for Roblox RemoteEvent security server validation. Use RAG and web. Answer one sentence.`
- Local screenshot artifacts were captured under `docs/e2e-artifacts/` and intentionally left local-only.

## Linear Checklist

| Step | Plan requirement | Evidence | Result |
| --- | --- | --- | --- |
| 1 | Web toggle exposes native web tool path only when enabled. | UI showed `Web` enabled and `AUTO: deepseek-v4-flash`. Backend logged `Skills active: ['Live Web Search']`. | Works |
| 2 | Web-enabled turn routes to tool-capable/agentic model. | Backend logged `kind=agentic -> deepseek/deepseek-v4-flash` while UI dropdown still showed `deepseek-chat`. | Works |
| 3 | HyDE/RAG prepass happens before web answer. | Backend logged `HyDE active ... duration=1.55s` and `Retrieval timings status=ok_hydrated total=0.59s ... final=8`. | Works |
| 4 | Utility/GLM performs bounded web-query enrichment. | Backend logged `utility_web_query_enrichment attempted=True applied=False model=openai/glm-5-turbo ... duration_ms=2267`. The base query was already clean, so Utility preserved it. | Works |
| 5 | Web query is clean and bounded. | Backend final query: `Roblox RemoteEvent security server validation`; prior exact run: `Roblox RemoteEvent security server client validation`. No `RAG` pollution reached SearXNG. | Works |
| 6 | Native streamed tool-call path executes `web_search`. | UI displayed `WEB SEARCH`; backend executed one `web_search pipeline` entry per turn. | Works |
| 7 | Final web source cap is 7. | First exact run source panel: `7 sources · 2 full page · 4 snippet · 0 rendered · 0 cached`. Backend cap and schema now allow 7. | Works |
| 8 | Source metadata is visible and distinct from corpus chunks. | Source panel showed `web` rows with URLs, snippet/full-page status, fetch status, rendered count, and cache status. | Works |
| 9 | Repeat query uses backend web cache. | Repeat source panel: `5 sources · 0 full page · 5 snippet · 0 rendered · 5 cached`. Current backend log: `redis_search_cache_hit=True redis_page_cache_hit=True`. | Works |
| 10 | Obscura is optional and policy-gated. | Live UI showed `0 rendered`; compose config has `OBSCURA_COMMAND=""`. Unit tests cover static-before-Obscura, allowlist gating, command validation, and failure metadata. | Works for policy; live renderer not enabled |
| 11 | Final message metadata reflects route. | Final badges included `RAG · ... · Hybrid · Thorough · Meta · HyDE · Web · Agentic`. | Works |
| 12 | Time is observable and bounded enough to debug. | Current run: Utility `2267ms`, RAG `0.59s`, total chat `17.70s`, TTFT `1.62s`, stream `5.12s`. | Partially works; acceptable but still not fast |

## Works Fully Intended

- Web-on routing used the agentic/tool-capable model and did not use the base dropdown model for the tool turn.
- Native `tool_calls` remained the primary web path; no raw JSON fallback was added.
- Utility/GLM now runs as a bounded query-enrichment helper and falls back deterministically if unavailable, unsafe, or low-overlap.
- Web final sources are capped at 7 across config, compose default, tool schema, and persisted web previews.
- SearXNG uses the current Polymath-owned YAML mount; no Agent Zero config was copied wholesale.
- Cache behavior is visible end-to-end: backend telemetry and UI source rows both showed cached results on repeat.
- Obscura policy behavior is tested in backend unit tests and remains disabled in the live stack unless `OBSCURA_COMMAND` is configured.

## Partially Works

- Utility enrichment attempted successfully, but `applied=False` in the live run because GLM kept the already-good query unchanged. This is acceptable, and telemetry now separates `attempted` from `applied`.
- Latency is clear but not ideal: full Web-on RAG turns were about 17 seconds in the live stack, with Utility adding roughly 2-3.3 seconds.
- Source quality is much better than the earlier polluted run, but SearXNG can still return mixed sources; the reranker selected relevant Roblox docs/devforum/reddit sources in this run.
- The Browser automation harness produced local Statsig and clipboard warnings. These came from the browser automation layer, not Polymath.

## Does Not Work / Not Enabled

- Live Obscura rendering was not exercised because `OBSCURA_COMMAND` is empty. The tested state is policy/fallback correctness, not a real renderer invocation.

## Backend Test Results

Focused suite:

```text
57 passed, 5 warnings in 3.09s
```

Warnings:

- Pydantic protected-namespace warnings for existing `model_*` fields.
- Docker compose orphan-container warning for older `vllm-*` containers.
- Browser automation Statsig/clipboard warnings during local UI typing.
