# Deep Research + RAG Toggle Implementation Handoff

> Status: design ratified; implementation not started.
> Created: 2026-07-11.
> Repository: `/Users/king/polymath_v3.3`.
> Intended branch at creation: `codex/ingestion-contract-checkpoint`.
> This document is the continuity source of truth for this feature. Re-check line numbers before editing.

---

## 0. Mission (BLUF)

Add two chat features without re-ingesting documents:

1. A session-scoped **RAG toggle**. When disabled, chat must use conversation history and the selected
   model without issuing any new corpus retrieval, HyDE, query planning, reranking, graph expansion,
   coverage repair, or answerability calls.
2. A one-shot **Deep Research toggle**. The next user message must launch a durable, corpus-scoped research
   job over exactly the corpora currently selected by the user. The result appears in chat as an artifact
   that can be opened or downloaded. Implement sanitized HTML first; add PDF from the same canonical
   Markdown later.

The implementation must reuse Polymath's existing retrieval planner, durable job leases, conversation
persistence, model/provider abstraction, and frontend message model. Do not import a second RAG stack or
an agent framework.

Working architecture:

```text
queued -> planning -> retrieving -> synthesizing
       -> coverage_check -> writing -> rendering -> complete

terminal alternatives: partial | failed | cancelled
```

Every stage must be bounded, observable, restart-safe, and idempotent.

---

## 1. Locked Product Decisions

- Deep Research scope is the immutable snapshot of the user's current corpus selector.
- The existing selector contract is `selectedCorpusIds=[]` meaning **All corpora**. At submission time the
  frontend must expand that sentinel to the explicit list returned by `/api/corpora`; the backend must
  receive and persist a nonempty explicit list. Never persist an ambiguous empty/all sentinel in a job.
- If the All-corpora sentinel cannot be resolved because corpora are still loading or none are accessible,
  block submission with a clear error. Never fall back to web, MCP, or an unscoped global search.
- Multiple selected corpora are allowed; every retrieval and citation must retain its `corpus_id`.
- Deep Research is one-shot: arming it affects the next submitted message and then disarms.
- RAG enablement is session/conversation scoped and remains in its chosen state until changed.
- RAG-off does not erase previously retrieved answers from chat history. It only prevents new retrieval.
- Output selection is deterministic and singular: `output_format` is one `Literal`, never a list. The
  popup only enables formats advertised by `/api/research/capabilities`.
- The initial required artifact format is HTML generated from canonical Markdown. Direct LLM-authored HTML
  is forbidden. PDF can be exposed only after its renderer passes the backend capability self-test.
- HTML artifacts must be sanitized and served from an authenticated endpoint with a restrictive CSP.
- PDF is a later renderer over the same stored Markdown, not a separate research workflow.
- Existing ingestions and indexes are sufficient. This feature does not require re-ingestion.
- Deep Research must not use HyDE.
- Only one active research job is allowed per conversation. A second request returns the existing active
  job instead of starting competing work.
- Deep Research always retrieves its captured corpus scope even when ordinary chat RAG is disabled. It
  must not mutate the conversation's RAG preference.
- No framework import from LangGraph, GPT Researcher, or the reviewed repositories.
- No API keys, provider credentials, or plaintext secrets may be written into jobs, artifacts, logs,
  prompts, source files, or this document.

---

## 2. GitHub Research Conclusions

Patterns to adopt:

- Explicit phases: plan, parallel research, evidence compression, coverage validation, final writing.
- Structured planner output instead of free-form task spawning.
- Hard limits on breadth, depth, iterations, tool calls, tokens, and elapsed time.
- Bounded parallel retrieval with per-facet error isolation.
- Separate raw evidence from compressed learnings and generated prose.
- Persistent progress fields and terminal job states.
- Deduplication of evidence and learnings before final synthesis.
- One targeted repair round for unsupported sections.
- Canonical Markdown before HTML/PDF rendering.
- Research job state separate from chat-message state.

Patterns to reject for Polymath v1:

- Open-ended recursive agent loops.
- Supervisor/researcher agent hierarchies.
- A new LangGraph runtime or a separate research service.
- Calling Fast, Hybrid, and Graph retrieval independently for every facet.
- A clarification LLM call before every job.
- An LLM planner when deterministic `QueryPlanV2` already yields adequate facets.
- A second streaming transport solely for research jobs.
- Filesystem-only artifacts that disappear after container recreation.
- Web-search or MCP fallback outside the selected corpus set.

Primary references:

- Open Deep Research orchestration:
  <https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py>
- Open Deep Research limits/configuration:
  <https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py>
- Minimal bounded recursion/concurrency example:
  <https://github.com/dzhng/deep-research/blob/main/src/deep-research.ts>
- Planner/execution/publisher architecture:
  <https://github.com/assafelovic/gpt-researcher>
- Artifact/history/SSE product patterns:
  <https://github.com/u14app/deep-research>
- Background job registry separated from messages:
  <https://github.com/langchain-ai/async-deep-agents>

These sources are architectural references only. Do not copy substantial source code.

---

## 3. Verified Existing Polymath Anchors

### Backend

- `backend/routers/chat.py`: authenticated `POST /api/chat`, existing SSE `StreamingResponse`.
- `backend/models/schemas.py`: live `ChatRequest` and `ChatMessage` extensions. Add fields here, not to a
  legacy duplicate model.
- `backend/services/chat_orchestrator.py`: current model/conversation resolution, HyDE, retrieval,
  persistence, and done-event flow. It is large; add a small early no-retrieval branch/helper instead of
  threading `if retrieval_enabled` through the whole pipeline.
- `backend/services/retriever/query_plan.py`: `QueryPlanV2`, phrase-aware deterministic facets,
  `corpus_ids`, evidence sides, and repair budget.
- `backend/services/retriever/__init__.py`: `retrieve_planned()` and the existing planned fanout/repair
  path. Graph tier already combines multiple retrieval signals and graph expansion.
- `backend/services/ingestion/job_leases.py`: expired-lease reclamation and atomic runnable-job claims.
  Reuse its generic patterns for `research_jobs`.
- `backend/services/conversation.py`: message persistence supports arbitrary metadata. A research message
  can point at a job and artifact without adding a separate message table.
- `backend/main.py`: existing async polling/background-task patterns. The HTML-first research worker can
  run in the backend process; do not add a new container for v1.

### Frontend

- `frontend/src/components/chat/ChatInput.tsx`: the requested **Features popup already exists** at
  `data-testid="composer-features-toggle"`. It opens above the composer and currently contains
  `ToggleBar`. Extend this popup; do not add a second modal or toolbar.
- `frontend/src/components/chat/ToggleBar.tsx`: existing inline HyDE, Reason, and Web switches plus the
  activator selector. Keep these as query enhancements below the new core-mode section.
- `frontend/src/App.tsx`: `handleSend` is the real submission router and current `ChatRequest` builder.
  It already expands `settings.selectedCorpusIds=[]` into all loaded corpora. Extract that logic into a
  shared `resolveSubmissionCorpusScope()` helper and use the same explicit snapshot for chat and research.
- `frontend/src/components/chat/MessageBubble.tsx`: render a research-progress/artifact card from message
  metadata.
- `frontend/src/stores/settingsStore.ts`: authoritative corpus selector for chat (`selectedCorpusIds`) and
  the persisted global query controls. Do not add research-arm state here because it is one-shot.
- `frontend/src/stores/chatStore.ts`: currently has a second corpus selection used by Corpus Manager; it is
  not the chat submission authority. Add update-by-`research_job_id` for cards, but do not read its corpus
  list when creating research jobs.
- `frontend/src/lib/api.ts`: existing API/SSE handling. Use a normal create request plus bounded status
  polling for research v1.
- `frontend/src/components/chat/ChatContextMenu.tsx`: the Sources panel is the existing selector UX and
  labels `[]` as All corpora. The Features popup should show a read-only scope summary and direct users to
  Sources for changes instead of duplicating corpus checkboxes.

### Naming Collision

The repository already uses `deep_research` as a reasoning/prompt mode in backend reasoning code and
frontend settings. That is not this durable feature. Rename the old UI label or use unambiguous new names:

- `research_job_enabled` for the one-shot product toggle/request behavior.
- `research_jobs` for durable storage.
- `research_mode` only inside the research job contract if needed.

Do not silently overload the existing prompt-mode meaning.

### Current-State Risks the Implementation Must Correct

- `ChatInput.onSend` currently returns `void` and clears text/attachments immediately. Change it to an
  async acceptance contract so one-shot research disarms and clears only after the backend accepts the
  job. On failure, preserve the prompt and armed state for retry.
- Backend `ChatMessage` does not expose a durable message ID. Research message reconciliation must use the
  unique `metadata.research_job_id`, not frontend-only `crypto.randomUUID()` values.
- Conversation CRUD is authenticated but the current service queries are not consistently filtered by
  `user_id`. New research collections and endpoints must enforce `user_id` ownership directly. Do not
  inherit the existing weakness.
- `retrieve_planned()` already executes query-plan lanes as a combined candidate/fusion/rerank pass.
  Deep Research must reuse that behavior instead of running Fast, Hybrid, and Graph as redundant full
  passes for each facet.
- `backend/requirements.txt` currently lacks a Markdown renderer and HTML sanitizer. Add pinned Markdown
  and Bleach dependencies for HTML. Do not advertise PDF until its renderer and system dependencies are
  present and healthy in the actual backend image.

---

## 3.1 Composer Feature and Submission Contract

### State Ownership

Use explicit state with different lifetimes:

```ts
type ResearchOutputFormat = "html" | "pdf";
type ResearchEffort = "focused" | "standard" | "extended";

interface ComposerFeatureState {
  // Conversation-scoped. Default true for a new conversation.
  retrievalEnabled: boolean;
  // Component-local and one-shot. Never persisted as a global setting.
  researchArmed: boolean;
  // Retained in the composer as the user's last selection.
  researchOutputFormat: ResearchOutputFormat;
  researchEffort: ResearchEffort;
}

interface ComposerSubmission {
  mode: "chat" | "research";
  retrievalEnabled: boolean;
  corpusScope: {
    selectorMode: "selected" | "all"; // UI receipt only
    corpusIds: string[];               // always explicit and nonempty
    corpusNames: string[];
  };
  research?: {
    outputFormat: ResearchOutputFormat; // exactly one
    effort: ResearchEffort;
  };
}

interface SubmissionResult {
  accepted: boolean;
  conversationId?: string;
  researchJobId?: string;
  error?: string;
}
```

Implementation ownership:

- Add `retrievalEnabledByConversation: Record<string, boolean>` and setters to `chatStore`; use
  `"__new__"` before a conversation exists. When messages load, initialize the map from the latest
  `metadata.retrieval_enabled`, defaulting to true.
- `ChatInput` reads/writes the active conversation's RAG value through `chatStore`.
- Keep `researchArmed`, `researchOutputFormat`, and `researchEffort` in `ChatInput` (or a composer hook),
  not persisted global settings. Disarm research when the active conversation changes so an armed job
  cannot accidentally move to another conversation.
- Capability data may live in a small cached hook/store because it is server state, not a user preference.

`ChatInputProps.onSend` becomes:

```ts
onSend: (
  message: string,
  attachments: File[] | undefined,
  submission: ComposerSubmission,
) => Promise<SubmissionResult>;
```

`ChatInput.handleSubmit()` rules:

1. Build a complete feature snapshot before any asynchronous work.
2. Await `onSend`.
3. Clear input and attachments only when `accepted=true`.
4. Disarm Deep Research only when its job was accepted or an existing idempotent job was returned.
5. Keep prompt, format, effort, and armed state when validation/network creation fails.

### Authoritative Corpus Snapshot

Extract this behavior from `App.handleSend`:

```ts
function resolveSubmissionCorpusScope(
  selectedCorpusIds: string[],
  loadedCorpora: CorpusResponse[],
): ResolvedCorpusScope {
  const selectorMode = selectedCorpusIds.length > 0 ? "selected" : "all";
  const requested = selectorMode === "selected"
    ? selectedCorpusIds
    : loadedCorpora.map((corpus) => corpus.corpus_id);
  const corpusIds = [...new Set(requested.filter(Boolean))];
  // map names from loadedCorpora; reject when corpusIds is empty
  return { selectorMode, corpusIds, corpusNames };
}
```

- The popup scope receipt and the submitted payload must use the same resolved object.
- If `/api/corpora` has not loaded, submission waits for one explicit refresh or returns a visible error.
- Canonically sort IDs for idempotency hashing, but retain display order/names separately.
- The backend re-resolves ownership and names; frontend names are presentation hints, not authority.
- A corpus removed between popup display and submit causes a 409 scope error. Never silently drop it.

### Submission Router

`App.handleSend` must branch once:

```text
submission.mode == chat
  -> POST /api/chat SSE with retrieval_enabled

submission.mode == research
  -> POST /api/research/jobs JSON
  -> add/update durable research card
  -> return immediately while backend worker runs
```

Do not encode Deep Research as `ChatRequest.overrides.reasoning_mode="deep_research"`. That existing value
is only a prompt style and cannot provide durable execution, corpus snapshots, artifacts, cancellation, or
restart recovery.

---

## 4. RAG Toggle Contract

Extend the live `ChatRequest`:

```python
retrieval_enabled: bool = True
```

When `retrieval_enabled` is false:

1. Resolve authentication, conversation, selected model, tools/attachments policy, and chat history.
2. Persist the user message with `retrieval_enabled=false` metadata.
3. Call the selected chat model using history only.
4. Stream the response through the existing SSE event contract.
5. Persist the assistant response with:
   - `strategy_used: "no_rag"`
   - `retrieval_enabled: false`
   - empty sources and retrieval trace
6. Emit the normal completion event.

The branch must occur before:

- HyDE
- query decomposition
- embed/search
- reranking
- hydration
- graph expansion
- facet/evidence repair
- coverage and answerability gates

Testing must prove those functions were not called, rather than merely returning no sources.

Optional later enhancement: include a compact, persisted evidence capsule from previously displayed
sources. It must not issue a new retrieval. This is not required for v1.

Frontend/backend state receipt:

- Default new conversations to `retrievalEnabled=true`.
- Include `retrieval_enabled` explicitly on every chat request; never rely on a backend default after the
  UI switch exists.
- Persist `retrieval_enabled` into both user and assistant message metadata.
- On conversation reload/switch, derive the current toggle from the most recent message carrying that
  field. If no message carries it, default to true.
- RAG off disables HyDE in the Features popup because HyDE has no work without corpus retrieval. Do not
  erase the user's HyDE preference; render it disabled and restore it when RAG is re-enabled.
- Web remains an independent explicit feature because the user's requirement is to stop repeatedly
  querying the corpus. The popup must state that Web can still fetch external context. If the desired
  product policy later becomes "no retrieval of any kind," add a separate Offline/No-tools mode rather
  than silently redefining RAG.
- Deep Research ignores ordinary chat `retrieval_enabled` for its one job and shows that fact in the popup.
- The RetrievalBadge must render a deterministic `NO RAG`/`MODEL ONLY` state from
  `strategy_used="no_rag"`, `chunks_returned=0`, and `collections_queried=[]`.

---

## 5. Durable Research Job Contract

Create Mongo collection `research_jobs` with a unique `job_id` index and useful status/lease indexes.

Minimum document shape:

```jsonc
{
  "job_id": "uuid",
  "conversation_id": "uuid",
  "user_id": "owner",
  "client_request_id": "uuid",
  "active_key": "user_id:conversation_id",
  "query": "original user query",
  "corpus_ids": ["immutable", "snapshot"],
  "corpus_snapshot": [
    {
      "corpus_id": "...",
      "name": "...",
      "truth_updated_at": "utc-or-null",
      "queryable_docs": 0,
      "eligible_docs": 0,
      "readiness_state": "..."
    }
  ],
  "scope_warnings": [],
  "model_snapshot": {
    "model_ref": "pool:entry-id",
    "display_model": "public model name",
    "entry_id": "public/non-secret entry id or null",
    "config_version": "optional non-secret version"
  },
  "output_format": "html",
  "effort": "standard",
  "retrieval_tier_requested": "qdrant_mongo_graph",
  "retrieval_tiers_effective": [],
  "status": "queued",
  "stage": "queued",
  "progress": {
    "completed_units": 0,
    "total_units": 0,
    "current_facet": null,
    "message": "Queued"
  },
  "budgets": {
    "max_facets": 4,
    "max_depth": 2,
    "max_repair_rounds": 1,
    "max_elapsed_seconds": 300,
    "max_evidence_tokens": 30000
  },
  "plan": null,
  "evidence": [],
  "learnings": [],
  "coverage": null,
  "artifact": null,
  "error": null,
  "lease_owner": null,
  "lease_expires_at": null,
  "attempt_count": 0,
  "created_at": "utc",
  "updated_at": "utc",
  "completed_at": null
}
```

Never store provider keys, base URLs containing secrets, or decrypted extras in `model_snapshot`. Snapshot
the selected `pool:<entry_id>`/`profile:<entry_id>` reference and resolve credentials at execution time via
`services.query_model_resolver.resolve_by_entry_id()`. If a configured fallback is used, record its public
entry/model identity in stage telemetry; never switch silently.

Effort presets are server-owned contracts, not LLM suggestions. The timing values below are the
**recommended SLA pending owner ratification**:

| Effort | Facets | Depth | Repair | Expected | Hard cutoff | Evidence budget |
|---|---:|---:|---:|---:|---:|---:|
| `focused` | 3 | 1 | 0 | 1-2 min | 3 min | 16K tokens |
| `standard` | 4 | 2 | 1 | 3-5 min | 8 min | 30K tokens |
| `extended` | 6 | 2 | 1 | 6-10 min | 15 min | 48K tokens |

The server maps the enum to budgets. The client cannot submit arbitrary numeric limits.

Recommended indexes:

- unique `{job_id: 1}`
- unique `{user_id: 1, client_request_id: 1}`
- unique sparse `{active_key: 1}`; remove/unset `active_key` on every terminal transition
- `{status: 1, created_at: 1}`
- `{lease_expires_at: 1}`
- `{conversation_id: 1, created_at: -1}`
- `{user_id: 1, created_at: -1}`

Idempotency is keyed by `user_id + client_request_id`; the stored request fingerprint additionally covers
conversation, normalized query, model reference, sorted corpus IDs, effort, and output format. Reusing a
client request ID with a different fingerprint is a 409 error. Repeated identical submits return the same
job. A unique sparse `active_key` guarantees one active job per conversation.

---

## 6. Research API

Suggested authenticated endpoints:

```text
GET    /api/research/capabilities
POST   /api/research/jobs
GET    /api/research/jobs/{job_id}
POST   /api/research/jobs/{job_id}/cancel
GET    /api/research/jobs/{job_id}/artifact
```

Capabilities response is the only source of truth for output controls:

```json
{
  "output_formats": [
    {"format": "html", "enabled": true, "reason": null},
    {"format": "pdf", "enabled": false, "reason": "renderer_not_installed"}
  ],
  "effort_presets": ["focused", "standard", "extended"],
  "max_active_jobs_per_conversation": 1,
  "contract_version": "research_job.v1"
}
```

The frontend must select the first enabled output only when its stored selection is unavailable. It must
show disabled formats with the server-provided safe reason and must not submit them.

Create request:

```json
{
  "conversation_id": "...",
  "query": "...",
  "corpus_ids": ["..."],
  "selector_mode": "selected",
  "output_format": "html",
  "effort": "standard",
  "model_ref": "pool:entry-id-or-null",
  "client_request_id": "..."
}
```

Pydantic must use closed enums/Literals and `extra="forbid"` for this request. Do not accept free-form
format names, numeric budgets, retrieval tiers, prompt templates, or arbitrary provider parameters.

Model resolution at job creation uses the same visible-selection precedence as chat: explicit composer
`model_ref`, then conversation model, then configured query-role model. Resolve enough to validate and
store a public entry/model receipt, but never credentials. For a `pool:`/`profile:` reference, persist the
entry ID and re-resolve its encrypted credentials at each provider stage. If no valid model can be resolved,
reject creation before appending the placeholder.

Shared backend/frontend enums:

```ts
type ResearchJobStatus =
  | "queued" | "running" | "complete" | "partial" | "failed" | "cancelled";

type ResearchStage =
  | "queued" | "planning" | "retrieving" | "synthesizing"
  | "coverage_check" | "writing" | "rendering"
  | "complete" | "partial" | "failed" | "cancelled";

type ResearchOutputFormat = "html" | "pdf";
type ResearchEffort = "focused" | "standard" | "extended";
```

The backend Pydantic models are canonical; TypeScript mirrors them. Add contract fixtures/tests so enum or
field drift fails CI rather than producing stale controls.

Create response (`201` for new, `200` for idempotent existing):

```json
{
  "job_id": "...",
  "conversation_id": "...",
  "status": "queued",
  "stage": "queued",
  "output_format": "html",
  "effort": "standard",
  "corpus_snapshot": [{"corpus_id": "...", "name": "..."}],
  "created": true
}
```

Status response must be a stable public projection, never the raw Mongo row. Include progress, safe stage
failure, selected scope receipt, output metadata, and links when complete. Exclude evidence text, prompts,
raw model output, leases, encrypted config, and provider error bodies.

Stable safe error codes include:

```text
research_scope_empty
research_scope_changed
research_scope_forbidden
research_model_unavailable
research_already_active
artifact_renderer_unavailable
provider_exhausted
research_timed_out
artifact_too_large
research_cancelled
```

Required validation:

- caller owns/accesses the conversation and every selected corpus; research services must include
  `user_id` in every Mongo filter
- at least one corpus ID
- no unselected/global corpus fallback
- supported format
- bounded query length and budgets
- selected model is enabled and resolvable
- at least one selected corpus has queryable documents; partial readiness is accepted with a durable
  warning and must be disclosed in the report

Artifact endpoint behavior:

- `inline=1`: display sanitized HTML
- default or `download=1`: attachment response with stable filename
- authenticate every request; never expose an unauthenticated static path
- send restrictive CSP, `X-Content-Type-Options: nosniff`, and safe disposition headers

Message persistence contract:

- Job creation idempotently appends the user query and one assistant placeholder carrying
  `metadata.message_kind="research_artifact"` and `metadata.research_job_id`.
- Add `ConversationService.ensure_research_messages(job)` and
  `update_research_message(job_id, metadata/content)` using the metadata key. Do not depend on frontend
  message IDs, which are currently ephemeral and absent from persisted `ChatMessage`.
- If job creation succeeds but message insertion fails, the worker/status path retries
  `ensure_research_messages()`; the job remains authoritative.

---

## 7. Research Execution Pipeline

### Stage 1: Planning

Use `QueryPlanV2` first. Preserve multiword concepts and requested comparison sides. This is the top-down
spine: the original research question defines report sections and evidence obligations before retrieval.
Do not let retrieved chunks opportunistically determine the report outline.

Defaults:

- facet/depth limits from the selected effort preset
- depth 1 for direct questions, maximum 2 for genuinely composite questions
- one optional structured LLM planning call only if deterministic planning produces fewer than two useful
  facets or misses an explicit comparison side

The structured plan should contain:

```json
{
  "research_question": "...",
  "facets": [
    {"id": "f1", "question": "...", "required_concepts": ["..."]}
  ],
  "report_sections": ["..."],
  "completion_criteria": ["..."]
}
```

Validate any LLM-assisted plan against a strict schema, normalize it back into `QueryPlanV2`-compatible
units, enforce the server budget, and retain the original deterministic concepts. Invalid planner output
falls back to the deterministic plan; it never blocks the job.

### Stage 2: Retrieval

Use a two-step retrieval strategy that matches the existing implementation:

1. Run one broad `retrieve_planned(plan=..., retrieval_tier=qdrant_mongo_graph)` pass over the complete
   explicit corpus snapshot. That method already executes lanes as a combined candidate-generation,
   fusion, graph-expansion, and rerank pass.
2. Evaluate evidence obligations by report section. Only missing/weak sections become targeted support
   plans. Run those support plans concurrently, bounded by the effort facet limit and remaining wall time.

This avoids N facets x 3 retrieval tiers. Record requested and effective tiers because the retriever can
downgrade when a store/graph route is unavailable. A downgrade is acceptable only when the coverage gate
still passes; otherwise the report must disclose the missing evidence.

- Default targeted-support concurrency: 3.
- Per-pass timeout: 30 seconds, additionally bounded by remaining job time.
- A support-pass failure is isolated and recorded; it does not fail the whole job when remaining evidence
  satisfies completion criteria.
- Never expand beyond the immutable corpus ID snapshot.

### Stage 3: Evidence Ledger

Persist normalized evidence separately from prose:

```jsonc
{
  "evidence_id": "stable hash",
  "facet_id": "f1",
  "corpus_id": "...",
  "document_id": "...",
  "document_title": "...",
  "parent_id": "...",
  "chunk_id": "...",
  "heading_path": ["..."],
  "supporting_text": "exact hydrated evidence",
  "retrieval_score": 0.0,
  "reranker_score": 0.0,
  "graph_context": null
}
```

Deduplicate by `(corpus_id, document_id, chunk_id)` while retaining the set of facets each item supports.
Never merge identical-looking chunks across corpora without retaining both corpus identities.

### Stage 4: Synthesis/Compression

- If evidence fits the final writer's context budget, skip this stage.
- Otherwise compress each facet concurrently into supported learnings.
- Every learning must list its `evidence_ids`.
- Preserve raw evidence even after compression.
- Provider failure for one facet is recoverable when raw evidence can still be passed to the writer.

### Stage 5: Coverage Check and Repair

For every planned report section, verify:

- required concepts are represented
- at least one evidence item supports the section
- comparison questions have evidence for every requested side
- cross-corpus questions include the intended corpora when the query requires them

Permit at most one targeted retrieval repair round. If evidence remains insufficient, the report must state
the limitation instead of fabricating coverage.

### Stage 6: Writing

Use one final writer call over the plan, supported learnings/raw evidence, and citation map. Require:

- Markdown output
- evidence citations by stable `evidence_id`
- no citations to generated summaries unless their underlying child evidence IDs are retained
- explicit limitations
- no claims that cannot be mapped to evidence

Resolve citations to human-readable document title, corpus, section, and internal source identifier during
rendering. Do not ask the model to invent citation URLs.

### Stage 7: Rendering

- Store canonical Markdown once. The selected model never chooses the file type and never writes the
  transport wrapper.
- Resolve `output_format` through the server capability registry before work starts and again before
  rendering. A missing renderer produces `artifact_renderer_unavailable`, never a mislabeled file.
- HTML: render Markdown with a maintained Markdown library, sanitize with Bleach, wrap in a deterministic
  versioned report template, and apply an allowlist CSP.
- PDF: only when enabled, render the same sanitized HTML through the pinned server renderer. Disable remote
  resource loading and record renderer/template versions. Do not generate a second report with the LLM.
- Do not permit scripts, iframes, event handlers, remote embeds, or model-supplied raw HTML.
- HTML/PDF v1 is text/table/citation only. Do not insert remote or ephemeral image URLs. If report images
  are added later, persist them as authenticated artifact assets and rewrite references to stable local
  artifact URLs before rendering.
- Compute `sha256` over canonical Markdown and final bytes. Persist byte count, MIME type, template version,
  model identity, corpus snapshot, and citation count.
- Keep job rows small. Store artifact metadata/text in `research_artifacts`; if binary PDF can exceed the
  safe Mongo document budget, store its bytes in GridFS and retain the GridFS ID in artifact metadata.

Suggested artifact metadata:

```json
{
  "artifact_id": "uuid",
  "job_id": "uuid",
  "format": "html",
  "mime_type": "text/html; charset=utf-8",
  "title": "...",
  "filename": "research-title.html",
  "markdown": "...",
  "html": "...",
  "content_sha256": "...",
  "size_bytes": 0,
  "renderer_version": "research_html.v1",
  "citation_count": 0,
  "corpus_ids": ["..."],
  "created_at": "utc"
}
```

### Stage 8: Provider-Deterministic LLM Contract

Do not call `llm_service.complete_sync()` directly from research stages. The current helper returns only a
string, discards provider usage metadata, defaults to a broad timeout, and may return a tail of
`reasoning_content` when normal content is empty. That salvage behavior is useful for chat but is not valid
for a structured plan, verifier result, or publishable report.

Add a typed completion primitive while preserving backward compatibility:

```python
@dataclass
class LLMCompletionResult:
    content: str
    reasoning_content: str | None
    finish_reason: str | None
    model: str
    provider: str | None
    request_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    elapsed_ms: int

async def complete_with_metadata(...) -> LLMCompletionResult: ...
```

`complete_sync()` can delegate to this method and retain its existing string behavior. New
`services/research/model_gateway.py` owns the stricter research contract:

```python
async def call_research_model(
    *, stage, messages, route, input_cap, output_cap, timeout,
    response_schema=None, temperature=0.0, ledger,
) -> ValidatedResearchCall: ...
```

Required behavior:

- Resolve the immutable public model reference through `query_model_resolver` for every stage; credentials
  remain runtime-only.
- Use non-streaming calls for planner, compressor, verifier, and report generation so retries cannot
  concatenate partial output.
- Disable provider thinking for helper/research calls unless a later explicit research setting allows it.
  Existing operator `extra_params` remain authoritative, but internal capability flags never reach the
  provider payload.
- Set stage-owned `temperature`, `max_tokens`, timeout, and response mode. User chat overrides do not
  silently alter the research contract.
- Reject empty `content`, reasoning-tail fallback markers, unknown citation IDs, invalid schema, and
  `finish_reason=length` as valid terminal output.
- Record public model/provider identity, elapsed time, usage, attempt number, response mode, finish reason,
  and safe failure class. Never record prompts, API keys, raw provider bodies, or reasoning traces in the
  public job projection.

Provider capability resolution precedence:

1. explicit safe model-pool capability fields/provider card
2. known server capability table
3. conservative unknown-provider defaults

Research capabilities:

```python
class ResearchModelCapabilities(BaseModel):
    context_window: int
    max_output_tokens: int
    supports_json_schema: bool = False
    supports_json_object: bool = False
    supports_seed: bool = False
    disable_thinking: bool = True
```

Unknown models use a conservative 64K context and 8K output cap for research, even though ordinary chat's
generic fallback is larger. Operator/model-card values can safely raise those caps.

Structured-output ladder:

1. native `json_schema` when the provider card says it is supported
2. native `json_object` when supported
3. JSON-in-prompt compatibility mode
4. strip Markdown fences and parse one JSON object deterministically
5. at most one bounded schema-repair call if token/time budget remains
6. stage-specific deterministic fallback (planner only) or safe stage failure

Never repeatedly send a provider a response mode it has already rejected. Persist a provider/model
capability observation with expiry so later jobs choose the compatible mode immediately.

Retry policy:

| Failure class | Behavior |
|---|---|
| pre-connect DNS/reset | one retry with short jitter if budget remains |
| 429 | honor `Retry-After`; one retry or route to configured fallback account/model |
| 500/502/503/504 | one retry; then configured fallback if available |
| timeout after request sent | no blind same-route retry; fallback only with cost receipt |
| 400/404/422 | no retry; classify request/model/capability error |
| 401/403 | no retry; fail `provider_auth` |
| 402/balance | no retry; fail/route `provider_balance` |
| context overflow | compact deterministically once, then retry once |
| invalid structured output | deterministic parse, then one schema repair at most |
| truncated final report | one bounded continuation for missing sections, never full regeneration |

Every retry consumes the same job wall/token ledger. Provider fallback is operator-configured and visible in
the job receipt; there is no hidden hardcoded model substitution.

### Stage 9: Token Ledger and Context Packing

Current `utils.tokens.count_tokens()` is suitable as a preflight estimate but is not exact for every
OpenAI-compatible model. Research therefore uses both conservative estimates and provider-reported usage.

For each call:

```text
effective_context = min(capability.context_window, stage_context_cap)
safe_context      = floor(effective_context * 0.80)
input_allowance   = safe_context - output_cap - protocol_reserve
```

The 20% margin covers tokenizer mismatch, provider wrappers, and schema/tool overhead. Reject or compact a
payload before sending when estimated input exceeds `input_allowance`.

Proposed stage caps:

| Stage | Input cap | Output cap | Timeout | Temperature |
|---|---:|---:|---:|---:|
| optional plan repair | 4K | 1K | 30 s | 0.0 |
| evidence compression, each | 12K | 1.5K | 60 s | 0.0 |
| coverage/claim verifier | 16K | 2K | 45 s | 0.0 |
| focused writer | 24K | 6K | 90 s | 0.1 |
| standard writer | 48K | 10K | 180 s | 0.1 |
| extended writer | 80K | 16K | 300 s | 0.1 |

Caps are clamped to the resolved model capability. Extended reports that cannot fit one writer call are
written section-by-section with maximum concurrency two, then assembled deterministically from the planned
section order. Do not regenerate the entire report because one section failed.

Whole-job model-token ceilings (estimated when providers omit usage):

| Effort | Total input ceiling | Total output ceiling |
|---|---:|---:|
| focused | 50K | 8K |
| standard | 120K | 18K |
| extended | 240K | 32K |

Persist a private `token_ledger` containing estimated/actual input, output, retry tokens, call count, and
remaining budget by stage. Public status exposes only aggregate call/tokens/cost when safe. Before each
provider call atomically reserve estimated tokens; reconcile against actual usage afterward. If the next
call would exceed the ceiling, stop expansion and enter finalization with the evidence already collected.

Context packing priority:

1. system/report contract
2. original question and immutable plan
3. direct evidence for required concepts/comparison sides
4. graph facts/relations required by the question
5. highest-quality supporting evidence with corpus/document diversity
6. compressed learnings
7. optional background evidence

Never resend the full raw evidence ledger to every stage. Compression and section-specific evidence views
exist specifically to prevent repeated API billing.

### Stage 10: Three Retrieval Layers

Deep Research must use the retrieval layers by evidence role, not perform three identical full searches.

| Layer | Contract | Research role |
|---|---|---|
| Fast (`qdrant_only`) | dense/summary anchors, no expensive graph path | exact titles, named phrases, quick source scouting, fallback anchors |
| Hybrid (`qdrant_mongo`) | dense + summaries + lexical + fusion + reranker | primary factual, definitional, procedural, and section evidence |
| Graph (`qdrant_mongo_graph`) | Hybrid signals plus graph facts, expansion, predicates, graph-aware selection | relationships, causal/mechanism questions, comparisons, bridges, and cross-corpus synthesis |

Primary pass:

- Request planned Graph retrieval for the whole query. The existing implementation concurrently executes
  dense child, summary, and lexical lanes, then graph facts/expansion, fusion, reranking, grounding,
  diversity, concept coverage, repair diagnostics, and hydration.
- Persist its diagnostics as a private layer receipt: candidate counts by lane, effective tier, downgrade,
  graph facts/predicates, required-concept coverage, corpus/document distribution, and timings.

Targeted repair routing:

- missing exact title/quoted/named anchor -> Fast
- missing ordinary concept/definition/procedure evidence -> Hybrid
- missing relationship, comparison side, mechanism, entity bridge, or cross-corpus connection -> Graph
- graph downgrade/zero graph evidence on a graph-required question -> one graph-specific retry if store
  health and budget allow; otherwise mark the obligation unsupported

The final card/report receipt states which layers materially contributed. A layer counts as used only when
it contributes retained evidence, not merely because a request was attempted.

### Stage 11: Answerability and Time Cutoff

The report must begin with a direct answer to the original question. A polished document that only
summarizes nearby topics is not successful.

`complete` requires:

- all required concepts supported (`required_concept_coverage == 1.0`)
- every required report section has at least one direct evidence item
- every comparison side has evidence
- requested cross-corpus synthesis retains evidence from each required corpus, or the query did not require
  every selected corpus to contribute
- graph-required questions have retained graph facts/relations or direct textual relationship evidence
- every citation resolves to an evidence-ledger ID
- final claim verifier finds no unsupported central claim
- report contains a direct-answer/BLUF section and explicit limitations

`partial` means an artifact was produced but one or more required obligations remain unsupported. The card
is amber and lists the limitation count. `failed` means there is not enough evidence to responsibly answer,
the writer/provider failed, or no valid artifact could be rendered. Never label either state `complete`.

Recommended time contract, pending owner ratification:

| Effort | Expected completion | Hard cutoff | Finalization reserve |
|---|---:|---:|---:|
| focused | 1-2 min | 3 min | 60 s |
| standard | 3-5 min | 8 min | 150 s |
| extended | 6-10 min | 15 min | 240 s |

When `hard_cutoff - finalization_reserve` is reached:

1. stop starting new retrieval/compression units
2. cancel unstarted work and await/cancel active bounded calls
3. run coverage on collected evidence
4. write/render a complete or partial report within the reserve
5. fail safely if evidence cannot support a direct answer

The frontend displays elapsed time, current stage, completed/total units, and the selected hard cutoff. It
does not promise an exact completion time or show a fabricated percentage.

---

## 8. Frontend Behavior

### Existing Popup to Extend

Do not create a new feature modal. `ChatInput.tsx` already renders:

```text
Composer header
  [Model] [Thinking]                         [Features N]

Features popup (30rem desktop, viewport-width mobile)
  Header
  ToggleBar(HyDE, Reason, Web, activators)
```

Refactor the popup body into `FeaturesPanel.tsx` and preserve its current anchoring, outside-click behavior,
mobile fixed positioning, reduced-motion rules, and visual tokens.

Target layout:

```text
FEATURES                                      2 configured

CORE MODES
  [RAG switch]  RAG
                Retrieve new evidence for ordinary chat
                State: ON | OFF (model/history only)

  [toggle]      DEEP RESEARCH          NEXT MESSAGE
                Build a cited report from the active corpus scope

  ┌ shown only when Deep Research is armed ─────────────────────┐
  │ SCOPE       All corpora (3)                 [Edit in Sources]│
  │             ecommerce, transcripts, polymath                │
  │ EFFORT      [Focused] [Standard] [Extended]                  │
  │ OUTPUT      [HTML] [PDF-disabled-with-reason]                │
  │ MODEL       Current selected chat model                      │
  │             Corpus retrieval runs even when chat RAG is off │
  └──────────────────────────────────────────────────────────────┘

QUERY ENHANCEMENTS
  existing ToggleBar: HyDE | Reason | Web | Activators
```

Design rules:

- Use a toggle for RAG and Deep Research, a segmented control for exactly-one effort/output selection,
  icons from Lucide, and familiar icon-only commands with tooltips on artifact cards.
- Keep cards/radii within the existing design system; do not place nested decorative cards inside cards.
- Scope is read-only in Features. `Edit in Sources` closes Features and opens the existing Sources panel,
  or focuses its button. Never create a second corpus selector with divergent state.
- Show at most two corpus names plus `+N`; put the full list in a tooltip/accessible description.
- The Features count represents non-default/armed behavior: RAG off counts as one, Deep Research armed
  counts as one, plus existing HyDE/Reason/Web/tools/skills. Default RAG on does not permanently inflate it.
- Feature popup width may grow from 30rem to 34rem on desktop; it must remain `max-width: calc(100vw-1rem)`.
- All labels must fit at 390px mobile width without horizontal scrolling.

### RAG Toggle UX

- Label: `RAG`.
- On description: `Retrieve new evidence from the active corpus scope.`
- Off description: `Use model knowledge and conversation history. No new corpus retrieval.`
- Add a visible `RAG OFF` chip above the textarea while disabled.
- Preserve displayed sources from earlier messages.
- Disable the HyDE control while RAG is off with tooltip `HyDE requires corpus retrieval`; preserve its
  stored preference without applying it.
- Keep Web separately controllable and state clearly that it can still fetch external context.

### Deep Research UX

- Label: `Deep Research`; state badge: `NEXT MESSAGE` while armed.
- Toggle arms one submission. It is not a permanent reasoning mode.
- Default effort: `standard`.
- Default output: last supported selection, otherwise first enabled capability (HTML initially).
- The popup scope receipt uses the exact resolved submission scope, including All-corpora expansion.
- Research uses the selected chat model reference shown in the composer. Do not add another model picker.
- Attachments are not part of corpus-only research v1. If attachments are staged, block research submission
  and tell the user to remove them or send them as an ordinary chat turn. Never silently discard them.
- Existing Web, HyDE, Reason, tools, and skills remain selected for later chat but are not sent to the
  research job. While armed, visually mark them `Chat only`/disabled where interaction would be misleading.
- RAG-off is not a conflict: research still performs its own selected-corpus retrieval and does not change
  the conversation RAG state.
- Change the execute button to `RESEARCH` with a research icon while armed. Change the input placeholder to
  `Describe the research question...` and footer to `Research armed / <scope> / <format>`.
- Await job acceptance. Clear prompt and disarm only after `created=true` or an idempotent existing job is
  returned. On error, retain everything.
- Only one active job per conversation. A 409/active response focuses the existing research card.

### Artifact Card

`MessageBubble` detects `metadata.message_kind === "research_artifact"` and renders
`ResearchArtifactCard` instead of treating placeholder text as ordinary Markdown.

States:

```text
queued       Research queued
planning     Building research plan
retrieving   Collecting corpus evidence       2 / 4 units
synthesizing Distilling supported findings
writing      Writing cited report
rendering    Rendering HTML/PDF
complete     <title>                           [Open] [Download]
partial      <title> · evidence gaps            [Open] [Download]
failed       Research stopped at <safe stage>  [Retry]
cancelled    Research cancelled                [Retry]
```

- Show deterministic progress units and elapsed time, not fabricated percentage completion.
- Show the expected range and hard cutoff for the selected effort; do not display a guaranteed ETA.
- Include scope receipt (`3 corpora`), effort, output, and public model label.
- `Open` opens authenticated HTML in a new tab/window. For PDF, open the authenticated blob URL.
- `Download` uses the authenticated API client and a blob URL; never expose bearer tokens in query strings.
- `Cancel` appears only for active states and requires a compact confirmation.
- `Retry` creates a new `client_request_id` with the same query/scope/effort/format after user confirmation.
- Poll every two seconds while visible; back off to ten seconds when the document is hidden. Stop at terminal
  state. Deduplicate polling per `job_id`.
- On conversation reload, each placeholder's `research_job_id` hydrates from the status endpoint. Update the
  existing message by job ID; never append a second card for the same job.

Suggested message metadata:

```json
{
  "message_kind": "research_artifact",
  "research_job_id": "...",
  "research_status": "running",
  "research_stage": "retrieving",
  "artifact_id": null,
  "output_format": "html",
  "effort": "standard",
  "corpus_ids": ["..."],
  "corpus_names": ["..."],
  "model_label": "..."
}
```

### Feature Interaction Matrix

| State | Ordinary chat | Deep Research job |
|---|---|---|
| RAG on | corpus retrieval enabled | selected-corpus research enabled |
| RAG off | no corpus retrieval | still selected-corpus research enabled |
| HyDE on | applies only when RAG on | never applies |
| Web on | may add web context to chat | never applies; corpus-only invariant |
| Reason on | current chat reasoning cascade | never applies unless later explicitly designed |
| Tools/skills on | current chat behavior | not included in research v1 |
| Attachments staged | ordinary attachment chat | submission blocked in v1 |

---

## 9. Latency and Cost Guardrails

These are requirements:

- No clarification LLM call by default.
- No HyDE.
- Deterministic plan first.
- One combined planned retrieval pass first; only uncovered sections fan out to targeted support passes.
- Maximum three concurrent targeted support passes by default.
- One optional planner-repair call, optional concurrent compression calls, and one final writer phase
  (sectioned only when the report cannot safely fit one call).
- Skip compression when evidence fits.
- One repair round maximum.
- Cache facet retrieval by corpus snapshot + normalized query + retrieval contract version when safe.
- Use existing pooled clients and query embedding cache.
- Never invoke Fast + Hybrid + Graph as three independent full passes for the same facet.
- Track stage latency, token usage, provider/model, evidence count, and retry count.
- Research runs asynchronously and must not hold the `/api/chat` request open.

Initial service-level targets:

- job creation response: p95 < 500 ms
- status endpoint: p95 < 300 ms
- initial visible progress: < 2 seconds after job creation
- proposed hard cutoffs pending owner ratification: focused 3 minutes, standard 8 minutes, extended 15 minutes
- ordinary RAG-off chat: no retrieval calls and no retrieval latency
- duplicate job execution: zero
- unsupported-citation rate in evaluation: zero

---

## 10. Restart, Stale-State, and Failure Semantics

- Research worker claims jobs atomically with a lease.
- Renew the lease between stages and during long provider calls.
- On startup, reclaim only expired running jobs.
- Each stage writes its durable output before advancing status.
- Re-executing a completed stage must either reuse its output or replace it transactionally.
- Cancellation is cooperative: mark `cancel_requested`, stop between bounded units, then set `cancelled`.
- A model/provider error records a sanitized failure class and stage.
- Partial facet failures do not fail the job when completion criteria remain satisfiable.
- Rendering failure must not destroy completed Markdown.
- A stale placeholder message recovers by reading `research_job_id`; the message is never the job source of
  truth.
- Unique indexes and idempotency prevent duplicate active jobs/artifacts.
- Every terminal transition unsets `active_key`, including failure during validation/rendering and
  cancellation. A reconciliation pass repairs leaked active keys before claiming new work.
- `ensure_research_messages()` repairs a missing placeholder after partial creation without duplicating an
  existing `metadata.research_job_id` message.
- Frontend capabilities are refreshed when the popup opens if older than five minutes. If a renderer
  disappears after arming, submission fails visibly and keeps the armed configuration.
- Corpus readiness/version drift is recorded. The job remains scoped to the same IDs; it never substitutes
  a different corpus because one became unavailable.

---

## 11. Testing Matrix

### Unit

- RAG-off branch skips every retrieval-related function.
- Query-plan conversion preserves phrases and corpus IDs.
- Evidence deduplication retains cross-corpus identity.
- Coverage detects a missing comparison side.
- One and only one repair round is allowed.
- Citation resolver rejects unknown evidence IDs.
- Markdown renderer strips scripts, event handlers, iframes, and unsafe URLs.
- Job transition table rejects illegal transitions.
- Idempotent creation returns the existing active job.
- Effort enums map to exact server-owned budgets; arbitrary limits are rejected.
- Exactly one output format is accepted and disabled capabilities are rejected.
- All-corpora frontend resolution produces an explicit deduplicated list.
- Persisted research messages reconcile by `metadata.research_job_id` without duplicates.
- Unknown-model capability defaults clamp context/output conservatively.
- Token reservations cannot exceed the effort ledger; actual usage reconciles estimates.
- 401/402/4xx do not retry; 429 honors cooldown; retry/fallback attempts remain bounded.
- Reasoning-tail salvage and truncated output cannot pass planner/report validation.
- Structured-output capability fallback is remembered and never loops on a rejected mode.
- Retrieval-gap routing chooses Fast for anchors, Hybrid for ordinary evidence, and Graph for relations.
- `complete` is impossible when required concept/side/citation coverage is missing.

### Integration

- Create -> claim -> retrieve -> write -> render -> complete.
- Worker restart after every stage resumes without duplicated calls/artifacts.
- Cancel queued and running jobs.
- One failed facet with sufficient remaining evidence still completes with a limitation note.
- Provider failure preserves the stage/evidence already completed.
- Artifact endpoint enforces conversation/corpus ownership.
- Multiple selected corpora remain scoped; an unselected corpus never appears in evidence.
- Similar books in separate corpora retain distinct corpus/document citations.
- Every research Mongo query is filtered by `user_id`; cross-user job/artifact access returns 404/403.
- A second active job in the same conversation returns/focuses the existing job.
- HTML capability self-test enables HTML; PDF remains disabled when renderer dependencies are absent.
- Output MIME type, extension, content hash, and selected format agree.
- Layer receipts prove which retained evidence came from Fast/Hybrid/Graph.
- At finalization reserve, new retrieval stops and the job reaches complete/partial/failed before cutoff.
- Provider usage metadata and retry tokens appear in the private ledger without leaking prompts/secrets.

### Frontend

- RAG toggle persists per conversation and affects the next chat payload.
- Deep Research disarms after one submit.
- All-corpora selector expands to the explicit IDs shown in the popup receipt.
- Empty/unloaded corpus resolution blocks submission without clearing the prompt.
- Failed job creation keeps Deep Research armed; accepted creation clears/disarms exactly once.
- Features count treats default RAG-on as zero and counts RAG-off/research-armed states.
- Research mode changes the execute label/placeholder and blocks staged attachments.
- Unsupported PDF is visibly disabled from the capabilities response.
- One card updates in place from queued through complete.
- Reload restores running/completed cards.
- Open/download work and display no broken or stale controls.
- Long titles, failures, and progress text do not overflow mobile or desktop layouts.
- Web/HyDE/Reason/tool selections are not accidentally included in research requests.
- The card distinguishes `partial` from `complete` and shows evidence limitations.
- The selected hard cutoff and elapsed time remain visible without a fabricated ETA/percentage.

### Quality Evaluation

Use real questions against `ecommerce_pdf` and `markbuildsbrands_transcripts`:

- single-corpus factual synthesis
- comparison requiring both corpora
- multiword domain concept preservation
- graph-relation question
- question with intentionally insufficient evidence

Score:

- required-concept coverage
- citation precision
- citation completeness
- corpus-scope violations
- duplicate evidence rate
- unsupported claim rate
- retrieval and end-to-end latency
- model token usage/cost

Do not call the feature production-ready without an asserting evaluation receipt.

---

## 12. Implementation Order

### Phase 1: Contracts and RAG-Off Path

- [ ] Add `retrieval_enabled` to the live request schema.
- [ ] Extract/add the early no-retrieval streaming helper.
- [ ] Persist RAG state in user/assistant metadata and rehydrate it per conversation.
- [ ] Add deterministic `NO RAG` trust-badge state.
- [ ] Add asserting backend tests proving no retrieval work occurs.
- [ ] Refactor the existing Features popup and add RAG toggle/request wiring.

### Phase 2: Durable Research Core

- [ ] Add research schemas, collection indexes, repository/service, and transition table.
- [ ] Add capability/create/status/cancel/artifact endpoints with `user_id` ownership checks.
- [ ] Add lease-based backend poller and startup reclamation.
- [ ] Add one-active-job enforcement, idempotency, and sanitized error classes.
- [ ] Add metadata-keyed conversation placeholder reconciliation.

### Phase 3: Retrieval and Evidence

- [ ] Adapt `QueryPlanV2` to a bounded research plan.
- [ ] Run one combined planned Graph retrieval, then bounded targeted support passes only for gaps.
- [ ] Add deterministic Fast/Hybrid/Graph gap routing and retained-evidence layer receipts.
- [ ] Persist/deduplicate the evidence ledger.
- [ ] Add optional evidence compression.
- [ ] Add coverage check and one targeted repair round.

### Phase 4: Writer and HTML Artifact

- [ ] Pin Markdown + Bleach dependencies and add an HTML capability self-test.
- [ ] Add metadata-returning LLM completion primitive and strict research model gateway.
- [ ] Add provider capability registry, structured-output ladder, retry classifier, and token ledger.
- [ ] Enforce finalization reserve and complete/partial/failed answerability contract.
- [ ] Add evidence-bound Markdown writer.
- [ ] Add deterministic citation resolution.
- [ ] Add versioned Markdown rendering, sanitization, CSP, hashes, and artifact persistence.
- [ ] Add artifact endpoint tests and malicious-content fixtures.

### Phase 5: Frontend Research Experience

- [ ] Add `FeaturesPanel` inside the existing popup; do not add another popover/modal.
- [ ] Add one-shot Deep Research state, effort/output segmented controls, and capabilities fetch.
- [ ] Extract shared All/selected corpus resolution and show the exact scope receipt.
- [ ] Route `ComposerSubmission` to chat SSE or research job creation.
- [ ] Add placeholder/progress/artifact message card.
- [ ] Add polling, reload recovery, cancellation, open, and download.
- [ ] Add Playwright contract tests and verify 390px/desktop layouts with screenshots.

### Phase 6: Production Gate

- [ ] Run unit and integration suites.
- [ ] Test restart after every durable stage.
- [ ] Run real corpus quality/latency evaluation.
- [ ] Verify Docker image contains new code and dependencies.
- [ ] Verify Cloudflare routes/auth headers for research endpoints.
- [ ] Verify MCP remains compatible; do not require MCP for feature operation.
- [ ] Record before/after metrics and commit intentionally.

### Later: PDF

- [ ] Render PDF from stored Markdown/HTML using a container-supported renderer.
- [ ] Verify fonts, page breaks, tables, citations, and download headers visually.
- [ ] Keep the research job and evidence contracts unchanged.

---

## 13. Expected File Surface

Exact names may follow existing conventions, but keep the change scoped:

```text
backend/models/schemas.py
backend/requirements.txt
backend/routers/chat.py
backend/routers/research.py                    # new
backend/services/chat_orchestrator.py
backend/services/conversation.py
backend/services/llm.py                        # metadata completion primitive
backend/services/query_model_resolver.py       # public capability/model receipt
backend/services/research/__init__.py           # new
backend/services/research/models.py             # new or use schemas.py
backend/services/research/repository.py         # new
backend/services/research/planner.py            # new adapter over QueryPlanV2
backend/services/research/executor.py            # new durable stage executor
backend/services/research/evidence.py            # new
backend/services/research/model_gateway.py       # new provider/token/validation contract
backend/services/research/retrieval.py           # new layer routing/receipts
backend/services/research/renderer.py            # new
backend/main.py
backend/tests/test_chat_no_rag.py                # new
backend/tests/test_research_*.py                 # new
frontend/src/App.tsx
frontend/src/types/chat.ts
frontend/src/types/research.ts                   # new
frontend/src/components/chat/ChatInput.tsx
frontend/src/components/chat/ToggleBar.tsx
frontend/src/components/chat/FeaturesPanel.tsx   # new
frontend/src/components/chat/MessageBubble.tsx
frontend/src/components/chat/ResearchArtifactCard.tsx  # new
frontend/src/hooks/useResearchJob.ts             # new
frontend/src/lib/api.ts
frontend/src/stores/chatStore.ts
frontend/tests/e2e/deep-research.spec.ts         # new
frontend/tests/e2e/rag-off.spec.ts               # new
```

Avoid adding research logic directly to the already-large chat orchestrator. It should route to cohesive
services.

---

## 14. Effort Estimate

Based on the verified current repository state:

- usable HTML-first vertical slice: 6-8 focused engineering hours
- hardened implementation with ownership, restart, security, and evaluation coverage: 10-14 hours total
- PDF renderer, container dependencies, and visual QA: additional 3-5 hours

These are implementation estimates, not elapsed wall-clock guarantees. Provider latency, existing failing
tests, or unrelated dirty changes can alter completion time.

---

## 15. Resume Checklist for a New Agent

1. Read this document completely.
2. Run `git status --short --branch`; do not revert unrelated ingestion changes.
3. Verify the anchors in Section 3 because line numbers and implementations may have moved.
4. Search for any work already completed from the Phase 1-6 checklists before adding duplicate code.
5. Confirm the old `deep_research` reasoning-mode naming collision still exists.
6. Implement Phase 1 first and prove RAG-off issues zero retrieval calls.
7. Keep research jobs separate from chat messages; job state is authoritative.
8. Preserve `settingsStore.selectedCorpusIds=[]` as the All-corpora UI sentinel, but expand it to explicit
   owned corpus IDs before job creation. Persist no ambiguous empty scope.
9. Keep canonical Markdown and evidence ledger even if HTML rendering fails.
10. Run asserting tests after each phase and update this document's checkboxes and status.

Definition of done:

- RAG-off chat behaves like normal model chat with history and performs zero new retrieval.
- One-shot Deep Research creates a durable job over the exact resolved selector snapshot.
- Restart/cancel/retry cannot duplicate jobs or artifacts.
- The completed HTML artifact opens/downloads from chat and survives container restart.
- Every material report claim can be traced to corpus/document/chunk evidence.
- The Features popup, scope receipt, effort/output selection, execute-button mode, and artifact card remain
  coherent on mobile, reload, provider failure, and stale-job recovery.
- Real cross-corpus evaluation meets the production gate with recorded quality, latency, and cost metrics.
