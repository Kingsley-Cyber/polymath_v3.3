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
  // Used when cloud extraction is enabled and models_linked === false.
  extraction_models: ModelProfileRef[];
  entity_confidence_threshold: number;

  /**
   * When true: worker reuses summary_models for GHOST B; UI renders one
   * combined chip pool. When false: extraction_models is an independent pool.
   */
  models_linked: boolean;

  /**
   * Per-corpus extraction contract (owner two-toggle model): local sidecars
   * on → "local", cloud pool on → "cloud", both → "dual", neither → "off".
   * "inherit" = legacy fallback to the global Settings engine (the lifespan
   * migration stamps existing corpora explicit). Resolved truthfully by
   * GET /api/corpora/{id}/extraction-contract.
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
  // New corpora are EXPLICIT about the extraction workflow — never "inherit"
  // (§13: the silent global fallback is how a corpus ran the wrong engine
  // for 14 hours). Local sidecars are the proven $0 default.
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
  status: "queued" | "processing" | "done" | "failed";
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
  status: "queued" | "running" | "done" | "partial" | "failed";
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
  | "dual"
  | "local_then_cloud"
  | "local_then_enrich";

/** GET /api/corpora/{id}/extraction-contract — the resolved truth. */
export interface ExtractionContractResponse {
  engine: Exclude<ExtractionEngine, "inherit">;
  source: "corpus" | "global" | "default";
  models_linked: boolean;
  pool_source: "extraction_models" | "summary_models" | "none";
  pool: Array<{
    model: string;
    base_url?: string | null;
    max_concurrent?: number | null;
    lifecycle_base_url?: string | null;
    lifecycle_auto_start?: boolean | null;
    lifecycle_auto_stop?: boolean | null;
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
