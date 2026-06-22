// Mission Control — cross-domain graph discovery types.
// Mirrors backend models.schemas.Graph{Discover,...}.

export type DiscoverMode = "auto" | "connect" | "gaps" | "themes";
export type BridgeClassification =
  | "terminological"
  | "conceptual"
  | "structural_analog";

export interface DiscoverFrontierItem {
  entity_id: string;
  canonical_name: string;
  primary_domain: string;
  degree: number;
  domains_touched: string[];
  cross_domain_potential: number;
  context: string;
}

export interface DiscoverBridgeItem {
  source: string;
  source_name: string;
  source_domain: string;
  target: string;
  target_name: string;
  target_domain: string;
  classification: BridgeClassification;
  path_count: number;
  path_entity_ids?: string[];
  path_entities?: string[];
  evidence?: string;
  topology_sim?: number | null;
  neighbor_jaccard?: number | null;
  explanation: string;
}

export interface DiscoverWeakLinkItem {
  source: string;
  source_name: string;
  source_domain: string;
  target: string;
  target_name: string;
  target_domain: string;
  weakness_type:
    | "fragile_bridge"
    | "generic_relation"
    | "thin_evidence"
    | "unsupported_analogy"
    | string;
  severity: "low" | "medium" | "high" | string;
  classification: string;
  relation_family?: string | null;
  path_count: number;
  path_entity_ids?: string[];
  path_entities?: string[];
  evidence?: string;
  rationale: string;
  action_question: string;
}

export interface DiscoverAnalogyItem {
  source: string;
  source_name: string;
  source_domain: string;
  target: string;
  target_name: string;
  target_domain: string;
  topology_sim: number;
  rationale: string;
}

export interface DiscoverTransferAnalog {
  entity?: string;
  name?: string;
  domain?: string;
  topology_sim?: number;
}

export interface DiscoverTransferItem {
  hub: string;
  hub_name: string;
  hub_domain: string;
  cd_pagerank: number;
  target_domains: string[];
  analogs: DiscoverTransferAnalog[];
  action_hypothesis: string;
}

export interface DiscoverQuestionItem {
  text: string;
  domain_pills: string[];
}

export interface DiscoverMetrics {
  node_count: number;
  edge_count: number;
  density: number;
  cross_domain_edge_pct: number;
  modularity_proxy: number;
  per_domain_edge_counts: Record<string, { internal: number; external: number }>;
  relation_family_counts?: Record<string, number>;
  domain_density: Record<string, number>;
  top_cross_domain_pagerank: Array<{
    entity_id: string;
    canonical_name: string;
    domain: string;
    score: number;
    domains_touched: string[];
  }>;
}

export interface DiscoverDomainSummary {
  cluster_id: number;
  name: string;
  size: number;
  top_entities: string[];
}

export interface DiscoverGraphNode {
  id: string;
  label: string;
  domain: string;
  emphasis: string;
  degree?: number;
  concept?: string;
  object_kind?: string;
  domain_type?: string;
  canonical_family?: string;
}

export interface DiscoverGraphLink {
  source: string;
  target: string;
  emphasis: string;
  classification?: string | null;
  predicate?: string | null;
  relation_family?: string | null;
  confidence?: number | null;
  path_entity_ids?: string[];
  path_entities?: string[];
  evidence?: string;
}

export interface DiscoverAnchor {
  anchor_type: string;
  anchor_id: string;
  label: string;
  score: number;
  source: string;
  doc_id?: string | null;
}

export interface DiscoverConceptCommunity {
  concept_id: string;
  label: string;
  size: number;
  top_entities: string[];
  bridge_count?: number;
  scope_count?: number;
}

export interface DiscoverEntityConcept {
  concept_id: string;
  label: string;
  top_entities: string[];
}

export interface DiscoverGraphHintPayload {
  shape?: {
    label?: string;
    description?: string;
    rationale?: string;
  };
  gateways?: Array<{
    id?: string;
    name?: string;
    connects?: string[];
    reason?: string;
  }>;
  gap_depths?: Array<{
    id?: string;
    question?: string;
    depth?: "near" | "deeper" | "lateral" | string;
    between?: string[];
  }>;
  supporting_statements?: Array<{
    evidence_id?: string;
    source_label?: string;
    statement?: string;
  }>;
  context_hint?: string;
}

export interface DiscoverGapProfile {
  version?: string;
  gap_intent?: boolean;
  primary_domain?: string;
  primary_label?: string;
  secondary_domains?: string[];
  domain_scores?: Record<string, number>;
  confidence?: number;
  method_frame?: string;
  output_shape?: string;
  required_metrics?: string[];
  required_evidence?: string[];
  likely_missing_data?: string[];
  matched_indicators?: Record<string, string[]>;
  synthesis_rule?: string;
  calculation_policy?: string;
}

export interface DiscoverHeadline {
  kicker: string;
  headline: string;
  deck: string[];
}

export interface DiscoverThemeItem {
  theme_id: string;
  name: string;
  weight_pct: number;
  size: number;
  top_concepts: string[];
  flag?: string | null;
  prose: string[];
}

export interface DiscoverBridgeV2Item {
  bridge_id: string;
  from_cluster?: string;
  to_cluster?: string;
  subhead: string;
  anchor_concepts: string[];
  betweenness: number;
  edge_count: number;
  prose: string[];
  source_entity_id?: string | null;
  target_entity_id?: string | null;
}

export interface DiscoverGapV2Item {
  gap_id: string;
  cluster_a: string;
  cluster_b: string;
  cluster_a_label: string;
  cluster_b_label: string;
  question: string;
  semantic_similarity: number;
  structural_connectivity: number;
  expected_connectivity: number;
  gap_score: number;
  anchor_concepts: string[];
  prose: string[];
}

export interface DiscoverLatentTopicItem {
  entity_id: string;
  canonical_name: string;
  domain: string;
  mention_count: number;
  doc_count: number;
  degree: number;
  latent_score: number;
  rationale: string;
  prose: string[];
}

export interface DiscoverTensionFrame {
  label: string;
  body: string;
  cluster?: string;
}

export interface DiscoverTensionItem {
  tension_id: string;
  shared_concept: string;
  tension_type: string;
  summary: string;
  frames: DiscoverTensionFrame[];
}

export interface DiscoverTracePayload {
  anchor_terms: string[];
  latent_terms: string[];
  vector_neighbors: Array<Record<string, any>>;
  graph_expansion: Record<string, any>;
  working_entities: Array<Record<string, any>>;
  selected_edges: Array<Record<string, any>>;
  source_docs: Array<Record<string, any>>;
  graph_hint?: DiscoverGraphHintPayload;
  gap_profile?: DiscoverGapProfile;
  evidence_filter?: {
    raw?: number;
    accepted?: number;
    rejected?: number;
    all_rejected?: boolean;
    rejection_reasons?: Record<string, number>;
  };
  retrieval_evidence?: {
    source?: string;
    status?: string;
    requested_tier?: string;
    effective_tier?: string;
    final_top_k?: number;
    chunks?: number;
    hydrated_chunks?: number;
    downgrade_reason?: string;
    error?: string;
  };
  llm_context?: {
    packet_version?: string;
    query?: string;
    collections?: Record<string, string>;
    research_contract?: {
      job?: string;
      claim_levels?: string[];
      avoid?: string;
    };
    prompt?: {
      system_chars?: number;
      user_chars?: number;
      estimated_tokens?: number;
      preview?: string;
    };
    files?: Array<{
      doc_id: string;
      source_label: string;
      source?: GraphSourceMetadata;
      chunk_count: number;
      chunk_ids: string[];
      has_temporal?: boolean;
    }>;
    chunks?: Array<{
      chunk_id: string;
      doc_id: string;
      source_label: string;
      source?: GraphSourceMetadata;
      preview: string;
      has_temporal?: boolean;
    }>;
    counts?: Record<string, number>;
    visibility?: Record<string, any>;
    graph_hint?: DiscoverGraphHintPayload;
    gap_profile?: DiscoverGapProfile;
  };
  stages?: Array<{
    stage: string;
    label: string;
    count: number;
    status: "ok" | "watch" | "missing" | string;
    detail: string;
  }>;
}

export interface GraphSourceMetadata {
  title?: string;
  filename?: string;
  source_type?: string;
  type?: string;
  mime?: string;
  section?: string;
  page_range?: string;
  page?: string;
  source_tier?: string;
  author?: string;
  publisher?: string;
  publication_date?: string;
  date?: string;
  genre?: string;
  description?: string;
  hints?: Record<string, string[]>;
}

export interface SynthesisSource {
  index: number;
  evidence_id: string;
  source_label: string;
  doc_id: string;
  chunk_id: string;
  snippet: string;
}

export interface AutoSynthesisPayload {
  headline: string;
  markdown: string;
  sources: SynthesisSource[];
  fallback: boolean;
  fallback_reason?: string | null;
}

/** Web evidence lane — bounded live-web sources for graph synthesis. */
export interface WebEvidenceItem {
  title?: string;
  url?: string;
  snippet?: string;
  source_tier: "web_search";
  fetch_depth?: "snippets" | "normal" | "deep";
}

export interface WebEvidencePayload {
  enabled: boolean;
  fetch_depth: "snippets" | "normal" | "deep";
  max_results: number;
  sources: WebEvidenceItem[];
}

export interface InsightPacketSummary {
  sparse: boolean;
  temporal_support: boolean;
  counts: Record<string, number>;
  evidence_sources: Record<string, number>;
  fallback_reason?: string | null;
}

export interface ContextGraphJumpTarget {
  section: "themes" | "bridges" | "gaps" | "trace" | string;
  label: string;
  detail: string;
  target_id?: string | null;
}

export interface ContextGraphNode {
  id: string;
  label: string;
  kind: "topic" | "concept" | string;
  role: string;
  topic_id?: string | null;
  size: number;
  weight: number;
  evidence_count: number;
  top_entities: string[];
  jump_targets: ContextGraphJumpTarget[];
}

export interface ContextGraphLink {
  source: string;
  target: string;
  kind: string;
  role: string;
  weight: number;
  suggested: boolean;
  evidence: string;
}

export interface ContextGraphPayload {
  nodes: ContextGraphNode[];
  links: ContextGraphLink[];
  meta: Record<string, any>;
}

export type GraphSynthesisMode = "research" | "ideation" | "nuance" | "gap";

export interface GraphDiscoverRequest {
  /**
   * DEPRECATED — use corpus_ids. Wrapped server-side into
   * corpus_ids=[corpus_id] when present. Kept for back-compat with
   * single-corpus callers.
   */
  corpus_id?: string;
  /**
   * Multi-corpus discover (PR 3+). Backend fans out per-corpus and
   * merges results via merge_discover_results. AtomicView passes its
   * full corpusIds list here so the merged synthesis sees evidence
   * from every selected corpus.
   */
  corpus_ids?: string[];
  query: string;
  mode?: DiscoverMode;
  // Phase 3 — synthesis-mode selector. "research" (default) gives the
  // concrete-claim research synthesis. "ideation" gives the build-advisor
  // output with [BUILD IDEA] blocks. "nuance" gives conceptual exploration
  // of gaps, analogies, transfers, and bridges. "gap" gives a structural
  // gap-analysis map — what the corpus does NOT yet connect (candidate gaps,
  // fragile bridges, weak links). Retrieval is the same shape; the packet
  // caps and system prompt differ per mode.
  synthesis_mode?: GraphSynthesisMode;
  /**
   * Sprint #2 — opt-in multi-stage synthesis. When true, the backend
   * runs a draft → critique → revise loop (2-3× LLM cost) that flags
   * fabricated terms / shell sentences / missing citations / label
   * leaks and revises them away. Off by default. Surfaced in
   * AgentSearchTab as a "validate" toggle next to synthesis-mode.
   */
  validate_synthesis?: boolean;
  // Optional live-web grounding lane for graph synthesis. Off by default.
  web_search_enabled?: boolean;
  web_fetch_depth?: "snippets" | "normal" | "deep";
  web_max_results?: number;
  session_id?: string;
  // Same model reference chat sends in overrides.model. May be a raw
  // LiteLLM id, pool:<id>, or profile:<id>.
  model?: string;
  // When true, add a second-pass strategic read from the user's
  // agentic/reasoning pool after the selected chat model creates the cards.
  agentic?: boolean;
}

export interface DiscoverStrategicRead {
  best_opportunity: string;
  weakest_assumption: string;
  bridge_to_inspect: string;
  next_query: string;
  confidence: "low" | "medium" | "high" | string;
}

export interface DiscoverIntentProfile {
  intent_type: string;
  confidence: number;
  suggested_mode: DiscoverMode | string;
  rationale: string;
  query_terms: string[];
}

export interface DiscoverAtomicReasoningStep {
  step: string;
  status: "ok" | "watch" | "missing" | string;
  detail: string;
  count?: number | null;
}

export interface DiscoverSocraticPrompt {
  question: string;
  why: string;
  mode: DiscoverMode | string;
  focus: string;
}

export interface GraphDiscoverResponse {
  session_id: string;
  corpus_id: string;
  query: string;
  mode: DiscoverMode;
  interpretation: string;
  frontier: DiscoverFrontierItem[];
  analogies: DiscoverAnalogyItem[];
  bridges: DiscoverBridgeItem[];
  weak_links?: DiscoverWeakLinkItem[];
  transfers: DiscoverTransferItem[];
  questions: DiscoverQuestionItem[];
  strategic_read?: DiscoverStrategicRead | null;
  intent_profile?: DiscoverIntentProfile;
  atomic_trace?: DiscoverAtomicReasoningStep[];
  socratic_prompts?: DiscoverSocraticPrompt[];
  metrics: DiscoverMetrics;
  domain_map_summary: DiscoverDomainSummary[];
  graph: { nodes: DiscoverGraphNode[]; links: DiscoverGraphLink[] };
  anchors?: DiscoverAnchor[];
  concept_communities?: DiscoverConceptCommunity[];
  entity_concept_map?: Record<string, DiscoverEntityConcept>;
  headline?: DiscoverHeadline;
  themes?: DiscoverThemeItem[];
  bridges_v2?: DiscoverBridgeV2Item[];
  gaps_v2?: DiscoverGapV2Item[];
  latent_topics?: DiscoverLatentTopicItem[];
  tensions?: DiscoverTensionItem[];
  trace?: DiscoverTracePayload;
  auto_synthesis?: AutoSynthesisPayload;
  web_evidence?: WebEvidencePayload;
  insight_packet_summary?: InsightPacketSummary;
  context_graph?: ContextGraphPayload;
}

export interface GraphDiscoverSession {
  session_id: string;
  corpus_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  turn_count: number;
  first_query?: string | null;
}

export interface GraphDiscoverTurn {
  query: string;
  mode: DiscoverMode;
  created_at: string;
  response?: GraphDiscoverResponse | null;
}

export interface GraphDiscoverSessionDetail extends GraphDiscoverSession {
  turns: GraphDiscoverTurn[];
}

export interface GraphResumeCandidateRequest {
  corpus_id: string;
  query: string;
  threshold?: number;
}

export interface GraphResumeCandidateResponse {
  session?: GraphDiscoverSession | null;
  score: number;
}

export interface GraphSuggestionItem {
  text: string;
  kind: string;
  entities: string[];
  domains: string[];
}

export interface GraphSuggestionsResponse {
  corpus_id: string;
  domain_map_summary: DiscoverDomainSummary[];
  suggestions: GraphSuggestionItem[];
}
