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
  DocumentResponse,
  IngestJobResponse,
  IngestionBatchResponse,
  IngestionResourceProfile,
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
    // Dynamic import avoids circular dep at module load; getState() for non-React context
    if (response.status === 401) {
      const { useAuthStore } = await import("../stores/authStore");
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
  try {
    return await fetchJSON("/collections");
  } catch (error) {
    if (error instanceof Error && error.message.includes("HTTP 404")) {
      // v3 corpus-scoped retrieval no longer requires the legacy collections
      // endpoint. Keep the old selector harmless while the UI migration settles.
      return [];
    }
    throw error;
  }
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
      const { useAuthStore } = await import("../stores/authStore");
      useAuthStore.getState().clearAuth();
      throw new Error("Session expired. Please log in again.");
    }
    const error = await response.text();
    throw new Error(`HTTP ${response.status}: ${error}`);
  }

  return response.json();
}

/**
 * POST /api/corpora/{corpus_id}/batch-ingest
 * Spool many files and enqueue durable background ingestion.
 */
export async function batchUploadDocumentsToCorpus(
  corpusId: string,
  files: File[],
  options?: {
    use_neo4j?: boolean;
    chunk_summarization?: boolean;
    model?: string;
  },
): Promise<IngestionBatchResponse> {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }
  if (options?.use_neo4j !== undefined)
    formData.append("use_neo4j", String(options.use_neo4j));
  if (options?.chunk_summarization !== undefined)
    formData.append("chunk_summarization", String(options.chunk_summarization));
  if (options?.model) formData.append("model", options.model);

  const token = getPersistedToken();
  const headers: Record<string, string> = {};
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const response = await fetch(`${API_BASE}/corpora/${corpusId}/batch-ingest`, {
    method: "POST",
    headers,
    body: formData,
  });

  if (!response.ok) {
    if (response.status === 401) {
      const { useAuthStore } = await import("../stores/authStore");
      useAuthStore.getState().clearAuth();
      throw new Error("Session expired. Please log in again.");
    }
    const error = await response.text();
    throw new Error(`HTTP ${response.status}: ${error}`);
  }

  return response.json();
}

export async function getIngestionBatch(
  batchId: string,
): Promise<IngestionBatchResponse> {
  return fetchJSON(`/ingestion/batches/${encodeURIComponent(batchId)}`);
}

export async function pauseIngestionBatch(
  batchId: string,
): Promise<IngestionBatchResponse> {
  return fetchJSON(`/ingestion/batches/${encodeURIComponent(batchId)}/pause`, {
    method: "POST",
  });
}

export async function resumeIngestionBatch(
  batchId: string,
): Promise<IngestionBatchResponse> {
  return fetchJSON(`/ingestion/batches/${encodeURIComponent(batchId)}/resume`, {
    method: "POST",
  });
}

export async function cancelIngestionBatch(
  batchId: string,
): Promise<IngestionBatchResponse> {
  return fetchJSON(`/ingestion/batches/${encodeURIComponent(batchId)}/cancel`, {
    method: "POST",
  });
}

export async function retryFailedIngestionBatch(
  batchId: string,
): Promise<IngestionBatchResponse> {
  return fetchJSON(
    `/ingestion/batches/${encodeURIComponent(batchId)}/retry-failed`,
    { method: "POST" },
  );
}

export async function getIngestionResourceProfile(): Promise<IngestionResourceProfile> {
  return fetchJSON("/ingestion/resource-profile");
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
 * POST /api/graph/query — Phase 17 Wave 1 Agent Query
 * Backend extracts query entities, expands N-hop subgraph, finds bridges/hubs/gaps.
 * Returns everything the GraphView canvas + DiscoveryPanel need.
 */
export async function queryGraph(
  corpusId: string,
  query: string,
  maxHops: number = 2,
  limit: number = 50,
): Promise<GraphQueryResult> {
  return fetchJSON("/graph/query", {
    method: "POST",
    body: JSON.stringify({
      corpus_id: corpusId,
      query,
      max_hops: maxHops,
      limit,
    }),
  });
}

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
  listDocuments,
  uploadDocumentToCorpus,
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
  default_top_k: number;
  tools: Array<{ name: string; description: string }>;
}

export async function getModelsSettings(): Promise<import("../types").ModelsConfig> {
  return fetchJSON("/settings/models");
}

export async function updateModelsSettings(
  config: import("../types").ModelsConfig,
): Promise<import("../types").ModelsConfig> {
  return fetchJSON("/settings/models", { method: "POST", body: JSON.stringify(config) });
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

export async function recoverDocumentVectors(
  corpusId: string,
  docId: string,
): Promise<{ status: string; vector_ready?: boolean; qdrant_written?: boolean; [key: string]: unknown }> {
  return fetchJSON(`/corpora/${encodeURIComponent(corpusId)}/documents/${encodeURIComponent(docId)}/vector-recovery`, {
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

export async function getGraphSuggestions(corpusId: string): Promise<import("../types").GraphSuggestionsResponse> {
  return fetchJSON(`/graph/suggestions?corpus_id=${encodeURIComponent(corpusId)}`);
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
