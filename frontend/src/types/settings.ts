// Settings-related types for Polymath RAG v3.3

import type { Tool } from "./tools";
import type { RetrievalTier } from "./chat";
import type { ModelProfileRef } from "./corpus";

// ============================================================================
// GLOBAL SETTINGS (Phase 10 — server-side, from /api/settings)
// ============================================================================

export interface AuthConfig {
  auth_secret_key: string;
  auth_algorithm: string;
  auth_token_expire_days: number;
}

export interface InfrastructureSettings {
  mongodb_url: string;
  qdrant_url: string;
  neo4j_uri: string;
  neo4j_user: string;
  neo4j_password: string;
  litellm_base_url: string;
  litellm_master_key: string;
  ollama_base_url: string;
  redis_url: string;
  embedder_url: string;
  reranker_url: string;
  // Modal cloud GPU (Phase 14.3) — primary ingestion embed path
  modal_enabled: boolean;
  modal_embedder_url: string;
  auth: AuthConfig;
}

export interface ServiceStatus {
  status: "ok" | "error" | null;
  latency_ms: number | null;
  error?: string | null;
}

export interface ChatLLMSettings {
  default_chat_model: string;
  max_context_tokens: number;
  max_completion_tokens: number;
  temperature: number;
  top_p: number;
  // Agentic mode (Phase 14.1)
  agentic_mode_enabled: boolean;
  agentic_model: string;
  // Reasoning modes (Phase 15)
  default_reasoning_mode: string;
  reasoning_blend: string[];
  // HyDE (Phase 17) — dedicated small/fast model for hypothetical generation
  hyde_model: string;
  // Query Profile (Phase 18) — speed preset
  query_profile: "fast" | "balanced" | "thorough" | "custom";
}

/**
 * 12 curated reasoning modes shown in the ToggleBar dropdown (Phase 15).
 * Power-user blend pool (40 raw modes) lives in Settings → advanced.
 * Keys must match REASONING_TEMPLATES in backend/services/reasoning.py.
 */
export interface ReasoningModeOption {
  key: string;
  label: string;
  description: string;
  /** True when this mode triggers extra LLM calls (atomic, self_correct). */
  retrievalLevel?: boolean;
}

export const REASONING_MODES: ReasoningModeOption[] = [
  { key: "none", label: "Off", description: "No reasoning template. Model answers directly." },
  { key: "step_by_step", label: "Step by Step", description: "Explicit numbered reasoning steps." },
  { key: "branching", label: "Branching", description: "Explore multiple approaches, pick the strongest." },
  { key: "creative", label: "Creative", description: "Unexpected angles and lateral thinking." },
  { key: "analytical", label: "Analytical", description: "Break into claims, rank by evidence." },
  { key: "self_correct", label: "Self-Correct", description: "Draft → review → revise (extra LLM call).", retrievalLevel: true },
  { key: "atomic", label: "Atomic", description: "Decompose into sub-questions, retrieve each (extra LLM call).", retrievalLevel: true },
  { key: "planning", label: "Planning", description: "Build a plan, then execute it step by step." },
  { key: "graph_reason", label: "Graph Reason", description: "Trace conceptual connections across sources." },
  { key: "debate", label: "Debate", description: "Argue two opposing sides, synthesize a balanced view." },
  { key: "deep_research", label: "Deep Research", description: "Solve foundational questions first, layer up." },
  { key: "concise", label: "Concise", description: "Maximum compression. One insight per sentence." },
  { key: "meta", label: "Meta", description: "Model chooses the reasoning style that fits the question." },
];

/**
 * Raw 40-mode pool — power-user surface for the reasoning selector.
 * Keys mirror REASONING_TEMPLATES in backend/services/reasoning.py.
 * When `powerUserReasoning` is true the selector shows this list instead of REASONING_MODES.
 */
export const REASONING_RAW_MODES: ReasoningModeOption[] = [
  { key: "none", label: "Off", description: "No reasoning template." },
  // Sequential / Chain
  { key: "chain_of_thought", label: "Chain of Thought", description: "Step-by-step reasoning before the answer." },
  { key: "self_consistent_cot", label: "Self-Consistent CoT", description: "Multiple reasoning paths; pick most consistent." },
  { key: "deliberate_cot", label: "Deliberate CoT", description: "Verify each step before moving on." },
  { key: "react", label: "ReAct", description: "Alternate reasoning and information-gathering." },
  { key: "atomic_thoughts", label: "Atomic Thoughts", description: "Verify smallest claims independently." },
  { key: "micro_cot", label: "Micro CoT", description: "One sentence per step, maximally compressed." },
  // Branching / Tree
  { key: "tree_of_thought", label: "Tree of Thought", description: "Explore parallel branches; prune weak ones." },
  { key: "guided_tot", label: "Guided ToT", description: "Score and expand only top branches." },
  { key: "monte_carlo_tot", label: "Monte Carlo ToT", description: "Simulate random candidates; pick best." },
  { key: "beam_search_tot", label: "Beam Search ToT", description: "Keep top 2-3 paths at each step." },
  { key: "reflexion_tot", label: "Reflexion ToT", description: "Reflect between attempts, improve iteratively." },
  { key: "program_aided_tot", label: "Program-Aided ToT", description: "Use pseudocode-like structured logic." },
  // Graph / Network
  { key: "graph_of_thought", label: "Graph of Thought", description: "Non-linear thought graph; merge and synthesize." },
  { key: "dynamic_graph", label: "Dynamic Graph", description: "Evolving graph; restructure as you learn." },
  { key: "kg_augmented", label: "KG-Augmented", description: "Ground in entity → relation → entity structure." },
  // Self-Correction / Refinement
  { key: "reflexion", label: "Reflexion", description: "Answer, self-critique, revise.", retrievalLevel: true },
  { key: "self_refine", label: "Self-Refine", description: "Two+ passes of critique and refinement.", retrievalLevel: true },
  { key: "multi_agent_debate", label: "Multi-Agent Debate", description: "Optimist / skeptic / pragmatist, then synthesize." },
  // Tool-Augmented
  { key: "toolformer", label: "Toolformer", description: "Flag moments where tools would help." },
  { key: "program_of_thought", label: "Program of Thought", description: "Reasoning as executable-style program." },
  { key: "scratchpad", label: "Scratchpad", description: "Visible working memory with intermediates." },
  // Planning / Decomposition
  { key: "plan_and_solve", label: "Plan and Solve", description: "Explicit numbered plan, then execute." },
  { key: "least_to_most", label: "Least to Most", description: "Easiest sub-problem first; build up." },
  { key: "goal_tree", label: "Goal Tree", description: "Decompose into sub-goals, execute bottom-up." },
  { key: "plan_and_execute", label: "Plan and Execute", description: "Plan with checkpoints, verify each phase." },
  // Agentic / Loop
  { key: "prar_loop", label: "PRAR Loop", description: "Perceive → Reason → Act → Reflect." },
  { key: "tool_use_reasoning", label: "Tool-Use Reasoning", description: "Reason about which tools and why." },
  { key: "memory_augmented", label: "Memory Augmented", description: "Use all available context and prior patterns." },
  // Stochastic / Sampling
  { key: "monte_carlo_sampling", label: "Monte Carlo Sampling", description: "Many candidates; score; pick best." },
  { key: "stochastic_exploration", label: "Stochastic Exploration", description: "Break obvious patterns, try unexpected angles." },
  { key: "hypothesis_ranking", label: "Hypothesis Ranking", description: "3-5 hypotheses ranked by plausibility." },
  { key: "self_consistent_sampling", label: "Self-Consistent Sampling", description: "Pick the most internally consistent answer." },
  // Hybrid / Advanced
  { key: "graphrag_integrated", label: "GraphRAG Integrated", description: "Structured relationships + generative reasoning." },
  { key: "modular_pipelines", label: "Modular Pipelines", description: "Specialized subtasks chained together." },
  { key: "thought_distillation", label: "Thought Distillation", description: "Compress everything to essential insight." },
  { key: "multimodal_integration", label: "Multimodal Integration", description: "Reason across multiple representations." },
  { key: "recursive_introspection", label: "Recursive Introspection", description: "Examine your own reasoning process." },
  { key: "meta_reasoning", label: "Meta-Reasoning", description: "Decide HOW to reason before reasoning." },
  { key: "dynamic_routing", label: "Dynamic Routing", description: "Switch approach mid-flight when stuck." },
  { key: "hybrid_agentic", label: "Hybrid Agentic", description: "Combine planning, tools, reflection, memory." },
];

export interface RetrievalSettings {
  default_tier: "qdrant_only" | "qdrant_mongo" | "qdrant_mongo_graph";
  top_k_child: number;
  top_k_summary: number;
  reranker_model: string;
  rerank_top_n: number;
  rerank_enabled: boolean;
  similarity_threshold: number;
  max_corpora_per_query: number;
  neo4j_expansion_cap: number;
  // Phase 24 — Final K (chunks to LLM, post-rerank). Custom profile only.
  final_top_k: number;
  fact_seed_limit: number;
  vector_child_chunks: number;
  vector_summaries: number;
  vector_final_sources: number;
  vector_reranker: boolean;
  hybrid_child_chunks: number;
  hybrid_summaries: number;
  hybrid_final_sources: number;
  hybrid_reranker: boolean;
  graph_child_chunks: number;
  graph_summaries: number;
  graph_fact_seeds: number;
  graph_expansion: number;
  graph_final_sources: number;
  graph_reranker: boolean;
  graph_query_seed_entities: number;
  graph_query_max_hops: number;
  graph_query_node_limit: number;
}

export type ModalGpuTier = "T4" | "L4" | "A10G" | "L40S" | "A100" | "H100";

export interface ModalDeploySettings {
  gpu_tier: ModalGpuTier;
  min_containers: number;
  max_containers: number;
  idle_timeout_seconds: number;
  concurrency_per_container: number;
  app_name: string;
  model_id: string;
  use_auth: boolean;
  // Phase 19.3 — runtime connection (previously only in .env)
  enabled: boolean;
  embedder_url: string;
  /** Workspace name captured by `modal token info`. UI-only. */
  workspace: string;
}

export const MODAL_GPU_TIERS: {
  tier: ModalGpuTier;
  label: string;
  priceHint: string;
  /** Hourly $/GPU used to drive the live monthly cost estimate. */
  pricePerHour: number;
  notes: string;
}[] = [
  { tier: "T4",   label: "T4 (16 GB)",   priceHint: "~$0.59/hr", pricePerHour: 0.59, notes: "Cheapest; fine for ≤1B embedding models" },
  { tier: "L4",   label: "L4 (24 GB)",   priceHint: "~$0.80/hr", pricePerHour: 0.80, notes: "Balanced price/perf for most inference" },
  { tier: "A10G", label: "A10G (24 GB)", priceHint: "~$1.10/hr", pricePerHour: 1.10, notes: "Faster than L4 for batch workloads" },
  { tier: "L40S", label: "L40S (48 GB)", priceHint: "~$1.95/hr", pricePerHour: 1.95, notes: "Modal's workhorse — 575k tok/s on Qwen2-7B" },
  { tier: "A100", label: "A100 (40 GB)", priceHint: "~$2.10/hr", pricePerHour: 2.10, notes: "Large models (>7B) or heavy concurrency" },
  { tier: "H100", label: "H100 (80 GB)", priceHint: "~$4.56/hr", pricePerHour: 4.56, notes: "Fastest; reserve for giant models" },
];

// One Ghost B extraction sidecar the user can toggle on/off. The worker
// health-probes ENABLED endpoints per document (list order = preference) and
// dispatches to whichever are live — power a GPU box off and work simply
// flows to the next enabled endpoint (e.g. the always-on local sidecar).
export interface ExtractionEndpoint {
  label: string;
  url: string;
  enabled: boolean;
}

export interface ExtractionSettings {
  endpoints: ExtractionEndpoint[];
}

// Deploy-readiness report from GET /api/settings/extraction/validate —
// every configured endpoint probed from the BACKEND's network position
// (what the ingestion worker actually sees), with per-check results.
export interface ExtractionValidationChecks {
  reachable: boolean;
  healthy?: boolean;
  warm?: boolean;
  model_loaded?: boolean;
  gpu_active?: boolean | null;
  version_match?: boolean | null;
}

export interface ExtractionValidationEndpoint {
  label: string;
  url: string;
  enabled: boolean;
  checks: ExtractionValidationChecks;
  info: {
    backend?: string;
    device?: string;
    model?: string;
    pipeline_version?: string | null;
    providers?: string[];
  };
  state: "ready" | "warning" | "fail";
  detail: string;
}

export interface ExtractionValidationReport {
  endpoints: ExtractionValidationEndpoint[];
  backend_pipeline_version: string | null;
  enabled_total: number;
  enabled_ready: number;
  deploy_ready: boolean;
}

export interface GlobalIngestionSummarySettings {
  enabled: boolean;
  max_summary_tokens: number;
  max_concurrent: number;
  summary_models: ModelProfileRef[];
}

export interface GlobalIngestionSettings {
  summary: GlobalIngestionSummarySettings;
}

export interface GlobalSettings {
  infrastructure: InfrastructureSettings;
  chat: ChatLLMSettings;
  retrieval: RetrievalSettings;
  modal: ModalDeploySettings;
  extraction?: ExtractionSettings;
  ingestion?: GlobalIngestionSettings;
}

export interface GlobalSettingsResponse {
  settings: GlobalSettings;
}

export interface GlobalSettingsUpdate {
  chat?: ChatLLMSettings | null;
  retrieval?: RetrievalSettings | null;
  modal?: ModalDeploySettings | null;
  extraction?: ExtractionSettings | null;
  ingestion?: GlobalIngestionSettings | null;
}

export interface InfrastructureTestResponse {
  services: Record<string, ServiceStatus>;
}

export interface ModelInfo {
  id: string;
  name: string;
  provider: string;
  source: string;
  type: "chat" | "embedding";
  context_length?: number;
  dimension?: number;
}

export interface ModelsResponse {
  chat_models: ModelInfo[];
  embedding_models: ModelInfo[];
  default_model: string;
  default_embedding_model: string;
}

export interface Collection {
  id: string;
  name: string;
  description?: string;
  document_count: number;
  created_at: string;
}

export interface RAGSettings {
  retrievalK: number;
  retrievalSummaryK: number;
  retrievalFinalK: number;
  retrievalFactSeedLimit: number;
  retrievalGraphExpansion: number;
  graphQuerySeedEntities: number;
  graphQueryMaxHops: number;
  graphQueryNodeLimit: number;
  vectorChildChunks: number;
  vectorSummaries: number;
  vectorFinalSources: number;
  vectorReranker: boolean;
  hybridChildChunks: number;
  hybridSummaries: number;
  hybridFinalSources: number;
  hybridReranker: boolean;
  graphChildChunks: number;
  graphSummaries: number;
  graphFactSeeds: number;
  graphExpansion: number;
  graphFinalSources: number;
  graphReranker: boolean;
  hydeEnabled: boolean;
  webSearchEnabled: boolean;
  webFetchDepth: "snippets" | "normal" | "deep";
  webResearchMode: boolean;
  webYoutubeTranscripts: boolean;
  webMaxSources: number;
  rerankingEnabled: boolean;
  /** Phase 15 — one of the 12 curated mode keys (see REASONING_MODES). */
  reasoningMode: string;
  selectedCollectionIds: string[];
  retrievalTier: RetrievalTier;
  selectedCorpusIds: string[];
}

/**
 * 7 deterministic UI themes. Legacy values ("light"/"dark"/"system") were
 * dropped — the App.tsx fallback rescues any persisted legacy value into
 * "ayu-mirage" on next mount, so existing users don't get a broken paint.
 */
export type Theme =
  | "ayu-mirage"
  | "gruvbox"
  | "serendipity"
  | "nord"
  | "dracula"
  | "solar"
  | "claude";

export interface UISettings {
  theme: Theme;
  fontSize: "small" | "medium" | "large";
  reducedMotion: boolean;
  sidebarOpen: boolean;
}

export interface SettingsState extends RAGSettings, UISettings {
  // Model Settings
  selectedModel: string;
  temperature: number;
  topP: number;
  maxTokens: number;

  // Agent Tool Settings
  availableTools: Tool[];
  selectedToolIds: string[];

  // Agentic mode (Phase 14.1)
  agenticModeEnabled: boolean;
  agenticModel: string;

  // Cloud embed mode (Phase 14.3) — read-only surface of server MODAL_ENABLED
  modalEnabled: boolean;
  modalEmbedderUrl: string;

  // Reasoning modes (Phase 15) — reasoningMode inherited from RAGSettings.
  // reasoningBlend is the advanced power-user blend pool keys.
  reasoningBlend: string[];

  // Search mode dispatch (Phase 27) — backend auto-detects local vs
  // global from query shape; user can override in the ToggleBar.
  searchMode: "auto" | "local" | "global";

  // Thinking-effort dial (Phase 28) — per-turn knob for reasoning
  // models (OpenAI o-series, Claude 3.7+, Gemini 2.5+, DeepSeek-R1).
  // Backend maps this to provider-native params; UI hides the
  // selector when the selected model has no thinking dial.
  thinkingEffort: "auto" | "none" | "low" | "medium" | "high";

  // HyDE (Phase 17) — dedicated cheap model for hypothetical generation
  hydeModel: string;

  // Query Profile (Phase 18) — speed preset selected in ToggleBar
  queryProfile: "fast" | "balanced" | "thorough" | "custom";

  // Power-user reasoning (P5) — swap curated 12 → raw 40 in the selector
  powerUserReasoning: boolean;

  // Phase 24 — Skills (multi-select) + Reasoning Cascade (per-turn opt-in)
  availableSkills: import("./skills").Skill[];
  selectedSkillIds: string[];
  reasoningCascadeEnabled: boolean;
}

/**
 * Legacy alias for pre-Phase-10 flat AppSettings shape.
 * Kept as a re-export of GlobalSettings for any callers still importing by old name.
 * @deprecated Use GlobalSettings directly.
 */
export type AppSettingsPayload = GlobalSettings;

export interface ModalTestResult {
  ok: boolean;
  latency_ms: number;
  dimension: number | null;
  error: string | null;
}

// ─── Sprint 2B — Modal one-click deploy contract ────────────────────────────

/** GET /api/infrastructure/modal/status — current Modal deployment state. */
export interface ModalStatus {
  deployed: boolean;
  url: string | null;
  app_id: string | null;
  container_count: number | null;
  deployed_at: string | null;
}

/** POST /api/infrastructure/modal/deploy — request body. */
export interface ModalDeployRequest {
  gpu_tier: ModalGpuTier;
  max_containers: number;
  min_containers: number;
  idle_timeout: number;
  app_name: string;
}

/** POST /api/infrastructure/modal/deploy — terminal success response. */
export interface ModalDeployResult {
  url: string;
  app_id: string;
  deployed_at: string;
}

/**
 * SSE frame from GET /api/infrastructure/modal/deploy/stream.
 * Phase enum: verifying_tokens → building_app → deploying → ready, or failed.
 */
export interface ModalDeployEvent {
  phase:
    | "verifying_tokens"
    | "building_app"
    | "deploying"
    | "ready"
    | "failed";
  message: string;
  estimated_seconds?: number;
  url?: string;
  app_id?: string;
  error?: string;
  at_phase?: string;
}

export type TokenCount = {
  current: number;
  max: number;
  percentage: number;
};
