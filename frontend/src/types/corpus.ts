// Corpus-related types for Polymath RAG v3.3
// Maps to MongoDB corpora, documents, chunks collections

/**
 * Sprint 2A — three-way embed_mode.
 *   "local"  — local sentence-transformers sidecar (CPU/GPU, in-cluster)
 *   "api"    — generic OpenAI-compatible /embeddings endpoint (e.g. SiliconFlow)
 *   "modal"  — globally deployed Modal cloud GPU endpoint
 *
 * Backend coerces legacy values ("local_st" | "modal_tei" | "siliconflow")
 * to the new three-way at validate-time, so reads of legacy corpora still
 * deserialize. New writes from the UI must use the new values.
 */
export type EmbedMode = "local" | "api" | "modal";

// Stored as a backend policy hint. The UI presents chunking as Auto because
// the worker resolves the concrete strategy per file type after parsing.
export type ChildChunkAlgorithm = "sentence_merge" | "semantic_split";

// Phase 14: Ontology-Lite — schema_strict enforcement mode for GHOST B extraction.
//   "off"  — schema is a hint, no enforcement
//   "soft" — remap unknowns to sentinels ('other' / 'related_to'); preserves edge/node
//   "hard" — drop unknowns entirely (precision-critical mode)
export type SchemaStrictMode = "off" | "soft" | "hard";

export interface TokenBudget {
  min_tokens: number;
  target_tokens: number;
  max_tokens: number;
}

/**
 * Single entry in a GHOST A / GHOST B model pool. The corpus-level config
 * holds a list of these (chips in the UI). At runtime, the worker
 * round-robins tasks across entries, each bounded by its own max_concurrent.
 *
 * api_key is Fernet-encrypted at rest. Server returns "[set]" as a masked
 * sentinel so the UI can display "key present" without ever seeing ciphertext
 * or plaintext. Round-tripping "[set]" preserves the stored key.
 */
export interface ModelProfileRef {
  /** UI label hint (openai / deepseek / ollama / …). Not runtime-authoritative. */
  provider_preset: string;
  model: string;
  base_url: string | null;
  /** "[set]" on GET; plaintext on POST/PUT; Fernet at rest. */
  api_key: string | null;
  max_concurrent: number;
  lifecycle_base_url?: string | null;
  /** "[set]" on GET; plaintext on POST/PUT; Fernet at rest. */
  lifecycle_api_key?: string | null;
  lifecycle_auto_start?: boolean;
  lifecycle_auto_stop?: boolean;
  lifecycle_up_path?: string;
  lifecycle_status_path?: string;
  lifecycle_down_path?: string;
  lifecycle_ready_timeout_seconds?: number;
  extra_params: Record<string, unknown>;
}

export interface IngestionConfig {
  // ── FROZEN — embedding identity (locked once doc_count > 0) ─────────────
  /** FROZEN. Friendly model name — display only. */
  embedding_model: string;
  /** FROZEN. Vector dim baked into Qdrant collection. Mismatch = 409. */
  embedding_dimension: number;
  /** FROZEN. Stable id used to detect cross-corpus drift. */
  embedding_model_id: string;

  // ── MUTABLE — embed dispatch (provider / credentials) ───────────────────
  embed_mode: EmbedMode;
  /** MUTABLE. Base URL for embed_mode='api' (e.g. https://api.siliconflow.com/v1). */
  embed_base_url: string | null;
  /** MUTABLE. API key for embed_mode='api'. Server returns "[set]" mask on GET. */
  embed_api_key: string | null;
  /** MUTABLE. Per-corpus concurrency cap on the embed dispatcher. */
  embed_max_concurrent: number | null;
  /**
   * Optional OpenAI-compatible embedding API pool. When embed_mode === "api"
   * and this list is non-empty, backend distributes embedding batches across
   * these entries using per-entry max_concurrent.
   */
  embedding_models: ModelProfileRef[];
  /** MUTABLE. Per-corpus Modal max-containers override. None = global cap. */
  modal_containers: number | null;

  // ── FROZEN — chunking shape (locked once doc_count > 0) ─────────────────
  parent_chunk_tokens: TokenBudget;
  child_chunk_tokens: TokenBudget;
  chunk_overlap: number;
  max_summary_tokens: number;
  child_chunk_algorithm: ChildChunkAlgorithm;

  // GHOST A — Summary Model Pool (round-robin dispatch)
  summary_models: ModelProfileRef[];

  // GHOST B — Extraction Model Pool (round-robin dispatch)
  // Used when provider-card extraction is enabled and models_linked === false.
  extraction_models: ModelProfileRef[];
  entity_confidence_threshold: number;

  /**
   * When true: worker reuses summary_models for GHOST B; UI renders one
   * combined chip pool. When false: extraction_models is an independent pool.
   */
  models_linked: boolean;

  /**
   * Per-corpus extraction contract. Modern production extraction uses
   * provider-card LLM chips. "local" means a local/private OpenAI-compatible
   * provider endpoint such as RTX/vLLM. "cloud" means external provider API.
   * "legacy_local" is the deprecated GLiNER/GLiREL sidecar path. "inherit" =
   * legacy fallback to global Settings; the lifespan migration stamps existing
   * corpora explicit. Resolved truthfully by GET /api/corpora/{id}/extraction-contract.
   */
  extraction_engine?: ExtractionEngine;

  // GHOST B — Universal schema (baked backend-side, see ghost_b.UNIVERSAL_*_SCHEMA).
  // The create/edit UI no longer exposes these. Fields retained on the
  // interface because GET responses still include them (useful for the
  // legacy-custom-schema banner in CorpusManager).
  entity_schema?: string[] | null;
  relation_schema?: string[] | null;
  schema_strict?: SchemaStrictMode;

  // Feature flags
  use_neo4j: boolean;
  chunk_summarization: boolean;
  /** Deprecated policy flag. OCR is disabled backend-side; kept for legacy rows. */
  docling_ocr_enabled: boolean;
  target_qdrant_collections: string[];

  // Onboarding shortcut. Backend normalizes the three flags above to match
  // the preset (except for "custom") on create / update — see
  // services/ingestion_service.apply_preset. Optional on the interface so
  // legacy corpora without the field still deserialize.
  preset?: "fast" | "balanced" | "deep" | "custom";
}

export type IngestionPreset = "fast" | "balanced" | "deep" | "custom";
// mac_queryable_first/mac_safe are displayed as "Mac optimized": one active
// local document, retrieval-first sweeps, and bounded phase-level parallelism.
export type IngestProfileName = "mac_safe" | "mac_queryable_first" | "rtx_assisted";

/** Open-time preset inference — used by the corpus create/edit forms to
 * decide which radio option to pre-select. If the stored preset disagrees
 * with the toggles (e.g. legacy corpora that got "balanced" from the
 * Pydantic default but whose toggles don't match), fall through to
 * toggle-based inference so we don't overwrite the user's existing intent
 * by claiming a preset that doesn't match. */
export function inferPreset(cfg: IngestionConfig): IngestionPreset {
  const toggleMatch: IngestionPreset | null =
    !cfg.use_neo4j && !cfg.chunk_summarization
      ? "fast"
      : cfg.use_neo4j && !cfg.chunk_summarization
        ? "balanced"
        : cfg.use_neo4j && cfg.chunk_summarization
          ? "deep"
          : null; // e.g. chunk_summarization=true but use_neo4j=false → exotic

  if (toggleMatch === null) return "custom";
  if (cfg.preset && cfg.preset !== "custom" && cfg.preset === toggleMatch) {
    return cfg.preset;
  }
  if (cfg.preset === "custom") return "custom";
  return toggleMatch;
}

export interface WriteState {
  mongo_written: boolean;
  qdrant_written: boolean;
  neo4j_written: boolean;
  warnings?: string[];
  // Phase E — post-write verification result (null = not yet run)
  verified?: boolean | null;
  verify_errors?: string[];
}

export type IngestJobStatus =
  | "queued"
  | "processing"
  | "done"
  | "failed"
  | "skipped_duplicate"
  | "awaiting_summary"
  | "queryable"
  | "queryable_with_pending_summary"
  | "queryable_with_pending_graph"
  | "queryable_with_pending_summary_and_graph";

export type IngestJobStage =
  | "uploading"
  | "ingesting"
  | "embedding"
  | "summary_indexing"
  | "graph_extracting"
  | "verifying"
  | "verified"
  | "verify_failed"
  | "finalized"
  | "failed"
  | "skipped_duplicate"
  | "awaiting_summary"
  | "queryable"
  | "queryable_with_pending_summary"
  | "queryable_with_pending_graph"
  | "queryable_with_pending_summary_and_graph";

export interface PresetMode {
  label: string;
  use_neo4j: boolean;
  chunk_summarization: boolean;
  target_qdrant_collections: string[];
}

export const PRESET_MODES: Record<string, PresetMode> = {
  Fast: {
    label: "Fast",
    use_neo4j: false,
    chunk_summarization: false,
    target_qdrant_collections: ["naive", "hrag"],
  },
  Balanced: {
    label: "Balanced",
    use_neo4j: true,
    chunk_summarization: false,
    target_qdrant_collections: ["naive", "hrag", "graph"],
  },
  Deep: {
    label: "Deep",
    use_neo4j: true,
    chunk_summarization: true,
    target_qdrant_collections: ["naive", "hrag", "graph"],
  },
};

export const DEFAULT_INGESTION_CONFIG: IngestionConfig = {
  embedding_model: "Qwen/Qwen3-Embedding-0.6B",
  embedding_dimension: 1024,
  embedding_model_id: "qwen3-embedding-0.6b-v1",
  embed_mode: "local",
  embed_base_url: null,
  embed_api_key: null,
  embed_max_concurrent: null,
  embedding_models: [],
  modal_containers: null,
  parent_chunk_tokens: {
    min_tokens: 400,
    target_tokens: 1200,
    max_tokens: 1800,
  },
  child_chunk_tokens: { min_tokens: 64, target_tokens: 128, max_tokens: 256 },
  chunk_overlap: 200,
  max_summary_tokens: 175,
  child_chunk_algorithm: "semantic_split",
  // Phase 24 — empty by default. The corpus editor populates real entries
  // from the user's pool (Settings → Models). Hardcoding ollama/llama3.2:3b
  // silently bound new corpora to a model the user may not have pulled.
  summary_models: [],
  extraction_models: [],
  entity_confidence_threshold: 0.5,
  models_linked: false,
  // New corpora are EXPLICIT about the extraction workflow — never "inherit".
  // Modern extraction is provider-card LLM based; the UI scaffolds a private
  // RTX/vLLM chip for create flows. "legacy_local" is the deprecated sidecar.
  extraction_engine: "local",
  // entity_schema / relation_schema / schema_strict intentionally omitted —
  // backend fills them from the universal schema on POST.
  use_neo4j: true,
  chunk_summarization: false,
  docling_ocr_enabled: false,
  target_qdrant_collections: ["naive", "hrag", "graph"],
  preset: "balanced",
};

export interface CorpusCreate {
  name: string;
  description?: string | null;
  default_ingestion_config: IngestionConfig;
}

export interface CorpusUpdate {
  name?: string;
  description?: string | null;
  default_ingestion_config?: IngestionConfig;
}

export interface CorpusReadiness {
  corpus_id: string;
  status: string;
  schema_version?: string;
  source?: string;
  computed_at?: string;
  stale?: boolean;
  refresh_error?: string;
  blocking?: string[];
  error?: string;
  next_actions?: Array<{
    id: string;
    label: string;
    lane: string;
    severity: "critical" | "warning" | "review" | string;
    reason: string;
    count: number;
    blocked_by_pressure?: boolean;
  }>;
  documents?: {
    total: number;
    registered_total?: number;
    excluded_total?: number;
    queryable: number;
    fully_enriched: number;
    verified: number;
    failed: number;
    coverage: number;
    fully_enriched_coverage: number;
    stage_counts: Record<string, number>;
  };
  chunks?: {
    total: number;
    docs_with_chunks: number;
  };
  summaries?: {
    scopes?: Record<
      string,
      | string
      | {
          label?: string;
          description?: string;
          readiness_gate?: boolean;
          includes_chunk_kinds?: string[];
          includes_missing_chunk_kind?: boolean;
        }
    >;
    primary_parent_scope?: "retrieval_parent" | string;
    primary_parent_label?: string;
    /** Legacy parent_* fields alias this scope. */
    parent_alias_scope?: "retrieval_parent" | string;
    /** Canonical retrieval-summary gate. Same values as retrieval_parent_*; excludes structural parent rows. */
    parent_total: number;
    parent_done: number;
    parent_missing: number;
    parent_coverage: number;
    /** Diagnostic count across all parent rows, including code/navigation/bibliography rows. */
    all_parent_total?: number;
    all_parent_done?: number;
    all_parent_missing?: number;
    all_parent_coverage?: number;
    summary_excluded_parent_total?: number;
    summary_excluded_parent_done?: number;
    retrieval_parent_total: number;
    retrieval_parent_done: number;
    retrieval_parent_missing: number;
    retrieval_parent_coverage: number;
    /** Pure body-prose diagnostic only. Retrieval readiness also includes eligible table/legacy parent rows. */
    body_parent_total: number;
    body_parent_done: number;
    body_parent_missing: number;
    body_parent_coverage: number;
    document_total: number;
    document_done: number;
    document_missing: number;
    document_coverage: number;
    document_synced_done?: number;
    document_sync_missing?: number;
    document_sync_coverage?: number;
    document_profile_done: number;
    document_tree_done: number;
    document_both_done?: number;
    document_profile_only?: number;
    document_tree_only?: number;
    document_mismatch?: number;
  };
  graph?: {
    required: boolean;
    promoted: number;
    pending: number;
    unpromoted_extraction_docs?: number;
    unpromoted_extraction_rows?: number;
    /** Legacy metadata drift: docs are graph-written, but old extraction artifacts lack promoted_at. */
    unmarked_promoted_extraction_docs?: number;
    unmarked_promoted_extraction_rows?: number;
    failed_docs: number;
    failed_chunks: number;
    failure_docs: number;
    failure_rows: number;
    stale_failure_docs: number;
    stale_failure_rows: number;
    reconciled_stale_failure_docs: number;
    reconciled_stale_failure_rows: number;
    orphaned_failure_docs: number;
  };
  idempotency?: {
    source_keyed_documents: number;
    content_hash_documents: number;
    missing_source_identity: number;
    stage_identity_missing_total?: number;
    stage_identity_blocking_total?: number;
    source_parse_jobs_missing_stage_identity?: number;
    document_pipeline_jobs_missing_stage_identity?: number;
    extraction_jobs_missing_stage_identity?: number;
    summary_jobs_missing_stage_identity?: number;
    graph_promotion_jobs_missing_stage_identity?: number;
    ghost_b_extractions_missing_stage_identity?: number;
    ghost_b_extractions_missing_stage_identity_blocking?: number;
    ghost_b_extractions_missing_stage_identity_legacy_ok?: number;
    duplicate_source_key_groups: number;
    duplicate_source_key_docs: number;
    source_key_collision_groups?: number;
    source_key_collision_docs?: number;
    duplicate_content_hash_groups: number;
    duplicate_content_hash_docs: number;
  };
  repair?: {
    active_runs: number;
    source_parse_jobs?: Record<string, number>;
    source_parse_jobs_pending?: number;
    source_parse_jobs_failed?: number;
    document_pipeline_jobs?: Record<string, number>;
    document_pipeline_jobs_pending?: number;
    document_pipeline_jobs_failed?: number;
    graph_promotion_jobs?: Record<string, number>;
    extraction_jobs?: Record<string, number>;
    extraction_jobs_pending?: number;
    extraction_jobs_failed?: number;
    extraction_jobs_blocked?: number;
    provider_lane_health?: {
      status?: string;
      window_minutes?: number;
      sample_size?: number;
      cooldown_keys?: string[];
      lanes?: Array<{
        key?: string;
        provider?: string;
        model?: string;
        lane?: number;
        attempts?: number;
        succeeded?: number;
        failed?: number;
        rate_limited?: number;
        rate_limit_ratio?: number;
        status?: string;
        reasons?: string[];
      }>;
    };
    provider_efficiency?: {
      window_hours?: number;
      calls?: number;
      billable_calls?: number;
      local_calls?: number;
      attempted_items?: number;
      accepted_artifacts?: number;
      calls_per_artifact?: number | null;
      tokens_per_artifact?: number | null;
      input_tokens?: number;
      output_tokens?: number;
      retries?: number;
      rate_limits?: number;
      providers?: Record<
        string,
        { calls?: number; accepted?: number; input_tokens?: number; output_tokens?: number }
      >;
    };
    scheduler?: {
      idle_ticks?: number;
      no_op_cycles?: number;
      next_eligible_at?: string;
      updated_at?: string;
      last_changed?: boolean;
    };
    queue_telemetry?: {
      dead_letter_total?: number;
      lanes?: Record<
        string,
        { dead_letter?: number; oldest_actionable_age_seconds?: number }
      >;
    };
    summary_jobs?: Record<string, number>;
    summary_jobs_pending?: number;
    summary_jobs_waiting_dependencies?: number;
    summary_jobs_failed?: number;
    latest_runs?: Array<Record<string, unknown>>;
  };
  pressure?: {
    status: "normal" | "elevated" | "high" | string;
    reasons?: string[];
    recommendations?: string[];
    resources?: {
      backend_rss_mb?: number | null;
      ram_cap_mb?: number | null;
      rss_soft_limit_mb?: number | null;
      rss_pressure?: number | null;
    };
    queues?: {
      active_repairs?: number;
      graph_pending?: number;
      extraction_pending?: number;
      summary_missing?: number;
    };
    storage?: {
      mongo_storage_bytes?: number;
      mongo_data_bytes?: number;
      mongo_index_bytes?: number;
      mongo_objects?: number;
      mongo_fs_used_bytes?: number;
      mongo_fs_total_bytes?: number;
      mongo_fs_pressure?: number | null;
      mongo_storage_warn_ratio?: number;
      mongo_storage_stop_ratio?: number;
    };
    limits?: {
      qdrant_write_concurrency?: number | null;
      neo4j_write_concurrency?: number | null;
    };
    writers?: {
      qdrant?: {
        status?: string;
        reasons?: string[];
        write_latency_ms?: number | null;
        queue_depth?: number;
        queue_warn?: number;
        queue_stop?: number;
        source?: string;
        max_queue_depth?: number;
        deferred_points?: number;
        collections_total?: number;
        vectors_total?: number;
        memory_resident_bytes?: number;
        memory_allocated_bytes?: number;
        memory_active_bytes?: number;
        memory_retained_bytes?: number;
        memory_limit_bytes?: number;
        memory_pressure?: number;
        memory_warn_ratio?: number;
        memory_stop_ratio?: number;
      };
      neo4j?: {
        status?: string;
        reasons?: string[];
        write_latency_ms?: number | null;
        queue_depth?: number;
        queue_warn?: number;
        queue_stop?: number;
      };
    };
    backpressure?: {
      source_parse_allowed?: boolean;
      document_pipeline_allowed?: boolean;
      summary_generation_allowed?: boolean;
      summary_indexing_allowed?: boolean;
      summary_backfill_allowed?: boolean;
      extraction_backfill_allowed?: boolean;
      graph_promotion_allowed?: boolean;
    };
  };
}

export interface CorpusResponse {
  corpus_id: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  doc_count: number;
  /** Fully verified docs — doc_count includes in-flight/failed rows. */
  ready_doc_count?: number;
  chunk_count: number;
  embedding_model_id: string | null;
  default_ingestion_config: IngestionConfig;
  readiness?: CorpusReadiness | null;
}

export interface ParentChunk {
  parent_id: string;
  text: string;
  token_count: number;
  heading_path: string[];
  summary: string | null;
}

export interface DocumentResponse {
  doc_id: string;
  corpus_id: string;
  source_path?: string;
  source_mime?: string;
  filename?: string;
  source_tier: string;
  ingestion_config: IngestionConfig;
  parent_chunks: ParentChunk[];
  /** Child-chunk count from `chunks` collection — the retrieval/embedding
   *  unit. Distinct from parent_chunks.length which is the context unit. */
  chunk_count?: number;
  /** Parent chunk count from `parent_chunks` collection; document list
   *  responses intentionally omit the heavy parent payload. */
  parent_count?: number;
  doc_summary?: string | null;
  entities_extracted?: boolean;
  is_near_duplicate?: boolean;
  near_duplicate_candidates?: Array<{
    doc_id?: string | null;
    filename?: string;
    similarity: number;
  }>;
  ghost_b_failures?: Array<{
    chunk_id: string;
    error_type?: string;
    error_message?: string;
    attempts?: number;
    model?: string;
  }>;
	  ghost_b_metrics?: {
	    requested_chunks?: number;
	    extracted_chunks?: number;
	    failed_chunks?: number;
	    success_rate?: number;
	    total_tokens?: number;
	    relation_count?: number;
	    related_to_count?: number;
	    related_to_ratio?: number;
	    fact_count?: number;
	    validation_rejection_count?: number;
	    routing_policy?: string;
	    lane_call_counts?: Record<string, number>;
	    provider_call_counts?: Record<string, number>;
	    model_call_counts?: Record<string, number>;
	    error_counts?: Record<string, number>;
	    domain_range_remap_count?: number;
	    /** "off" when the corpus contract disabled extraction (vectors-only). */
	    engine?: string;
	  };
  write_state: WriteState;
  ingested_at?: string;
  created_at?: string;
  updated_at?: string;
  user_id?: string | null;
  file_id?: string | null;
}

export interface ChunkResponse {
  chunk_id: string;
  doc_id: string;
  corpus_id: string;
  parent_id: string;
  text: string;
  token_count: number;
  heading_path: string[];
  source_tier: string;
  user_id: string | null;
  file_id: string | null;
}

export interface IngestJobResponse {
  job_id: string;
  doc_id: string;
  corpus_id: string;
  filename: string;
  source_tier: string | null;
  status: IngestJobStatus;
  stage?: IngestJobStage;
  write_state: WriteState;
  chunk_count: number;
  parent_count: number;
  error: string | null;
}

export interface IngestBatchItemResponse {
  item_id: string;
  batch_id: string;
  corpus_id: string;
  filename: string;
  relative_path?: string;
  source_path?: string;
  stored_path?: string | null;
  status:
    | "queued"
    | "running"
    | "staged"
    | "done"
    | "failed"
    | "failed_recoverable"
    | "skipped";
  attempts: number;
  phase?: string | null;
  stage?: string | null;
  stage_rank?: number | null;
  failure_stage?: string | null;
  doc_id?: string | null;
  error?: string | null;
  size_bytes?: number;
  stored_bytes?: number;
  last_heartbeat_at?: string | null;
  phase_started_at?: string | null;
  updated_at?: string;
}

export interface IngestBatchResponse {
  batch_id: string;
  corpus_id: string;
  source: "local_folder" | string;
  root_path?: string;
  store_files?: boolean;
  total_source_bytes?: number;
  stored_bytes?: number;
  storage_limit_bytes?: number | null;
  status: "queued" | "running" | "done" | "partial" | "failed" | "cancelled";
  total: number;
  counts: Record<string, number>;
  /** Owner metric: FILES + MB with an explicit "extracted" milestone —
   * extraction finished (phase past ghosts) even while embeds/writes
   * continue. Computed by refresh_batch_counts on every item completion. */
  progress?: {
    files_done: number;
    files_total: number;
    files_extracted: number;
    files_queryable?: number;
    files_graph_extracted?: number;
    mb_done: number;
    mb_extracted: number;
    mb_queryable?: number;
    mb_graph_extracted?: number;
    mb_total: number;
    ladder?: Record<string, number>;
  };
	  options?: Record<string, unknown>;
	  report?: {
	    docs?: number;
	    docs_verified?: number;
	    parents?: number;
	    parents_summary_required?: number;
	    parents_summary_skipped?: number;
	    parents_summarized?: number;
	    parents_summary_required_summarized?: number;
	    parents_summary_missing_required?: number;
	    parents_structured?: number;
	    parents_summary_required_structured?: number;
	    summary_coverage_rate?: number | null;
	    summary_fallback_rate?: number | null;
	    summary_raw_missing_rate?: number | null;
	    structure_rate?: number | null;
	    children?: number;
	    children_promoted?: number;
	    ghost_b_requested_chunks?: number;
	    ghost_b_extracted_chunks?: number;
	    ghost_b_failed_chunks?: number;
	    ghost_b_success_rate?: number | null;
	    ghost_b_docs_requested?: number;
	    ghost_b_docs_partial?: number;
	    ghost_b_docs_dead?: number;
	    ghost_b_related_to_ratio?: number;
	    ghost_b_validation_rejection_count?: number;
	    ghost_b_lane_call_counts?: Record<string, number>;
	    ghost_b_provider_call_counts?: Record<string, number>;
	    ghost_b_model_call_counts?: Record<string, number>;
	    alerts?: string[];
	    alert?: string;
	    [key: string]: unknown;
	  };
	  runner_started?: boolean;
	  appended_items?: number;
  discovered_files?: number;
  items?: IngestBatchItemResponse[];
}

export interface LocalIngestBatchRequest {
  root_path: string;
  profile?: IngestProfileName | null;
  recursive?: boolean;
  extensions?: string[];
  max_files?: number;
  store_files?: boolean;
  max_total_bytes?: number;
  use_neo4j?: boolean;
  chunk_summarization?: boolean;
  model?: string;
  concurrency?: number;
  start?: boolean;
}

export interface CorpusDeleteResponse {
  status: string;
  message: string;
}

/**
 * Per-corpus extraction workflow. Two-toggle mental model: local on →
 * "local", cloud on → "cloud", both → "dual", neither → "off"; "inherit"
 * is the legacy global-Settings fallback (stamped away by migration).
 */
export type ExtractionEngine =
  | "inherit"
  | "off"
  | "local"
  | "cloud"
  | "legacy_local"
  | "dual"
  | "local_then_cloud"
  | "local_then_enrich";

/** GET /api/corpora/{id}/extraction-contract — the resolved truth. */
export interface ExtractionContractResponse {
  engine: Exclude<ExtractionEngine, "inherit">;
  source: "corpus" | "global" | "default";
  models_linked: boolean;
  pool_source: "extraction_models" | "summary_models" | "none";
  routing_policy?: "work_stealing" | "balanced" | "primary_fallback" | null;
  lane_capacities?: Array<{
    lane: number;
    provider?: string | null;
    model?: string | null;
    max_concurrent?: number | null;
    concurrency_policy?: "static_lane_cap" | "adaptive_vram_85" | string;
    local_private?: boolean;
  }>;
  pool: Array<{
    provider_preset?: string | null;
    model: string;
    base_url?: string | null;
    max_concurrent?: number | null;
    lifecycle_base_url?: string | null;
    lifecycle_auto_start?: boolean | null;
    lifecycle_auto_stop?: boolean | null;
    provider_card?: {
      provider: string;
      model: string;
      endpoint: string;
      auth_mode: string;
      schema_mode: "json_schema" | "json_object" | "json_object_prompt" | "jsonl";
      json_repair_mode:
        | "provider_native"
        | "balanced_object_repair"
        | "jsonl_repair_resume"
        | "deterministic_compiler";
      semantic_verifier_mode: "strict" | "strict_with_direction_repair";
      concurrency_policy: "static_lane_cap" | "adaptive_vram_85";
      failure_backfill_policy: "retry_then_stage" | "stage_failures";
      supports_json_schema: boolean;
      supports_json_object: boolean;
      disable_thinking: boolean;
      local_private: boolean;
      managed_vllm: boolean;
      lifecycle_base_url: string;
      promotion_gate: string[];
      notes: string[];
    } | null;
    lifecycle_status?: {
      ok: boolean;
      ready: boolean;
      gpu_vram_total_gb?: number | null;
      gpu_vram_used_gb?: number | null;
      gpu_vram_free_gb?: number | null;
      recommended_concurrency?: number | null;
      running_requests?: number;
      waiting_requests?: number;
      source?: string;
      error?: string | null;
    } | null;
  }>;
  endpoints: Array<{
    label?: string | null;
    url: string;
    enabled: boolean;
    /** null = not probed (engine does not use local sidecars) */
    alive: boolean | null;
  }>;
  errors: string[];
  warnings: string[];
}
