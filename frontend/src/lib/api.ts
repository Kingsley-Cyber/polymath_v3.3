// api.ts - Centralized API client for all backend communication
// ALL backend calls go through here. No raw fetch in components.

import type {
  Conversation,
  ConversationListItem,
  ChatRequest,
  ModelsResponse,
  Collection,
  SSEEvent,
  ChatMessage,
  Tool,
  ToolCreate,
  ToolUpdate,
  LoginRequest,
  LoginResponse,
  UserMeResponse,
  UpdateCredentialsRequest,
  UpdateCredentialsResponse,
  CorpusCreate,
  CorpusUpdate,
  CorpusResponse,
  CorpusDeleteResponse,
  ModelProfileRef,
  DocumentResponse,
  IngestJobResponse,
  IngestBatchResponse,
  LocalIngestBatchRequest,
  EntityResult,
  RelationEdge,
  ChunkExtractionResponse,
  DocExtractionItem,
  ModalTestResult,
  GlobalSettingsResponse,
  GlobalSettingsUpdate,
  InfrastructureTestResponse,
  GraphQueryResult,
  DiscourseGraphResponse,
  EntitySearchResponse,
  GraphNodeInsightResponse,
  GraphAnalyzeRequest,
  GraphAnalyzeResponse,
  ApiKeysPublic,
  ModelProfile,
  ModelProfileCreate,
  ModelProfileUpdate,
  ModelProfilesListResponse,
  ModelProfileTestResult,
  ModelPoolEntry,
  ModelPoolEntryCreate,
  ModelPoolEntryUpdate,
  ModelPoolListResponse,
  ModelPoolTestResult,
} from "../types";
import { useAuthStore } from "../stores/authStore";

const API_BASE = "/api";

// Helper to read persisted auth token from localStorage
function getPersistedToken(): string | null {
  try {
    const raw = localStorage.getItem("polymath-auth");
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed?.state?.token || null;
  } catch {
    return null;
  }
}

// Helper for JSON requests — auto-attaches Bearer token when available
async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  // Auto-attach Authorization header if we have a persisted token
  const token = getPersistedToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE}${url}`, {
    headers,
    ...options,
  });

  if (!response.ok) {
    // 401 interceptor — token expired or invalid, force re-login (UT-004)
    if (response.status === 401) {
      useAuthStore.getState().clearAuth();
      throw new Error("Session expired. Please log in again.");
    }
    const error = await response.text();
    throw new Error(`HTTP ${response.status}: ${error}`);
  }

  return response.json();
}

// Health check
export async function checkHealth(): Promise<{
  status: "ok" | "degraded" | "error";
  services: Record<
    string,
    { status: "ok" | "error"; latency_ms: number; error?: string }
  >;
}> {
  return fetchJSON("/health");
}

// Models
export async function getModels(): Promise<ModelsResponse> {
  return fetchJSON("/models");
}

// Conversations
export async function listConversations(
  limit = 50,
  offset = 0,
): Promise<ConversationListItem[]> {
  return fetchJSON(`/conversations?limit=${limit}&offset=${offset}`);
}

export async function getConversation(id: string): Promise<Conversation> {
  return fetchJSON(`/conversations/${id}`);
}

export async function getMessages(
  conversationId: string,
  skip = 0,
  limit = 50,
): Promise<ChatMessage[]> {
  return fetchJSON(
    `/conversations/${conversationId}/messages?skip=${skip}&limit=${limit}`,
  );
}

export async function createConversation(payload?: {
  title?: string;
  llm_config?: {
    model: string;
    temperature: number;
    top_p: number;
    max_tokens: number;
  };
}): Promise<{ id: string }> {
  return fetchJSON("/conversations", {
    method: "POST",
    body: JSON.stringify(payload || {}),
  });
}

export async function updateConversation(
  id: string,
  payload: { title?: string; llm_config?: Record<string, unknown> },
): Promise<{ success: boolean }> {
  return fetchJSON(`/conversations/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });
}

export async function deleteConversation(
  id: string,
): Promise<{ success: boolean }> {
  return fetchJSON(`/conversations/${id}`, {
    method: "DELETE",
  });
}

// Collections
export async function getCollections(): Promise<Collection[]> {
  // v3 is corpus-scoped — the legacy /api/collections endpoint was removed.
  // Short-circuit instead of firing a request that always 404s and noisily
  // pollutes the browser console. The CollectionSelector renders empty,
  // which is the intended state until that legacy UI surface is retired.
  return [];
}

// Chat Streaming (SSE)
/**
 * Contract for POST /api/chat
 * Request:  { message: string, conversation_id?: string, overrides?: ModelOverrides }
 * Response: SSE stream of `data: {json}\n\n` events, where json is one of:
 *           { type: "token",    content: string }
 *           { type: "thinking", thinking: string }
 *           { type: "trimming", trimming_applied: bool, trimming_details: string }
 *           { type: "done",     conversation_id: string, model_used: string }
 *           { type: "error",    content: string }
 */
export async function streamChat(
  request: ChatRequest,
  onEvent: (event: SSEEvent) => void,
  onError: (error: Error) => void,
): Promise<void> {
  try {
    // Build headers with auth token — SSE uses raw fetch, not fetchJSON (UT-002)
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
    };
    const token = getPersistedToken();
    if (token) {
      headers["Authorization"] = `Bearer ${token}`;
    }

    const response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers,
      body: JSON.stringify(request),
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}: ${await response.text()}`);
    }

    if (!response.body) {
      throw new Error("No response body");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });

        let boundary = buffer.indexOf("\n\n");
        while (boundary !== -1) {
          const chunk = buffer.slice(0, boundary).trim();
          buffer = buffer.slice(boundary + 2);

          if (chunk.startsWith("data: ")) {
            const dataStr = chunk.slice(6).trim();
            if (dataStr && dataStr !== "[DONE]") {
              try {
                const event: SSEEvent = JSON.parse(dataStr);
                onEvent(event);
              } catch (e) {
                console.error("Failed to parse SSE event:", dataStr, e);
              }
            }
          }
          boundary = buffer.indexOf("\n\n");
        }
      }
    } finally {
      reader.releaseLock();
    }
  } catch (error) {
    onError(error as Error);
  }
}

// File upload for ingestion
export async function uploadFile(
  file: File,
  collectionId?: string,
): Promise<{ job_id: string; status: string }> {
  const formData = new FormData();
  formData.append("file", file);
  if (collectionId) {
    formData.append("collection_id", collectionId);
  }

  const response = await fetch(`${API_BASE}/ingest/start`, {
    method: "POST",
    body: formData,
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(`HTTP ${response.status}: ${error}`);
  }

  return response.json();
}

// Check ingestion status
export async function getIngestionStatus(jobId: string): Promise<{
  job_id: string;
  status: "pending" | "processing" | "completed" | "failed";
  progress?: number;
  error?: string;
}> {
  return fetchJSON(`/ingest/${jobId}`);
}

// Tools
export async function listTools(): Promise<Tool[]> {
  return fetchJSON("/tools");
}

export async function createTool(tool: ToolCreate): Promise<Tool> {
  return fetchJSON("/tools", {
    method: "POST",
    body: JSON.stringify(tool),
  });
}

export async function updateTool(id: string, tool: ToolUpdate): Promise<Tool> {
  return fetchJSON(`/tools/${id}`, {
    method: "PATCH",
    body: JSON.stringify(tool),
  });
}

export async function deleteTool(id: string): Promise<{ success: boolean }> {
  return fetchJSON(`/tools/${id}`, {
    method: "DELETE",
  });
}

// ============================================================================
// SETTINGS — Phase 14 Modal probe helper only
// (Canonical settings CRUD: see getGlobalSettings / updateGlobalSettings /
//  testInfrastructure / testService further below.)
// ============================================================================

/**
 * POST /api/settings/infrastructure/modal/verify-token
 * Verify saved (or inline) Modal tokens by calling `modal token info`
 * server-side. Returns workspace name on success. Zero cost, ~1s latency.
 */
export async function verifyModalToken(
  override?: { token_id?: string; token_secret?: string },
): Promise<{ ok: boolean; workspace: string | null; error: string | null }> {
  return fetchJSON("/settings/infrastructure/modal/verify-token", {
    method: "POST",
    body: JSON.stringify(override ?? {}),
  });
}

export async function testModalEndpoint(): Promise<ModalTestResult> {
  // Single-service probe — backend returns {service, status, latency_ms, error?}
  const raw = await fetchJSON<{
    service: string;
    status: "ok" | "error" | null;
    latency_ms: number | null;
    error: string | null;
  }>("/settings/infrastructure/test/modal", { method: "POST" });
  return {
    ok: raw.status === "ok",
    latency_ms: raw.latency_ms ?? 0,
    dimension: raw.status === "ok" ? 1024 : null,
    error: raw.error,
  };
}

// ============================================================================
// AUTH
// ============================================================================

/**
 * POST /api/auth/login
 * Authenticate with username/password, receive JWT.
 */
export async function login(request: LoginRequest): Promise<LoginResponse> {
  // Use raw fetch here — no token needed for login itself
  const response = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(`HTTP ${response.status}: ${error}`);
  }

  return response.json();
}

/**
 * GET /api/auth/me
 * Validate current token and return user info.
 */
export async function getMe(): Promise<UserMeResponse> {
  return fetchJSON("/auth/me");
}

/**
 * PATCH /api/auth/update
 * Update username and/or password. Requires current password.
 * Returns a fresh JWT token.
 */
export async function updateCredentials(
  request: UpdateCredentialsRequest,
): Promise<UpdateCredentialsResponse> {
  return fetchJSON("/auth/update", {
    method: "PATCH",
    body: JSON.stringify(request),
  });
}

// ============================================================================
// CORPORA
// ============================================================================

/**
 * GET /api/corpora
 * List all corpora owned by the current user.
 */
export async function listCorpora(): Promise<CorpusResponse[]> {
  return fetchJSON("/corpora");
}

/**
 * GET /api/corpora/{corpus_id}
 * Get a single corpus by ID.
 */
export async function getCorpus(corpusId: string): Promise<CorpusResponse> {
  return fetchJSON(`/corpora/${corpusId}`);
}

/**
 * POST /api/corpora
 * Create a new corpus.
 */
export async function createCorpus(
  payload: CorpusCreate,
): Promise<CorpusResponse> {
  return fetchJSON("/corpora", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * PUT /api/corpora/{corpus_id}
 * Update corpus metadata (name, description, config).
 */
export async function updateCorpus(
  corpusId: string,
  payload: CorpusUpdate,
): Promise<CorpusResponse> {
  return fetchJSON(`/corpora/${corpusId}`, {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

/**
 * DELETE /api/corpora/{corpus_id}
 * Delete a corpus and cascade all associated data.
 */
export async function deleteCorpus(
  corpusId: string,
): Promise<CorpusDeleteResponse> {
  return fetchJSON(`/corpora/${corpusId}`, {
    method: "DELETE",
  });
}

export type IngestionModelTestKind = "chat" | "embedding";
export type IngestionModelPoolField =
  | "summary_models"
  | "extraction_models"
  | "embedding_models";

export interface IngestionModelTestRequest {
  kind: IngestionModelTestKind;
  entry: ModelProfileRef;
  corpus_id?: string | null;
  pool_field?: IngestionModelPoolField | null;
  index?: number | null;
}

export interface IngestionModelTestResult {
  ok: boolean;
  kind: IngestionModelTestKind;
  status?: number | null;
  latency_ms?: number | null;
  model?: string | null;
  base_url?: string | null;
  dimension?: number | null;
  error?: string | null;
}

export async function testIngestionModelRef(
  body: IngestionModelTestRequest,
): Promise<IngestionModelTestResult> {
  return fetchJSON("/ingestion/model-ref/test", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * GET /api/corpora/{corpus_id}/documents
 * List all documents in a corpus.
 */
export async function listDocuments(
  corpusId: string,
  limit = 100,
  offset = 0,
): Promise<DocumentResponse[]> {
  return fetchJSON(
    `/corpora/${corpusId}/documents?limit=${limit}&offset=${offset}`,
  );
}

/**
 * POST /api/corpora/{corpus_id}/ingest
 * Upload and ingest a document into a corpus.
 */
export async function uploadDocumentToCorpus(
  corpusId: string,
  file: File,
  options?: {
    use_neo4j?: boolean;
    chunk_summarization?: boolean;
    model?: string;
  },
): Promise<IngestJobResponse> {
  const formData = new FormData();
  formData.append("file", file);
  if (options?.use_neo4j !== undefined)
    formData.append("use_neo4j", String(options.use_neo4j));
  if (options?.chunk_summarization !== undefined)
    formData.append("chunk_summarization", String(options.chunk_summarization));
  if (options?.model) formData.append("model", options.model);

  const token = getPersistedToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch(`${API_BASE}/corpora/${corpusId}/ingest`, {
    method: "POST",
    headers,
    body: formData,
  });

  if (!response.ok) {
    if (response.status === 401) {
      useAuthStore.getState().clearAuth();
      throw new Error("Session expired. Please log in again.");
    }
    const error = await response.text();
    throw new Error(`HTTP ${response.status}: ${error}`);
  }

  return response.json();
}

export async function createLocalIngestBatch(
  corpusId: string,
  body: LocalIngestBatchRequest,
): Promise<IngestBatchResponse> {
  return fetchJSON(
    `/corpora/${encodeURIComponent(corpusId)}/ingest-batches/local`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

export async function getIngestBatch(
  batchId: string,
): Promise<IngestBatchResponse> {
  return fetchJSON(`/ingest-batches/${encodeURIComponent(batchId)}`);
}

export async function resumeIngestBatch(
  batchId: string,
): Promise<IngestBatchResponse> {
  return fetchJSON(`/ingest-batches/${encodeURIComponent(batchId)}/resume`, {
    method: "POST",
  });
}

export async function reconcileStaleIngestion(
  corpusId: string,
  body: { stale_after_minutes?: number; auto_backfill_graph?: boolean } = {},
): Promise<Record<string, unknown>> {
  return fetchJSON(
    `/corpora/${encodeURIComponent(corpusId)}/ingestion/reconcile-stale`,
    {
      method: "POST",
      body: JSON.stringify(body),
    },
  );
}

// ============================================================================
// EXTRACTION / GRAPH (Phase 9)
// ============================================================================

export interface GraphData {
  nodes: Array<{
    id: string;
    name: string;
    entity_type: string;
    mention_count: number;
    color: string;
    val: number;
  }>;
  links: Array<{
    source: string;
    target: string;
    predicate: string;
    confidence: number;
  }>;
  neo4jEnabled: boolean;
}

const ENTITY_COLORS: Record<string, string> = {
  person: "#3b82f6",
  org: "#a855f7",
  concept: "#22c55e",
  other: "#6b7280",
};

export async function getEntityGraph(
  corpusId: string,
  maxEntities = 50,
): Promise<GraphData> {
  // Fetch entities
  let entities;
  try {
    entities = await getExtractionEntities(corpusId, { limit: maxEntities });
  } catch (err) {
    // Neo4j disabled or error — return empty graph
    return { nodes: [], links: [], neo4jEnabled: false };
  }

  if (!entities.length) {
    return { nodes: [], links: [], neo4jEnabled: true };
  }

  // Fetch relations for all entities in parallel
  const relationResults = await Promise.all(
    entities.map((e) =>
      fetchJSON<RelationEdge[]>(
        `/corpora/${corpusId}/entities/${e.entity_id}/relations?limit=20`,
      ).catch(() => [] as RelationEdge[]),
    ),
  );
  const allRelations = relationResults.flat();

  // Build nodes
  const nodes = entities.map((e) => ({
    id: e.entity_id,
    name: e.display_name || e.normalized_name,
    entity_type: e.entity_type,
    mention_count: e.mention_count,
    color: ENTITY_COLORS[e.entity_type] ?? ENTITY_COLORS.other,
    val: Math.max(2, Math.min(e.mention_count, 15)),
  }));

  // Deduplicate links
  const seen = new Set<string>();
  const links = allRelations
    .filter((r) => {
      const key = [r.subject_id, r.object_id].sort().join(":");
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map((r) => ({
      source: r.subject_id,
      target: r.object_id,
      predicate: r.predicate,
      confidence: r.confidence,
    }));

  return { nodes, links, neo4jEnabled: true };
}

/**
 * POST /api/graph/query — Agent Query (PR 3 multi-corpus)
 * Backend extracts query entities per corpus, expands subgraph, finds
 * bridges/hubs/gaps, then merges across all selected corpora.
 */
export async function queryGraph(
  corpusIds: string[],
  query: string,
  maxHops: number = 2,
  limit: number = 50,
  opts: { seedLimitPerToken?: number } = {},
): Promise<GraphQueryResult> {
  return fetchJSON("/graph/query", {
    method: "POST",
    body: JSON.stringify({
      corpus_ids: corpusIds,
      query,
      max_hops: maxHops,
      limit,
      seed_limit_per_token: opts.seedLimitPerToken,
    }),
  });
}

// ─── PR 2 / PR 3 multi-corpus graph viewer endpoints ──────────────────────

export type GraphOverviewMultiResponse = {
  view: string;
  status: "ready" | "cache_warming";
  message?: string;
  nodes: Array<{
    id: string;
    display_name: string;
    entity_type: string;
    mention_count: number;
    supernode_type?: string;
    primary_domain?: string;
    top_entities?: string[];
    member_ids?: string[];
    bridge_count?: number;
    source_corpus?: string;
    source_corpora?: string[];
  }>;
  edges: Array<{
    source: string;
    target: string;
    predicate: string;
    confidence: number;
    weight: number;
    source_corpus?: string;
    source_corpora?: string[];
    dangling?: boolean;
  }>;
  truncated: boolean;
  raw_node_count: number;
  raw_edge_count: number;
  concept_count: number;
  domain_count: number;
  _meta?: {
    successful_ids: string[];
    failed_ids: string[];
    errors: Record<string, string>;
    cache_warming_corpora: string[];
  };
};

export type GraphFullMultiResponse = {
  nodes: Array<{
    id: string;
    display_name: string;
    entity_type: string;
    mention_count: number;
    object_kind?: string;
    canonical_family?: string;
    source_corpus?: string;
    source_corpora?: string[];
  }>;
  edges: Array<{
    source: string;
    target: string;
    predicate: string;
    relation_family?: string;
    confidence?: number;
    source_corpus?: string;
    source_corpora?: string[];
    dangling?: boolean;
  }>;
  truncated: boolean;
  _meta?: {
    successful_ids: string[];
    failed_ids: string[];
    errors: Record<string, string>;
    cache_warming_corpora: string[];
  };
};

export type CacheStatus = {
  corpus_id: string;
  domain_cache: "ready" | "warming" | "missing";
  metrics_cache: "ready" | "warming" | "missing";
  signature: string;
  last_built_at: string | null;
};

/**
 * POST /api/graph/overview — multi-corpus cached supernode overview.
 * Brain View's primary data source. Per-corpus cache misses surface as
 * `_meta.cache_warming_corpora`; the warm corpora still render.
 */
export async function getGraphOverviewMulti(
  corpusIds: string[],
  opts?: { max_concepts?: number; max_edges?: number },
): Promise<GraphOverviewMultiResponse> {
  return fetchJSON("/graph/overview", {
    method: "POST",
    body: JSON.stringify({
      corpus_ids: corpusIds,
      max_concepts: opts?.max_concepts ?? 80,
      max_edges: opts?.max_edges ?? 220,
    }),
  });
}

/**
 * POST /api/graph/full — multi-corpus full entity graph.
 * Used for drill into a cluster (after applying node_id filter on the
 * client) when /graph/cluster/{concept_id} isn't appropriate.
 */
export async function getGraphFullMulti(
  corpusIds: string[],
  opts?: { max_nodes?: number; max_edges?: number },
): Promise<GraphFullMultiResponse> {
  return fetchJSON("/graph/full", {
    method: "POST",
    body: JSON.stringify({
      corpus_ids: corpusIds,
      max_nodes: opts?.max_nodes ?? 20000,
      max_edges: opts?.max_edges ?? 60000,
    }),
  });
}

/**
 * POST /api/graph/cluster/{concept_id} — single concept-community drill.
 * Returns all entity nodes + RELATES_TO edges within the requested
 * community across the selected corpora.
 */
export async function getGraphCluster(
  conceptId: string,
  corpusIds: string[],
  opts?: { max_nodes?: number; max_edges?: number },
): Promise<GraphFullMultiResponse & { concept_id: string }> {
  return fetchJSON(`/graph/cluster/${encodeURIComponent(conceptId)}`, {
    method: "POST",
    body: JSON.stringify({
      corpus_ids: corpusIds,
      max_nodes: opts?.max_nodes ?? 5000,
      max_edges: opts?.max_edges ?? 20000,
    }),
  });
}

/**
 * GET /api/corpora/{cid}/cache-status — lightweight warming poll target.
 * Frontend polls every 15s while any selected corpus is warming.
 */
export async function getCorpusCacheStatus(corpusId: string): Promise<CacheStatus> {
  return fetchJSON(`/corpora/${encodeURIComponent(corpusId)}/cache-status`);
}

/**
 * POST /api/graph/cache/rebuild — manually trigger analytics cache build
 * for corpora whose cache is missing or stale. Returns immediately; the
 * actual emerge_domains run happens in a background asyncio.Task.
 */
export type GraphCacheRebuildResponse = {
  rebuilding: string[];
  already_running: string[];
  skipped: string[];
  errors: Record<string, string>;
};

export async function rebuildGraphCache(
  corpusIds: string[],
  opts?: { force?: boolean },
): Promise<GraphCacheRebuildResponse> {
  return fetchJSON("/graph/cache/rebuild", {
    method: "POST",
    body: JSON.stringify({
      corpus_ids: corpusIds,
      force: Boolean(opts?.force),
    }),
  });
}

/** GET /api/graph/cache/rebuild-status — which corpora are mid-rebuild. */
export async function getGraphCacheRebuildStatus(): Promise<{
  in_flight: string[];
  finished: string[];
}> {
  return fetchJSON("/graph/cache/rebuild-status");
}

// PR 4 fix — `getGraphByDocument` is already declared near line 1650 of
// this file; the GraphViewer imports it from there.

// ── Phase 19 Wave 2 — Cloud API key manager ────────────────────────────────

/**
 * GET /api/settings/api-keys → masked api keys.
 * Plaintext values NEVER come back from the backend; UI only sees masked
 * placeholders ("[not set]" or "sk-****abc4").
 */
export async function listApiKeys(): Promise<ApiKeysPublic> {
  return fetchJSON("/settings/api-keys");
}

/**
 * PUT /api/settings/api-keys → save plaintext keys (Fernet-encrypted at rest).
 * Pass empty string to clear a provider's key.
 */
export async function updateApiKeys(
  keys: Record<string, string>,
): Promise<ApiKeysPublic> {
  return fetchJSON("/settings/api-keys", {
    method: "PUT",
    body: JSON.stringify({ keys }),
  });
}

// ── Phase 19 Wave 1 — Ollama Model Manager ────────────────────────────────

export interface OllamaInstalled {
  name: string;
  size_bytes: number;
  size_human: string;
  modified_at: string | null;
  digest: string;
  details: Record<string, any>;
}

/**
 * GET /api/models/ollama/installed — list installed Ollama models.
 */
export async function listOllamaInstalled(): Promise<{
  models: OllamaInstalled[];
  count: number;
}> {
  return fetchJSON("/models/ollama/installed");
}

/**
 * DELETE /api/models/ollama — remove an Ollama model.
 */
export async function deleteOllamaModel(
  name: string,
): Promise<{ ok: boolean; deleted: string }> {
  return fetchJSON("/models/ollama", {
    method: "DELETE",
    body: JSON.stringify({ name }),
  });
}

/**
 * POST /api/models/ollama/pull — pull a model, streaming progress.
 *
 * Returns an async iterator of progress events. Consumer decides when to stop
 * (e.g. on `{status: "success"}` or `{error: ...}`). Uses fetch+ReadableStream
 * rather than EventSource because EventSource can't send Authorization headers.
 */
export async function* pullOllamaModel(
  name: string,
): AsyncGenerator<Record<string, any>, void, undefined> {
  const token = getPersistedToken();
  const resp = await fetch(`${API_BASE}/models/ollama/pull`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ name }),
  });
  if (!resp.ok || !resp.body) {
    const txt = await resp.text().catch(() => "");
    throw new Error(`HTTP ${resp.status}: ${txt}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE frames are separated by blank lines
    const frames = buf.split(/\r?\n\r?\n/);
    buf = frames.pop() || "";
    for (const frame of frames) {
      const lines = frame.split(/\r?\n/).filter((l) => l.startsWith("data:"));
      for (const line of lines) {
        const payload = line.slice(5).trim();
        if (!payload) continue;
        try {
          yield JSON.parse(payload);
        } catch {
          /* skip malformed */
        }
      }
    }
  }
}

/**
 * POST /api/graph/entity-search — Mode B entity-first retrieval.
 * Matches Entity nodes by name, returns chunks that mention them.
 * Ranked by summed MENTIONS.confidence; hydrated with parent text + corpus/doc.
 */
export async function entitySearch(
  query: string,
  opts: { corpusIds?: string[]; limit?: number; hydrate?: boolean } = {},
): Promise<EntitySearchResponse> {
  return fetchJSON("/graph/entity-search", {
    method: "POST",
    body: JSON.stringify({
      query,
      corpus_ids: opts.corpusIds ?? null,
      limit: opts.limit ?? 20,
      hydrate: opts.hydrate ?? true,
    }),
  });
}

/**
 * POST /api/graph/node-insight — read-only semantic neighborhood lookup.
 * Used by graph node clicks to map vector-nearest passages/documents/entities
 * beside the explicit graph edges. Does not mutate the graph.
 */
export async function getGraphNodeInsight(body: {
  corpusIds: string[];
  nodeId: string;
  label: string;
  entityType?: string | null;
  nodeKind?: string | null;
  topEntities?: string[];
  limit?: number;
}): Promise<GraphNodeInsightResponse> {
  return fetchJSON("/graph/node-insight", {
    method: "POST",
    body: JSON.stringify({
      corpus_ids: body.corpusIds,
      node_id: body.nodeId,
      label: body.label,
      entity_type: body.entityType ?? null,
      node_kind: body.nodeKind ?? null,
      top_entities: body.topEntities ?? [],
      limit: body.limit ?? 8,
    }),
  });
}

/**
 * POST /api/graph/analyze — Phase 17 Wave 3 LLM structural synthesis
 * Accepts a client-side snapshot of the current canvas(es) and returns a
 * markdown narrative that reads STRUCTURE (hubs/bridges/gaps/alignment),
 * never raw text.
 */
export async function analyzeGraph(
  body: GraphAnalyzeRequest,
): Promise<GraphAnalyzeResponse> {
  return fetchJSON("/graph/analyze", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * GET /api/corpora/{corpusId}/discourse — Phase 17 Wave 2 Discourse graph
 * Computes on-the-fly co-occurrence graph with cluster, bridge, gap, and
 * shape analysis from MongoDB chunks. No Neo4j required.
 */
export async function getDiscourseGraph(
  corpusId: string,
  opts: { topTerms?: number; minCooccur?: number; chunkLimit?: number } = {},
): Promise<DiscourseGraphResponse> {
  const qs = new URLSearchParams();
  if (opts.topTerms != null) qs.set("top_terms", String(opts.topTerms));
  if (opts.minCooccur != null) qs.set("min_cooccur", String(opts.minCooccur));
  if (opts.chunkLimit != null) qs.set("chunk_limit", String(opts.chunkLimit));
  const query = qs.toString() ? `?${qs}` : "";
  return fetchJSON(`/corpora/${corpusId}/discourse${query}`);
}

/**
 * GET /api/corpora/{corpusId}/entities
 * Search entities mentioned in this corpus. Optional doc_id narrows scope.
 */
export async function getExtractionEntities(
  corpusId: string,
  params?: { q?: string; limit?: number; doc_id?: string },
): Promise<EntityResult[]> {
  const qs = new URLSearchParams();
  if (params?.q) qs.set("q", params.q);
  if (params?.limit != null) qs.set("limit", String(params.limit));
  if (params?.doc_id) qs.set("doc_id", params.doc_id);
  const query = qs.toString() ? `?${qs}` : "";
  return fetchJSON(`/corpora/${corpusId}/entities${query}`);
}

/**
 * GET /api/corpora/{corpusId}/chunks/{chunkId}/extraction
 * All entities and relations for a single chunk.
 */
export async function getChunkExtraction(
  corpusId: string,
  chunkId: string,
): Promise<ChunkExtractionResponse> {
  return fetchJSON(`/corpora/${corpusId}/chunks/${chunkId}/extraction`);
}

/**
 * GET /api/corpora/{corpusId}/documents/{docId}/extraction
 * Per-chunk entity + relation counts for a document.
 */
export async function getDocExtraction(
  corpusId: string,
  docId: string,
): Promise<DocExtractionItem[]> {
  return fetchJSON(`/corpora/${corpusId}/documents/${docId}/extraction`);
}

/**
 * GET /api/corpora/{corpusId}/entities/{entityId}/relations
 * Outgoing + incoming RELATES_TO edges for an entity.
 */
export async function getEntityRelations(
  corpusId: string,
  entityId: string,
  limit = 20,
): Promise<RelationEdge[]> {
  return fetchJSON(
    `/corpora/${corpusId}/entities/${entityId}/relations?limit=${limit}`,
  );
}

// ============================================================================
// GLOBAL SETTINGS (Phase 10)
// ============================================================================

/**
 * GET /api/settings
 * Get global settings for the current user.
 * Infrastructure is read-only from .env, chat + retrieval are user-mutable.
 */
export async function getGlobalSettings(): Promise<GlobalSettingsResponse> {
  return fetchJSON("/settings");
}

/**
 * PUT /api/settings
 * Partial update of global settings (chat + retrieval sections only).
 */
export async function updateGlobalSettings(
  payload: GlobalSettingsUpdate,
): Promise<GlobalSettingsResponse> {
  return fetchJSON("/settings", {
    method: "PUT",
    body: JSON.stringify(payload),
  });
}

/**
 * POST /api/settings/infrastructure/test
 * Test connectivity to all infrastructure services.
 */
export async function testInfrastructure(): Promise<InfrastructureTestResponse> {
  return fetchJSON("/settings/infrastructure/test", {
    method: "POST",
  });
}

/**
 * POST /api/settings/infrastructure/test/{serviceName}
 * Test connectivity to a single infrastructure service.
 */
export async function testService(
  serviceName: string,
): Promise<{
  service: string;
  status: string;
  latency_ms: number;
  error?: string;
}> {
  return fetchJSON(`/settings/infrastructure/test/${serviceName}`, {
    method: "POST",
  });
}

// ── Phase 19.3 — Custom Model Profiles ─────────────────────────────────

/**
 * GET /api/model-profiles — list this user's custom profiles (masked keys).
 */
export async function listModelProfiles(): Promise<ModelProfilesListResponse> {
  return fetchJSON("/model-profiles");
}

/**
 * POST /api/model-profiles — create a new custom model profile.
 * Server encrypts `api_key` with Fernet before storage.
 */
export async function createModelProfile(
  body: ModelProfileCreate,
): Promise<ModelProfile> {
  return fetchJSON("/model-profiles", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/**
 * PUT /api/model-profiles/{id} — partial update.
 * Send `api_key: ""` (or omit) to leave the existing key unchanged.
 */
export async function updateModelProfile(
  profileId: string,
  patch: ModelProfileUpdate,
): Promise<ModelProfile> {
  return fetchJSON(`/model-profiles/${profileId}`, {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

/**
 * DELETE /api/model-profiles/{id}
 */
export async function deleteModelProfile(profileId: string): Promise<void> {
  const token = getPersistedToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(`${API_BASE}/model-profiles/${profileId}`, {
    method: "DELETE",
    headers,
  });
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
  }
}

/**
 * POST /api/model-profiles/{id}/test — ping the profile's endpoint.
 */
export async function testModelProfile(
  profileId: string,
): Promise<ModelProfileTestResult> {
  return fetchJSON(`/model-profiles/${profileId}/test`, { method: "POST" });
}

// ── Phase E — Unified Model Pool ───────────────────────────────────────

export async function listModelPool(): Promise<ModelPoolListResponse> {
  return fetchJSON("/model-pool");
}

export async function createModelPoolEntry(
  body: ModelPoolEntryCreate,
): Promise<ModelPoolEntry> {
  return fetchJSON("/model-pool", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function updateModelPoolEntry(
  entryId: string,
  patch: ModelPoolEntryUpdate,
): Promise<ModelPoolEntry> {
  return fetchJSON(`/model-pool/${entryId}`, {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

export async function deleteModelPoolEntry(entryId: string): Promise<void> {
  const token = getPersistedToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const resp = await fetch(`${API_BASE}/model-pool/${entryId}`, {
    method: "DELETE",
    headers,
  });
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`HTTP ${resp.status}: ${await resp.text()}`);
  }
}

export async function testModelPoolEntry(
  entryId: string,
): Promise<ModelPoolTestResult> {
  return fetchJSON(`/model-pool/${entryId}/test`, { method: "POST" });
}

// ─────────────────────────────────────────────────────────────────────────
// Phase F — Query Preferences (per-user role→pool mappings + ollama exclusions)
// ─────────────────────────────────────────────────────────────────────────

import type { QueryPrefs, QueryPrefsUpdate } from "../types/queryPrefs";

export async function getQueryPrefs(): Promise<QueryPrefs> {
  return fetchJSON("/query-prefs");
}

export async function updateQueryPrefs(
  patch: QueryPrefsUpdate,
): Promise<QueryPrefs> {
  return fetchJSON("/query-prefs", {
    method: "PUT",
    body: JSON.stringify(patch),
  });
}

// Export all as default object
export const api = {
  checkHealth,
  getModels,
  listModelPool,
  createModelPoolEntry,
  updateModelPoolEntry,
  deleteModelPoolEntry,
  testModelPoolEntry,
  getQueryPrefs,
  updateQueryPrefs,
  listModelProfiles,
  createModelProfile,
  updateModelProfile,
  deleteModelProfile,
  testModelProfile,
  listConversations,
  getConversation,
  getMessages,
  createConversation,
  updateConversation,
  deleteConversation,
  getCollections,
  streamChat,
  uploadFile,
  getIngestionStatus,
  listTools,
  createTool,
  updateTool,
  deleteTool,
  login,
  getMe,
  updateCredentials,
  listCorpora,
  getCorpus,
  createCorpus,
  updateCorpus,
  deleteCorpus,
  testIngestionModelRef,
  listDocuments,
  uploadDocumentToCorpus,
  createLocalIngestBatch,
  getIngestBatch,
  resumeIngestBatch,
  reconcileStaleIngestion,
  queryGraph,
  entitySearch,
  getDiscourseGraph,
  analyzeGraph,
  listOllamaInstalled,
  pullOllamaModel,
  deleteOllamaModel,
  listApiKeys,
  updateApiKeys,
  getExtractionEntities,
  getChunkExtraction,
  getDocExtraction,
  getEntityRelations,
  getGlobalSettings,
  updateGlobalSettings,
  testInfrastructure,
  testService,
  testModalEndpoint,
  verifyModalToken,
};

// ── Mission Control restored additive API helpers ──────────────────────────
export interface IngestOverrides {
  use_neo4j?: boolean;
  chunk_summarization?: boolean;
  model?: string;
  embed_mode?: "local" | "api" | "modal";
  embed_base_url?: string;
  embed_api_key?: string;
  embed_max_concurrent?: number;
  summary_model?: string;
  summary_base_url?: string;
  summary_api_key?: string;
  extraction_model?: string;
  extraction_base_url?: string;
  extraction_api_key?: string;
}

export interface FullGraphNode {
  id: string;
  display_name: string;
  entity_type: string;
  observed_entity_types?: string[] | null;
  mention_count: number;
  object_kind?: string | null;
  object_kind_parent?: string | null;
  object_kind_root?: string | null;
  domain_type?: string | null;
  domain_type_parent?: string | null;
  domain_type_root?: string | null;
  canonical_family?: string | null;
  ontology_version?: string | null;
  supernode_type?: "domain" | "concept" | string;
  primary_domain?: string | null;
  top_entities?: string[];
  bridge_count?: number;
  context_kind?: string;
  context_role?: string;
  topic_id?: string | null;
  evidence_count?: number;
  context_weight?: number;
}

export interface FullGraphEdge {
  source: string;
  target: string;
  predicate: string;
  relation_family?: string | null;
  confidence: number;
  weight?: number;
  role?: string;
  suggested?: boolean;
}

export interface FullGraphResponse {
  view?: "overview" | "full" | string;
  status?: "ready" | "cache_warming" | string;
  message?: string;
  nodes: FullGraphNode[];
  edges: FullGraphEdge[];
  truncated: boolean;
  raw_node_count?: number;
  raw_edge_count?: number;
  concept_count?: number;
  domain_count?: number;
}

export interface McpInfo {
  transport: string;
  url: string;
  port: number;
  host: string;
  require_auth: boolean;
  has_api_key: boolean;
  has_static_api_key?: boolean;
  has_user_api_key?: boolean;
  user_api_key_count?: number;
  supports_user_api_keys?: boolean;
  default_top_k: number;
  tools: Array<{ name: string; description: string }>;
}

export interface McpApiKeyPublic {
  key_id: string;
  name: string;
  prefix: string;
  created_at: string | null;
  last_used_at: string | null;
  revoked_at?: string | null;
  scope: "user" | string;
}

export interface McpApiKeyCreated {
  key_id: string;
  name: string;
  api_key: string;
  prefix: string;
  created_at: string;
  restart_required: boolean;
  scope: "user" | string;
}

export async function getModelsSettings(): Promise<import("../types").ModelsConfig> {
  return fetchJSON("/settings/models");
}

export async function updateModelsSettings(
  config: import("../types").ModelsConfig,
): Promise<import("../types").ModelsConfig> {
  return fetchJSON("/settings/models", { method: "POST", body: JSON.stringify(config) });
}

export async function testUtilityModel(): Promise<import("../types").UtilityModelTestResult> {
  return fetchJSON("/settings/models/utility/test", { method: "POST" });
}

export async function deletePoolEntry(entryId: string): Promise<import("../types").ModelsConfig> {
  return fetchJSON(`/settings/models/pool/${encodeURIComponent(entryId)}`, { method: "DELETE" });
}

export async function addOllamaToPool(modelNames: string[]): Promise<import("../types").ModelsConfig> {
  return fetchJSON("/settings/models/ollama/add", {
    method: "POST",
    body: JSON.stringify({ model_names: modelNames }),
  });
}

export async function listInstalledOllamaModels(): Promise<import("../types").OllamaInstalledModel[]> {
  const result = await listOllamaInstalled();
  return result.models || [];
}

export async function getModalStatus(): Promise<import("../types").ModalStatus> {
  return fetchJSON("/infrastructure/modal/status");
}

export async function deployModal(body: Record<string, unknown>): Promise<any> {
  return fetchJSON("/infrastructure/modal/deploy", {
    method: "POST",
    body: JSON.stringify(body || {}),
  });
}

export async function destroyModal(): Promise<any> {
  return fetchJSON("/infrastructure/modal/destroy", { method: "POST", body: JSON.stringify({}) });
}

async function* streamSse(path: string): AsyncGenerator<any, void, undefined> {
  const token = getPersistedToken();
  const resp = await fetch(`${API_BASE}${path}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok || !resp.body) {
    const txt = await resp.text().catch(() => "");
    throw new Error(`HTTP ${resp.status}: ${txt}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    const frames = buf.split("\n\n");
    buf = frames.pop() || "";
    for (const frame of frames) {
      const line = frame.split("\n").find((part) => part.startsWith("data:"));
      if (!line) continue;
      const raw = line.slice(5).trim();
      if (!raw || raw === "[DONE]") continue;
      yield JSON.parse(raw);
    }
  }
}

export function streamModalDeploy(): AsyncGenerator<any, void, undefined> {
  return streamSse("/infrastructure/modal/deploy/stream");
}

export function streamIngestionJob(
  docId: string,
  corpusId?: string,
): AsyncGenerator<any, void, undefined> {
  const query = corpusId ? `?corpus_id=${encodeURIComponent(corpusId)}` : "";
  return streamSse(`/ingestion/jobs/${encodeURIComponent(docId)}/stream${query}`);
}

export async function deleteDocument(corpusId: string, docId: string): Promise<{ success?: boolean }> {
  return fetchJSON(`/corpora/${encodeURIComponent(corpusId)}/documents/${encodeURIComponent(docId)}`, {
    method: "DELETE",
  });
}

export async function backfillDocumentGraph(
  corpusId: string,
  docId: string,
): Promise<{ status: string; failed_chunks?: number; [key: string]: unknown }> {
  return fetchJSON(`/corpora/${encodeURIComponent(corpusId)}/documents/${encodeURIComponent(docId)}/graph-backfill`, {
    method: "POST",
  });
}

export async function warmGraphCache(corpusId: string): Promise<Record<string, unknown>> {
  return fetchJSON(`/corpora/${encodeURIComponent(corpusId)}/graph-cache/warm`, { method: "POST" });
}

export async function listSkills(): Promise<import("../types").Skill[]> {
  return fetchJSON("/skills");
}

export async function createSkill(body: import("../types").SkillCreate): Promise<import("../types").Skill> {
  return fetchJSON("/skills", { method: "POST", body: JSON.stringify(body) });
}

export async function updateSkill(
  skillId: string,
  body: import("../types").SkillUpdate,
): Promise<import("../types").Skill> {
  return fetchJSON(`/skills/${encodeURIComponent(skillId)}`, { method: "PATCH", body: JSON.stringify(body) });
}

export async function deleteSkill(skillId: string): Promise<{ success: boolean }> {
  return fetchJSON(`/skills/${encodeURIComponent(skillId)}`, { method: "DELETE" });
}

export async function getMcpInfo(): Promise<McpInfo> {
  return fetchJSON("/mcp/info");
}

export async function listMcpApiKeys(): Promise<{ keys: McpApiKeyPublic[] }> {
  return fetchJSON("/mcp/api-keys");
}

export async function createMcpApiKey(name?: string): Promise<{ key: McpApiKeyCreated }> {
  return fetchJSON("/mcp/api-keys", {
    method: "POST",
    body: JSON.stringify({ name: name || "MCP key" }),
  });
}

export async function revokeMcpApiKey(keyId: string): Promise<{ key_id: string; revoked: boolean }> {
  return fetchJSON(`/mcp/api-keys/${encodeURIComponent(keyId)}`, {
    method: "DELETE",
  });
}

export interface PortabilityImportResponse {
  status: "ok";
  stats: {
    mongo_documents?: Record<string, number>;
    qdrant_points?: Record<string, number>;
    neo4j_nodes?: number;
    neo4j_relationships?: number;
  };
}

export async function downloadPortabilityArchive(): Promise<Blob> {
  const token = getPersistedToken();
  const response = await fetch(`${API_BASE}/portability/export`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(`HTTP ${response.status}: ${error}`);
  }
  return response.blob();
}

export async function uploadPortabilityArchive(
  file: File,
): Promise<PortabilityImportResponse> {
  const token = getPersistedToken();
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${API_BASE}/portability/import`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  });
  if (!response.ok) {
    const error = await response.text();
    throw new Error(`HTTP ${response.status}: ${error}`);
  }
  return response.json();
}

export async function discoverGraph(
  body: import("../types").GraphDiscoverRequest,
): Promise<import("../types").GraphDiscoverResponse> {
  return fetchJSON("/graph/discover", { method: "POST", body: JSON.stringify(body) });
}

export async function listGraphSessions(corpusId?: string | null): Promise<import("../types").GraphDiscoverSession[]> {
  const qs = corpusId ? `?corpus_id=${encodeURIComponent(corpusId)}` : "";
  return fetchJSON(`/graph/sessions${qs}`);
}

export async function getGraphSession(sessionId: string): Promise<import("../types").GraphDiscoverSessionDetail> {
  return fetchJSON(`/graph/sessions/${encodeURIComponent(sessionId)}`);
}

export async function findGraphResumeCandidate(
  body: import("../types").GraphResumeCandidateRequest,
): Promise<import("../types").GraphResumeCandidateResponse> {
  return fetchJSON("/graph/resume-candidate", { method: "POST", body: JSON.stringify(body) });
}

export async function getGraphSuggestions(corpusIds: string | string[]): Promise<import("../types").GraphSuggestionsResponse> {
  if (Array.isArray(corpusIds)) {
    const ids = corpusIds.map((id) => id.trim()).filter(Boolean);
    if (ids.length === 0) throw new Error("corpus_id required");
    const qs = new URLSearchParams({ corpus_ids: ids.join(",") });
    return fetchJSON(`/graph/suggestions?${qs.toString()}`);
  }
  return fetchJSON(`/graph/suggestions?corpus_id=${encodeURIComponent(corpusIds)}`);
}

export async function deleteGraphSession(sessionId: string): Promise<{ success?: boolean }> {
  return fetchJSON(`/graph/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
}

export async function getGraphOverview(
  corpusId: string,
  maxConcepts = 80,
  maxEdges = 220,
): Promise<FullGraphResponse> {
  const qs = new URLSearchParams({ max_concepts: String(maxConcepts), max_edges: String(maxEdges) });
  return fetchJSON(`/corpora/${encodeURIComponent(corpusId)}/graph/overview?${qs}`);
}

export async function getFullCorpusGraph(
  corpusId: string,
  maxNodes = 20000,
  maxEdges = 60000,
): Promise<FullGraphResponse> {
  const qs = new URLSearchParams({ max_nodes: String(maxNodes), max_edges: String(maxEdges) });
  return fetchJSON(`/corpora/${encodeURIComponent(corpusId)}/graph/full?${qs}`);
}

// ─── Books-as-clusters graph (multi-corpus) ─────────────────────────
//
// POST /api/graph/by-document — three modes:
//   "overview" → just clusters (no nodes/edges) for 100s+ of books
//   "drill"    → expand one cluster + its bridge neighbours
//   "full"     → every node/edge across the requested corpora
//
// See backend/services/graph/neo4j_reader.py for the response shape.
export interface ByDocumentCluster {
  cluster_id: string;
  corpus_id: string;
  label?: string;
  entity_count: number;
  total_mentions?: number;
  top_entities?: string[];
  top_entity_names?: string[];
  ghost_b_success_rate?: number | null;
  ghost_b_extracted?: number | null;
  ghost_b_total?: number | null;
}

export interface ByDocumentNode {
  id: string;
  display_name: string;
  entity_type?: string;
  primary_doc_id: string;
  bridge_doc_ids: string[];
  total_mentions: number;
  per_doc_mentions: Record<string, number>;
}

export interface ByDocumentEdge {
  source: string;
  target: string;
  predicate: string;
  relation_family?: string;
  confidence?: number | null;
  cross_cluster?: boolean;
}

export interface ByDocumentGraphResponse {
  mode: "overview" | "drill" | "full";
  clusters: ByDocumentCluster[];
  nodes: ByDocumentNode[];
  edges: ByDocumentEdge[];
  truncated: boolean;
}

export async function getGraphByDocument(opts: {
  corpusIds: string[];
  mode?: "overview" | "drill" | "full";
  drillDocId?: string;
  minEntityMentions?: number;
  maxNodes?: number;
  maxEdges?: number;
  topEntitiesPerCluster?: number;
}): Promise<ByDocumentGraphResponse> {
  return fetchJSON("/graph/by-document", {
    method: "POST",
    body: JSON.stringify({
      corpus_ids: opts.corpusIds,
      mode: opts.mode ?? "overview",
      drill_doc_id: opts.drillDocId,
      min_entity_mentions: opts.minEntityMentions ?? 2,
      max_nodes: opts.maxNodes ?? 20000,
      max_edges: opts.maxEdges ?? 60000,
      top_entities_per_cluster: opts.topEntitiesPerCluster ?? 200,
    }),
  });
}

// ─── Brain View (anchor-driven, pure Cypher) ────────────────────────
//
// POST /api/graph/brain-view — returns :Document cluster anchors + bridge
// strengths derived from shared Entity mentions. Anchor metadata (filename,
// chunk_count, ghost_b_*) lives on the Document node directly, so the
// response needs zero MongoDB enrichment. Pairs with POST /api/graph/
// book-drilldown for click-to-drill.

export interface BrainViewBridge {
  target_doc_id: string;
  target_filename?: string;
  target_corpus_id?: string;
  shared_entities: number;
  strength: number;
  // Pt 5: pass-through from backend Cypher; same as the flattened version.
  dominant_relation_family?: string | null;
  // Pt 7c: top 3 distinct shared concept names — drives the on-edge label
  // so users see what connects two books without clicking.
  top_shared_entities?: string[];
}

export interface BrainViewDocument {
  doc_id: string;
  corpus_id: string;
  label: string;
  filename?: string | null;
  kind: string;
  chunk_count: number;
  parent_count: number;
  actual_chunk_count: number;
  // Pt 5: extraction-schema facets surfaced for deterministic frontend coloring.
  // `dominant_family` = top canonical_family by entity mention across the book.
  // `dominant_entity_type` = fallback when canonical_family is sparse.
  dominant_family?: string | null;
  dominant_entity_type?: string | null;
  ghost_b_success_rate?: number | null;
  ghost_b_extracted?: number | null;
  ghost_b_total?: number | null;
  schema_lens_id?: string | null;
  source_tier?: string | null;
  ingested_at?: string | null;
  updated_at?: string | null;
  bridge_count: number;
  bridges: BrainViewBridge[];
  // Octopus mode — top distinct entity names per anchor (capped at 8 by
  // the backend Cypher) + the full entity_count for that anchor.
  // GraphViewer reads `top_entities` to spawn satellite nodes for the
  // spotlight (top-bridge-count) books; `entity_count` is informational.
  entity_count?: number;
  top_entities?: string[];
}

export interface BrainViewFlatBridge {
  source: string;
  source_corpus_id?: string;
  target: string;
  target_corpus_id?: string;
  strength: number;
  shared_entities: number;
  // Pt 5: dominant RELATES_TO.relation_family across this bridge. Drives
  // edge color (EDGE_COLORS_BY_FAMILY) on the frontend canvas.
  dominant_relation_family?: string | null;
  // Pt 7c: top 3 distinct shared concept names. Drives the on-edge label.
  top_shared_entities?: string[];
}

export interface BrainViewResponse {
  documents: BrainViewDocument[];
  bridges: BrainViewFlatBridge[];
  meta: {
    corpus_count: number;
    total_documents: number;
    total_bridges: number;
    limit_applied: number;
    error?: string;
    partial?: boolean;
  };
}

export async function getBrainView(
  corpusIds: string[],
  limit = 2000,
): Promise<BrainViewResponse> {
  return fetchJSON("/graph/brain-view", {
    method: "POST",
    body: JSON.stringify({ corpus_ids: corpusIds, limit }),
  });
}

export interface BookDrilldownEntity {
  entity_id: string;
  display_name: string;
  entity_type: string;
  object_kind?: string | null;
  canonical_family?: string | null;
}

export interface BookDrilldownRelation {
  source_id: string;
  target_id: string;
  predicate: string;
  relation_family: string;
  confidence?: number | null;
}

export interface BookDrilldownCrossBridge {
  via_entity_id: string;
  bridge_entity_id: string;
  bridge_entity_name?: string;
  target_doc_id: string;
  target_filename?: string;
  target_corpus_id?: string;
  strength: number;
}

export interface BookDrilldownResponse {
  anchor: Record<string, any> | null;
  local_entities: BookDrilldownEntity[];
  local_relations: BookDrilldownRelation[];
  cross_book_bridges: BookDrilldownCrossBridge[];
  meta: {
    found: boolean;
    local_entity_count?: number;
    local_relation_count?: number;
    bridge_count?: number;
    limit?: number;
    error?: string;
    partial?: boolean;
  };
}

export async function getBookDrilldown(
  docId: string,
  otherCorpusIds: string[],
  limit = 350,
): Promise<BookDrilldownResponse> {
  return fetchJSON("/graph/book-drilldown", {
    method: "POST",
    body: JSON.stringify({
      doc_id: docId,
      other_corpus_ids: otherCorpusIds,
      limit,
    }),
  });
}

// ─── Pt 7 — HyDE-style query refinement + entity-type search ───────────
//
// Both clients implement client-side IDEMPOTENCY at two layers:
//   (a) In-flight de-duplication — if a request with the same key is
//       already pending, return the SAME promise instead of firing a
//       second one. Stops double-clicks from doubling cost.
//   (b) Short-TTL result cache — for the next N seconds after a
//       successful result, return the cached payload instead of fetching
//       again. Tighter than the backend's 24h cache because the backend
//       is already doing the heavy lifting; this is just UX smoothing.
//
// The backend `/api/graph/refine` is itself idempotent (Mongo cache
// keyed by hash(question + corpus_ids + model)) — these client-side
// layers are belt-and-suspenders so we don't ship even a network round
// trip on a known-recent request.

export interface RefinementResult {
  alternative_phrasings: string[];
  opposing_framings: string[];
  related_questions: string[];
}

export interface ContextualQuestions {
  rag: string[];
  research: string[];
  nuance: string[];
  ideation: string[];
}

export interface ConceptQuestionPacket {
  matched_entities: Array<{
    name: string;
    type: string;
    mentions: number;
  }>;
  nearby_relations: Array<Record<string, unknown>>;
  source_hints: Array<Record<string, unknown>>;
}

/** Pt 7b: entity extracted from the user's question via
 *  extract_query_entities Cypher. Same shape as EntityRow (below) so the
 *  frontend can reuse rendering helpers. `score` is a rough token-overlap
 *  metric the backend computes; clients can use it to sort. */
export interface ExtractedEntity {
  entity_id: string;
  display_name: string;
  entity_type: string;
  mention_count: number;
  score?: number;
  source_corpora?: string[];
}

export interface RefineQueryResponse {
  idempotency_key: string;
  cached: boolean;
  result: RefinementResult;
  /** Pt 7b: in-corpus entities matched against the question. NOT cached
   *  on the backend (recomputed every call) so it reflects fresh graph
   *  state. Returned even on cache hits for the refinement portion. */
  entities: ExtractedEntity[];
  contextual_questions?: ContextualQuestions;
  contextual_cached?: boolean;
  contextual_error?: string;
  contextual_source?: "llm" | "cache" | "local_fallback" | string;
  concept_packet?: ConceptQuestionPacket;
  context_signature?: string;
  error?: string;
}

// In-flight + result caches (module-scoped — survive component re-mounts).
const _refineInflight = new Map<string, Promise<RefineQueryResponse>>();
const _refineCache = new Map<string, { at: number; value: RefineQueryResponse }>();
const REFINE_RESULT_TTL_MS = 5 * 60 * 1000; // 5 minutes

function _refineKey(
  question: string,
  corpusIds: string[],
  model?: string,
  includeContextual = false,
): string {
  const normalized = question.trim().toLowerCase().replace(/\s+/g, " ");
  const ids = [...corpusIds].sort().join(",");
  return `${normalized}|${ids}|${model ?? ""}|context:${includeContextual ? "1" : "0"}`;
}

export async function refineQuery(
  question: string,
  corpusIds: string[],
  model?: string,
  forceRefresh = false,
  includeContextual = false,
): Promise<RefineQueryResponse> {
  const key = _refineKey(question, corpusIds, model, includeContextual);

  // (a) Result cache hit (skipped on forceRefresh).
  if (!forceRefresh) {
    const cached = _refineCache.get(key);
    if (cached && Date.now() - cached.at < REFINE_RESULT_TTL_MS) {
      return cached.value;
    }
  }

  // (b) In-flight dedupe — return the same promise if one's pending.
  const pending = _refineInflight.get(key);
  if (pending) return pending;

  const fire = (async () => {
    try {
      const res = await fetchJSON<RefineQueryResponse>("/graph/refine", {
        method: "POST",
        body: JSON.stringify({
          question,
          corpus_ids: corpusIds,
          model,
          force_refresh: forceRefresh,
          include_contextual: includeContextual,
        }),
      });
      _refineCache.set(key, { at: Date.now(), value: res });
      return res;
    } finally {
      _refineInflight.delete(key);
    }
  })();
  _refineInflight.set(key, fire);
  return fire;
}

// ─── Entity-type search (Pt 7 Graph Query tab) ──────────────────────────
// Wraps the existing GET /api/corpora/{id}/entities with the same
// in-flight + result-cache pattern. Listings are stable per corpus
// snapshot so a short TTL is plenty.

export interface EntityRow {
  entity_id: string;
  normalized_name: string;
  display_name: string;
  entity_type: string;
  confidence?: number | null;
  mention_count: number;
}

const _entityInflight = new Map<string, Promise<EntityRow[]>>();
const _entityCache = new Map<string, { at: number; value: EntityRow[] }>();
const ENTITY_RESULT_TTL_MS = 30 * 1000; // 30 seconds

function _entityKey(
  corpusId: string,
  q: string,
  entityType: string,
  limit: number,
): string {
  return `${corpusId}|${q.trim().toLowerCase()}|${entityType}|${limit}`;
}

export async function searchEntities(
  corpusId: string,
  opts: { q?: string; entityType?: string; limit?: number } = {},
): Promise<EntityRow[]> {
  const q = opts.q ?? "";
  const entityType = opts.entityType ?? "";
  const limit = opts.limit ?? 20;
  const key = _entityKey(corpusId, q, entityType, limit);

  const cached = _entityCache.get(key);
  if (cached && Date.now() - cached.at < ENTITY_RESULT_TTL_MS) {
    return cached.value;
  }

  const pending = _entityInflight.get(key);
  if (pending) return pending;

  const params = new URLSearchParams();
  if (q) params.set("q", q);
  params.set("limit", String(limit));

  const fire = (async () => {
    try {
      const rows = await fetchJSON<EntityRow[]>(
        `/corpora/${encodeURIComponent(corpusId)}/entities?${params.toString()}`,
      );
      // Backend doesn't filter by type today — filter client-side. Adding
      // `?type=` to the backend is a one-line Cypher tweak; for v1 we
      // just narrow on the result.
      const filtered = entityType
        ? rows.filter((r) => r.entity_type === entityType)
        : rows;
      _entityCache.set(key, { at: Date.now(), value: filtered });
      return filtered;
    } finally {
      _entityInflight.delete(key);
    }
  })();
  _entityInflight.set(key, fire);
  return fire;
}
