// Chat-related types for Polymath RAG v3.3

export type RetrievalTier =
  | "qdrant_only"
  | "qdrant_mongo"
  | "qdrant_mongo_graph";

export interface GraphProvenance {
  entity: string;
  confidence: number;
  predicate?: string;
  relation_family?: string | null;
}

// ── Phase 17 Wave 1 — Agent Query discovery types ─────────────────────────

export interface GraphQueryNode {
  id: string;
  display_name: string;
  entity_type: string;
  // Classification surfaced in the node inspector ("what is this").
  primary_entity_type?: string | null;
  definitional_phrase?: string | null;
  observed_entity_types?: string[] | null;
  canonical_family?: string | null;
  confidence?: number | null;
  mention_count: number;
  is_seed: boolean;
}

export interface GraphQueryLink {
  source: string;
  target: string;
  predicate: string;
  relation_family?: string | null;
  confidence: number;
}

export interface GraphBridge {
  entity_id: string;
  display_name: string;
  entity_type: string;
  connected_seed_count: number;
  connected_seeds: string[];
}

export interface GraphHub {
  entity_id: string;
  display_name: string;
  entity_type: string;
  degree: number;
  is_seed: boolean;
}

export interface GraphGap {
  entity_a_id: string;
  entity_a_name: string;
  entity_b_id: string;
  entity_b_name: string;
}

export interface GraphQueryResult {
  nodes: GraphQueryNode[];
  links: GraphQueryLink[];
  bridges: GraphBridge[];
  hubs: GraphHub[];
  gaps: GraphGap[];
  seed_entities: GraphQueryNode[];
}

// ── Phase 17 Wave 2 — Discourse graph types ───────────────────────────────

export interface DiscourseNode {
  id: string;
  label: string;
  freq: number;
  type: "lexeme";
  cluster: number | null;
}

export interface DiscourseLink {
  source: string;
  target: string;
  weight: number;
}

export interface DiscourseGraphData {
  nodes: DiscourseNode[];
  links: DiscourseLink[];
}

export interface DiscourseCluster {
  cluster_id: number;
  size: number;
  top_terms: string[];
}

export interface DiscourseBridge {
  term: string;
  centrality: number;
  connects_clusters: number[];
  degree: number;
}

export interface DiscourseGap {
  cluster_a: number;
  cluster_b: number;
  bridging_words: string[];
  bridging_count: number;
  severity: "DISCONNECTED" | "THIN";
  interpretation: string;
}

export interface DiscourseShape {
  shape: "CONCENTRATED" | "SKEWED" | "DISPERSED" | "BALANCED" | "EMPTY";
  shape_description: string;
  gini_coefficient: number;
  cluster_proportions: Record<string, number>;
  dominant_cluster: number | null;
  dominant_percentage: number;
  top_words_by_degree: Array<{ term: string; degree: number }>;
}

export interface DiscourseGraphResponse {
  graph: DiscourseGraphData;
  chunk_count: number;
  clusters: DiscourseCluster[];
  bridges: DiscourseBridge[];
  gaps: DiscourseGap[];
  shape: DiscourseShape;
}

// ── Phase 19.2 — Cloud API key manager ────────────────────────────────────

export interface ApiKeysPublic {
  /** map of provider → masked value ("[not set]" or "sk-****abc4") */
  keys: Record<string, string>;
  /** all known provider names available in the UI */
  providers: string[];
}

export interface ApiKeysUpdate {
  /** map of provider → plaintext key. Empty value clears that provider. */
  keys: Record<string, string>;
}

// ── Phase 17 Wave 3 — Graph analyzer (LLM structural synthesis) ───────────

export type GraphAnalyzeMode = "knowledge" | "discourse" | "split";

export interface GraphAnalyzeKnowledgeSnapshot {
  nodes: Record<string, any>[];
  links: Record<string, any>[];
  seed_ids: string[];
}

export interface GraphAnalyzeDiscourseSnapshot {
  nodes: Record<string, any>[];
  links: Record<string, any>[];
  clusters: Record<string, any>[];
  bridges: Record<string, any>[];
  gaps: Record<string, any>[];
  shape: Record<string, any>;
}

export interface GraphAnalyzeRequest {
  corpus_id: string;
  mode: GraphAnalyzeMode;
  query?: string | null;
  model?: string | null;
  knowledge?: GraphAnalyzeKnowledgeSnapshot;
  discourse?: GraphAnalyzeDiscourseSnapshot;
}

export interface SplitOverlayAlignment {
  intersection: string[];
  intersection_size: number;
  union_size: number;
  score: number;
  entities_present_as_lexemes: string[];
  entities_absent_from_lexemes: string[];
}

export interface SplitOverlay {
  nodes: Record<string, any>[];
  links: Record<string, any>[];
  alignment: SplitOverlayAlignment;
  crosslinks_count: number;
}

export interface GraphAnalyzeResponse {
  mode: GraphAnalyzeMode;
  markdown: string;
  structural_summary: Record<string, any>;
  overlay?: SplitOverlay | null;
  handoff_prompt: string;
}

export interface SourceChunk {
  chunk_id: string;
  parent_id: string;
  doc_id: string;
  corpus_id: string;
  text: string;
  summary?: string | null;
  score: number;
  source_tier: string;
  chunk_kind?: string | null;
  corpus_name?: string | null;
  doc_name?: string | null;
  heading_path?: string[] | null;
  metadata?: Record<string, unknown> | null;
  /** Phase 16.1 — graph expansion provenance (Mode A / Mode B only) */
  provenance?: GraphProvenance[] | null;
}

/** Mode B entity-first search response. */
export interface EntitySearchResponse {
  chunks: SourceChunk[];
  neo4j_enabled: boolean;
}

export interface GraphNodeInsightDocument {
  doc_id: string;
  doc_name: string;
  corpus_id: string;
  corpus_name: string;
  count: number;
  best_score: number;
}

export interface GraphNodeInsightRelatedEntity {
  name: string;
  predicate?: string;
  relation_family?: string;
  confidence?: number;
  count: number;
}

export interface GraphNodeInsightResponse {
  query: string;
  chunks: SourceChunk[];
  documents: GraphNodeInsightDocument[];
  related_entities: GraphNodeInsightRelatedEntity[];
  effective_tier: string;
  downgrade_reason?: string | null;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  thinking?: string;
  trace_events?: TraceEvent[];
  process_timeline?: ProcessTimelineItem[];
  model_used?: string;
  token_count?: number;
  created_at: string;
  trimming_applied?: boolean;
  collections_queried?: string[];
  metadata?: Record<string, unknown>;
  // Trust-signal fields (Sprint — RetrievalBadge). All optional so
  // legacy messages without them deserialize cleanly. The badge's
  // state-derivation treats `chunks_returned === undefined` (with
  // a populated `collections_queried`) as "RAG_GROUNDED, unknown
  // chunk count" — see RetrievalBadge.tsx for the full table.
  chunks_returned?: number;
  /** Raw effective_tier from the backend retrieval pipeline.
   *  "qdrant_only" | "qdrant_mongo" | "qdrant_mongo_graph". Frontend
   *  humanizes via TIER_LABELS in RetrievalBadge. */
  strategy_used?: string;
  query_profile_used?: string;
  reasoning_mode_used?: string;
  hyde_applied?: boolean;
  agentic_mode_used?: boolean;
  downgrade_reason?: string | null;
  // Phase 24 — trust signals for skills + tools + reasoning cascade
  skills_used?: string[];
  tools_used?: string[];
  reasoning_cascade_applied?: boolean;
  /** Source chunks captured from the SSE `sources` frame and attached on
   *  finalize. The backend now persists compact source previews, so reloads
   *  can still show source receipts even when full chunk bodies are compacted. */
  sources?: SourceChunk[];
}

export interface ProcessTimelineItem {
  id: string;
  kind: "trace" | "reasoning" | "tool";
  title: string;
  status?: "running" | "done" | "error" | "skipped" | string;
  content?: string;
  detail?: string;
  timestamp: string;
  metadata?: Record<string, unknown>;
}

export interface TraceEvent {
  id: string;
  lane:
    | "reasoning"
    | "model_call"
    | "retrieval"
    | "plan"
    | "tool_call"
    | "tool_result"
    | "warning"
    | "final"
    | string;
  title: string;
  status?: "running" | "done" | "error" | "skipped" | string;
  content?: string;
  timestamp: string;
  metadata?: Record<string, unknown>;
}

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_preview?: string;
  model_config?: ModelConfig;
  messages?: ChatMessage[];
}

export interface ModelConfig {
  model: string;
  temperature: number;
  top_p: number;
  max_tokens: number;
}

export interface ChatOverrides extends Partial<ModelConfig> {
  hyde_enabled?: boolean;
  /** Opt-in live web context for this turn. Omit/false keeps chat corpus-only. */
  web_search_enabled?: boolean;
  /** Live-web fetch depth. snippets = search only, normal = static fetch,
   *  deep = static fetch plus Obscura fallback for allowlisted JS pages. */
  web_fetch_depth?: "snippets" | "normal" | "deep";
  /** Expands bounded live-web budgets for deeper research turns. */
  web_research_mode?: boolean;
  /** Allows YouTube URLs to be converted into transcript evidence. */
  web_youtube_transcripts?: boolean;
  /** Max final web evidence sources requested for this turn. */
  web_max_sources?: number;
  collection_ids?: string[];
  agentic_mode?: boolean;
  agentic_model?: string;
  reasoning_mode?: string;
  reasoning_blend?: string[];
  /** Phase 17 — per-request HyDE model override */
  hyde_model?: string;
  /** Phase 18 — Query Profile speed preset and overrides */
  query_profile?: "fast" | "balanced" | "thorough" | "custom";
  retrieval_k?: number;
  rerank_enabled?: boolean;
  top_k_summary?: number;
  rerank_top_n?: number;
  similarity_threshold?: number;
  neo4j_expansion_cap?: number;
  max_corpora_per_query?: number;
  final_top_k?: number;
  fact_seed_limit?: number;
  /** Phase 27 — search-mode dispatch. "auto" lets the backend infer
   *  local vs global from query shape; "local" forces the full
   *  vector+BM25+graph+rerank+hydrate path; "global" returns
   *  summary-only chunks for thematic / corpus-wide queries. Omit
   *  to use the server default (auto). */
  search_mode?: "auto" | "local" | "global";
  /** Phase 28 — thinking / reasoning-effort dial. Mapped server-side
   *  to provider-native params (OpenAI reasoning_effort, Anthropic
   *  thinking budget, Gemini thinking_budget). Ignored for models
   *  that don't expose a dial. Omit when "auto" — server picks the
   *  per-provider default. */
  thinking_effort?: "auto" | "none" | "low" | "medium" | "high";
}

/** Phase 18 / 23 — speed profile options for the ToggleBar dropdown. */
export type QueryProfile = "fast" | "balanced" | "thorough" | "custom";

export interface QueryProfileOption {
  key: QueryProfile;
  label: string;
  description: string;
  retrieval_k: number;
  rerank_enabled: boolean;
  hyde_enabled: boolean;
  approxLatency: string;
}

export const QUERY_PROFILES: QueryProfileOption[] = [
  {
    key: "fast",
    label: "Fast",
    description: "Minimum retrieval — 10 vector candidates, no lexical sidecar, no rerank, no HyDE. Best for quick lookups and follow-ups.",
    retrieval_k: 10,
    rerank_enabled: false,
    hyde_enabled: false,
    approxLatency: "~0.8-2s",
  },
  {
    key: "balanced",
    label: "Balanced",
    description: "Default. Balanced retrieval profile with reranker on. HyDE only runs when the HyDE toggle is on.",
    retrieval_k: 40,
    rerank_enabled: true,
    hyde_enabled: false,
    approxLatency: "~2-8s",
  },
  {
    key: "thorough",
    label: "Thorough",
    description: "Full retrieval pipeline. 60 vector candidates plus deeper lexical recall and reranker on. HyDE only runs when toggled.",
    retrieval_k: 60,
    rerank_enabled: true,
    hyde_enabled: false,
    approxLatency: "~8-30s",
  },
  {
    key: "custom",
    label: "Custom",
    description: "Your saved Retrieval Settings drive the gather and final-source shape. Match sensitivity stays off.",
    retrieval_k: 0, // display-only; actual value comes from user settings
    rerank_enabled: true,
    hyde_enabled: false,
    approxLatency: "varies",
  },
];

/**
 * Phase 29 — per-turn chat attachment.
 *
 * Image: `content` is base64 (NO `data:...;base64,` prefix — the
 * backend builds the data URI from `mime_type` + `content`).
 * Text: `content` is UTF-8 plain text (NOT base64). Inlined into
 * the augmented prompt server-side.
 *
 * Attachments live on a single ChatRequest. They are NOT ingested
 * into the corpus — that's a separate upload path.
 *
 * Hard caps enforced server-side:
 *   - Max 4 attachments per turn
 *   - Per-attachment size: 20MB binary / 28MB content string
 *   - Text attachments truncated to ~32K chars at prompt build time
 */
export interface ChatAttachment {
  filename: string;
  mime_type: string;
  size_bytes: number;
  kind: "image" | "text";
  content: string;
}

export interface ChatRequest {
  conversation_id?: string | null;
  message: string;
  corpus_ids?: string[];
  retrieval_tier?: RetrievalTier;
  collections?: string[];
  overrides?: ChatOverrides;
  selected_tools?: string[];
  // Phase 24 — multi-select skills active for this turn
  active_skill_ids?: string[];
  // Phase 24 — opt-in reasoning cascade (analyst → chat model)
  reasoning_cascade?: boolean;
  // Phase 29 — per-turn attachments (images, text files). Empty
  // message is valid when at least one attachment is present.
  attachments?: ChatAttachment[];
}

export interface SSEEvent {
  type:
    | "token"
    | "thinking"
    | "trimming"
    | "budget"
    | "error"
    | "done"
    | "sources"
    | "tool_call_start"
    | "tool_result"
    | "trace_event"
    | "tier_downgraded"
    | "draft_reset";
  content: string | null;
  thinking?: string;
  trace_event?: TraceEvent;
  conversation_id?: string;
  model_used?: string;
  trimming_applied?: boolean;
  trimming_details?: string;
  sources?: SourceChunk[];
  tokens_used?: number;
  tokens_max?: number;
  // Trust-signal fields — present on the terminal `done` frame so the live
  // message can render the RetrievalBadge before any reload.
  chunks_returned?: number;
  strategy_used?: string;
  query_profile_used?: string;
  reasoning_mode_used?: string;
  hyde_applied?: boolean;
  agentic_mode_used?: boolean;
  downgrade_reason?: string | null;
  // Phase 24
  skills_used?: string[];
  tools_used?: string[];
  reasoning_cascade_applied?: boolean;
  collections_queried?: string[];
}

export interface ToolCallPayload {
  name: string;
  args?: string;
  result?: string;
}

export interface ConversationListItem {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
  last_message_preview?: string;
}
