# backend/config.py
# Pydantic Settings - Single source of truth for all configuration
# NEVER hardcode values elsewhere - always import from here

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file="../.env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # === DATABASES ===
    MONGODB_URI: str = Field(
        default="mongodb://mongodb:27017/polymath", description="MongoDB connection URI"
    )
    MONGODB_DATABASE: str = Field(
        default="polymath", description="MongoDB database name"
    )

    # === VECTOR DATABASE ===
    QDRANT_URL: str = Field(
        default="http://qdrant:6333", description="Qdrant vector database URL"
    )
    QDRANT_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        description="HTTP timeout for Qdrant operations such as per-corpus collection provisioning",
    )
    QDRANT_PREFER_GRPC: bool = Field(
        default=True,
        description=(
            "Use gRPC for the hot search clients (funnel_a/funnel_b/lexical). "
            "Lower per-request overhead than HTTP/REST (~60x faster per call), "
            "which helps because a multi-lane turn issues many small query_points "
            "calls. ON by default after validation — the Qdrant gRPC port (6334) "
            "is internal to the docker network. Set false to fall back to HTTP/REST."
        ),
    )
    QDRANT_GRPC_PORT: int = Field(
        default=6334, description="Qdrant gRPC port (used when QDRANT_PREFER_GRPC=true)"
    )
    HYDRATION_MODE: str = Field(
        default="child_summary",
        description=(
            "How a matched child chunk is expanded for the LLM prompt. "
            "'child_summary' (default) keeps the precise child passage and "
            "appends the section summary as context — ~4x denser prompt, "
            "NotebookLM-style grounding; validated as the default. 'parent' "
            "replaces the child with the full parent body (legacy small-to-big)."
        ),
    )
    PARENT_EXCERPT_ENABLED: bool = Field(
        default=False,
        description=(
            "B2 — query-guided parent excerpt. When on, 'parent' hydration "
            "returns a query-centered window of the parent body (the matched "
            "child passage plus its neighbours and the highest answer-bearing "
            "paragraphs) instead of the full 1200-token block, once the parent "
            "exceeds PARENT_EXCERPT_MAX_CHARS. Denser prompt, same grounding. "
            "Off by default — A/B before defaulting on, like HYDRATION_MODE."
        ),
    )
    PARENT_EXCERPT_MAX_CHARS: int = Field(
        default=1600,
        ge=400,
        le=8000,
        description=(
            "Character budget for a B2 query-guided parent excerpt. Parents at "
            "or below this length are returned whole."
        ),
    )
    # ── Answerability / relationship gate (fluid cross-document synthesis) ──
    # The corpus supplies facts; the LLM supplies the bridge. These knobs move
    # both mirror gates (chat + retriever sufficiency) in lockstep via
    # services.answerability_tuning. Defaults are the LOOSENED settings so a
    # "how does X relate to Y" query answers whenever each side has >=1 source.
    RELATIONSHIP_GATE: str = Field(
        default="lenient",
        description=(
            "Strictness for the cross_document_relationship_evidence bridge atom. "
            "'strict' = legacy: the bridge atom is CRITICAL and a relationship "
            "query refuses unless lanes carry enough distinct docs. 'lenient' "
            "(default) = bridge atom is required-but-not-critical, so the LLM "
            "synthesizes the link and the query answers (with a 'partial' caveat "
            "when coverage is mid). 'off' = never inject the bridge atom; "
            "relationship queries answer on per-lane coverage alone."
        ),
    )
    RELATIONSHIP_MIN_DISTINCT_DOCS: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "Distinct documents across relationship lanes before the cross-doc "
            "bridge atom counts as covered. Replaces the hardcoded min(2, lanes). "
            "1 lets a single distinct doc satisfy the bridge."
        ),
    )
    RERANK_EVIDENCE_SUPPORT: bool = Field(
        default=False,
        description=(
            "Cross-encoder rerank for evidence-plan SUPPORT retrievals (the "
            "per-lane gap-fill passes). Lane selection is lexical "
            "(evidence_lane_match_score); reranking the support pool surfaces "
            "the right PASSAGE within the right book — A/B validated on the "
            "Le Guin sentence-rhythm probe (2026-07-01): off = right book, "
            "wrong passage; on = quotes the actual rhythm passage. Default "
            "OFF: the same A/B measured retrieval-phase p50 ~12s -> ~31s, "
            "because support rerank contends with the embedder on the single "
            "Metal GPU (embed stage spiked to 8.6s). Turn on for "
            "quality-first sessions; the durable passage-precision fix is "
            "query-guided excerpts (roadmap B2), which costs CPU not GPU."
        ),
    )
    RELATIONSHIP_LANE_MIN_SOURCES: int = Field(
        default=1,
        ge=1,
        le=3,
        description=(
            "Distinct STRONG docs each relationship lane needs to be 'covered'. "
            "Replaces MULTI_CONCEPT_MIN_SOURCES=2. At 1, a side backed by one "
            "strong doc is enough; a side with zero evidence still refuses."
        ),
    )
    LANE_STRONG_SCORE: int = Field(
        default=8,
        ge=4,
        le=12,
        description=(
            "Minimum evidence_lane_match_score for a chunk to count toward lane "
            "coverage. Lower toward 5 to let alias/term co-occurrence count."
        ),
    )
    ANSWERABILITY_COVERAGE_THRESHOLD: float = Field(
        default=0.80,
        ge=0.40,
        le=0.95,
        description=(
            "Required-atom coverage to answer without the text-help branch. "
            "Shared by both gates. Lower toward 0.70 to widen answerability."
        ),
    )
    ANSWERABILITY_TEXT_HELP_THRESHOLD: float = Field(
        default=0.50,
        ge=0.30,
        le=0.80,
        description=(
            "Coverage floor for the lexical text-help answer branch (a concept "
            "whose term appears in retrieved text counts as covered)."
        ),
    )
    ANSWERABILITY_PARTIAL_FLOOR: float = Field(
        default=0.50,
        ge=0.20,
        le=0.70,
        description=(
            "Coverage boundary between 'partial' (caveat answer) and 'weak' "
            "(refuse), and the floor for the relationship carve-out."
        ),
    )
    ANSWERABILITY_CHUNK_GATE: Literal["off", "soft", "strict"] = Field(
        default="off",
        description=(
            "Final-context per-chunk answerability filter. 'off' leaves the "
            "selected evidence packet unchanged. 'soft' demotes/drops chunks "
            "with no answer-bearing overlap when enough evidence remains. "
            "'strict' also drops weak partial overlaps. Default off until A/B "
            "evaluation proves it improves final evidence quality."
        ),
    )
    ANSWERABILITY_CHUNK_GATE_MIN_KEEP: int = Field(
        default=4,
        ge=1,
        le=20,
        description=(
            "Minimum final chunks retained when ANSWERABILITY_CHUNK_GATE is on. "
            "Weak chunks may be demoted instead of dropped to honor this floor."
        ),
    )
    ANSWERABILITY_CHUNK_GATE_STRICT_FLOOR: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description=(
            "Per-chunk answerability score below which strict mode drops a chunk."
        ),
    )
    # ── Chunker routers (POLYMATH_ARCHITECTURE §3.S2, routers 1+2) ──
    CHUNKER_STRUCTURED_ROUTERS: bool = Field(
        default=True,
        description=(
            "Layer-2 structured-text routing in the child/parent splitters: "
            "oversize LIST blocks split at item boundaries (items never broken) "
            "and line-structured low-punctuation blocks (transcripts, poetry, "
            "chat logs) group by lines instead of being shredded by sentence "
            "packing. Pure deterministic rules. Set false to revert to the "
            "pre-router paragraph/sentence-only behaviour."
        ),
    )
    CHUNKER_SEMANTIC_PARENTS: bool = Field(
        default=True,
        description=(
            "Semantic PARENT formation for structureless text (tier_c): parent "
            "boundaries at embedding-deviation dips between paragraph units "
            "instead of blind 1200-token windows, budget-clamped to "
            "[parent_min, parent_max]. Deterministic (fixed model+text → same "
            "boundaries); latched token-window fallback when the embedder is "
            "down. Structured tiers (headings/AST/pages) are unaffected."
        ),
    )
    CHUNKER_SEMANTIC_ESCALATION: bool = Field(
        default=True,
        description=(
            "Router 5 — semantic-deviation splitting for topic-fused oversize "
            "paragraphs (>=8 sentences): sentences batch-embedded via the local "
            "embedder, chunk boundaries at cosine-deviation dips (topic shifts) "
            "instead of arbitrary token counts. One embedder call per flagged "
            "block; latched greedy-packing fallback if the sidecar is down."
        ),
    )
    CHUNKER_SENTENCE_ENGINE: str = Field(
        default="sat",
        description=(
            "Sentence segmentation engine for oversize-paragraph splitting. "
            "'sat' (default) = wtpsplit SaT sat-3l-sm — punctuation-agnostic, "
            "85 languages, fixes no-punctuation text the [.!?] regex cannot "
            "split; falls back to the regex with a logged warning when the "
            "package/model is unavailable. 'regex' = legacy [.!?] splitting."
        ),
    )
    # ── HyDE: opt-in, not on every query (it costs an extra LLM round-trip) ──
    HYDE_ENABLED: bool = Field(
        default=False,
        description=(
            "Global master switch for HyDE (hypothetical-document retrieval "
            "rewrite). OFF by default — HyDE adds a pre-retrieval LLM call and is "
            "not worth it on most queries. When off, HyDE runs ONLY when a request "
            "explicitly toggles overrides.hyde_enabled. When on, profile presets "
            "decide (and source-constrained queries are still auto-skipped)."
        ),
    )
    # ── Resilience: retry the streaming LLM connection on transient blips ──
    LLM_STREAM_MAX_RETRIES: int = Field(
        default=2,
        ge=0,
        le=5,
        description=(
            "Retries for the streaming chat completion on TRANSIENT connection "
            "errors (DNS getaddrinfo, connect reset/refused/timeout) that happen "
            "BEFORE the first token — so a burst-load DNS blip no longer surfaces "
            "as a blank answer. Never retries after a token has streamed."
        ),
    )
    LLM_STREAM_RETRY_BACKOFF_SECONDS: float = Field(
        default=0.4,
        ge=0.0,
        le=5.0,
        description="Linear backoff between streaming-connection retries (s * attempt).",
    )
    QDRANT_COLLECTION: str = Field(
        default="polymath_chunks", description="Default Qdrant collection name"
    )
    QDRANT_UPSERT_BATCH_SIZE: int = Field(
        default=256,
        ge=1,
        le=2048,
        description=(
            "Maximum points per Qdrant upsert request. Large deep-ingest "
            "documents can produce thousands of dense+sparse points, and one "
            "huge HTTP payload is brittle under load."
        ),
    )

    # === GRAPH DATABASE ===
    NEO4J_ENABLED: bool = Field(
        default=False, description="Enable Neo4j graph database (optional)"
    )
    NEO4J_URI: str = Field(
        default="bolt://neo4j:7687", description="Neo4j bolt connection URI"
    )
    NEO4J_USER: str = Field(default="neo4j", description="Neo4j username")
    NEO4J_PASSWORD: str = Field(default="", description="Neo4j password")
    GRAPH_FACT_SEED_TIMEOUT_SECONDS: float = Field(
        default=5.0,
        ge=0.2,
        le=30.0,
        description=(
            "Hard cap for the optional Neo4j fact-seed lane in graph-augmented "
            "retrieval. If the vector-scoped graph lookup is slow or unhealthy, "
            "retrieval skips facts and continues with Qdrant/Mongo evidence "
            "instead of blocking the chat turn."
        ),
    )
    GRAPH_FACT_SEED_LIMIT: int = Field(
        default=16,
        ge=0,
        le=50,
        description=(
            "SINGLE source for the code default number of graph facts to seed per "
            "query. The per-user value lives in RetrievalSettings.graph_fact_seeds "
            "(the 'Fact seeds' slider) and overrides this; a per-request override "
            "trumps both. Used as the seeder's last-resort fallback so the default "
            "is defined in exactly one place. Now that fact lookup is indexed "
            "(entity_id) and no longer times out, this can be raised safely."
        ),
    )
    GRAPH_ENTITY_LIMIT: int = Field(
        default=8,
        ge=1,
        le=50,
        description=(
            "Maximum query-resolved graph entities used for fact seeding in "
            "Graph Augmentation. Keeps graph anchoring bounded and deterministic."
        ),
    )
    GRAPH_CHILD_TOP_K: int = Field(
        default=40,
        ge=1,
        le=150,
        description="Maximum child-vector/lexical recall budget for Graph Augmentation.",
    )
    GRAPH_SUMMARY_TOP_K: int = Field(
        default=20,
        ge=0,
        le=100,
        description="Maximum parent-summary recall budget for Graph Augmentation.",
    )
    GRAPH_SEED_CHUNKS: int = Field(
        default=8,
        ge=1,
        le=100,
        description=(
            "Maximum top hybrid chunks allowed to seed Neo4j Mode A expansion "
            "in Graph Augmentation."
        ),
    )
    GRAPH_EXPANSION_LIMIT: int = Field(
        default=8,
        ge=0,
        le=100,
        description=(
            "Server-side cap for graph-expanded chunks. Per-user expansion "
            "settings are clamped to this budget unless this environment value "
            "is intentionally raised."
        ),
    )
    GRAPH_EXPANSION_TIMEOUT_SECONDS: float = Field(
        default=4.0,
        ge=0.2,
        le=30.0,
        description=(
            "Wall-clock timeout for query-time Neo4j Mode A expansion. A timeout "
            "degrades to the hybrid seed pool instead of stalling Graph Augmentation."
        ),
    )
    GRAPH_REL_MIN_CONFIDENCE: float = Field(
        default=0.20,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum RELATES_TO confidence for query-time graph traversal. This "
            "is intentionally permissive so discovery keeps recall while still "
            "dropping the noisiest edges."
        ),
    )
    GRAPH_REL_GENERIC_MIN_CONFIDENCE: float = Field(
        default=0.35,
        ge=0.0,
        le=1.0,
        description=(
            "Stricter confidence floor for generic RELATES_TO predicates such as "
            "'related_to' during bounded graph expansion, especially beyond the "
            "first hop."
        ),
    )
    GRAPH_REL_HOP2_MIN_CONFIDENCE: float = Field(
        default=0.30,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum RELATES_TO confidence after the first hop. Multi-hop graph "
            "retrieval is useful but drifts faster, so second-hop expansion is "
            "slightly stricter than direct seed expansion."
        ),
    )
    GRAPH_PREFILTER_POOL: int = Field(
        default=64,
        ge=8,
        le=300,
        description="Maximum graph-tier candidate pool kept before MLX rerank.",
    )
    GRAPH_MLX_RERANK_POOL: int = Field(
        default=16,
        ge=1,
        le=200,
        description="Maximum graph-tier candidates sent to the MLX reranker.",
    )
    GRAPH_DECORATE_ENTITIES_PER_CHUNK: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Maximum mentioned seed entities expanded per final chunk during decoration.",
    )
    GRAPH_DECORATE_MAX_CHUNKS: int = Field(
        default=8,
        ge=1,
        le=50,
        description="Maximum final chunks decorated with detailed graph arrows.",
    )
    GRAPH_DECORATE_MAX_PATHS_PER_CHUNK: int = Field(
        default=3,
        ge=0,
        le=20,
        description="Maximum relation arrows requested per decorated answer.",
    )
    GRAPH_DECORATE_EVIDENCE_CHUNKS_PER_PATH: int = Field(
        default=2,
        ge=0,
        le=10,
        description="Maximum evidence chunk ids attached to each graph arrow.",
    )
    GRAPH_DECORATION_TIMEOUT_SECONDS: float = Field(
        default=0.75,
        ge=0.1,
        le=20.0,
        description=(
            "Best-effort timeout for final-only graph decoration. If it expires, "
            "the answer falls back to selected chunks and compact graph signals."
        ),
    )

    # === LLM GATEWAY ===
    LITELLM_URL: str = Field(
        default="http://litellm:4000", description="LiteLLM proxy URL"
    )
    LITELLM_MASTER_KEY: str = Field(
        default=..., description="LiteLLM master key for authentication"
    )

    # === OLLAMA ===
    OLLAMA_URL: str = Field(default="http://ollama:11434", description="Ollama API URL")
    OLLAMA_KEEP_ALIVE: str = Field(
        default="30m",
        description=(
            "Default keep_alive for native Ollama chat calls so a local chat "
            "model stays resident between turns instead of unloading after "
            "Ollama's ~5m idle default (which forces a multi-minute cold reload "
            "on the next turn). Applied only when the request does not set its "
            "own keep_alive. Set to '-1' to keep loaded indefinitely, '0' to "
            "unload immediately. No effect on remote-API models."
        ),
    )
    OLLAMA_WARMUP_MODEL: str = Field(
        default="",
        description=(
            "Optional model id to pre-load at startup with a tiny completion so "
            "the first chat turn skips the cold-load. Leave blank to disable "
            "(the chat model is user-selected, so there is no safe default). "
            "Set to your primary local chat model, e.g. 'ollama/llama3.2:3b'."
        ),
    )

    # === REDIS ===
    REDIS_URL: str = Field(
        default="redis://redis:6379", description="Redis connection URL"
    )

    # === EMBEDDER (sentence-transformers GPU service) ===
    EMBEDDER_URL: str = Field(
        default="http://embedder:80",
        description="Embedder service URL — GPU-accelerated, OpenAI-compatible /embeddings",
    )
    EMBEDDER_MODEL_NAME: str = Field(
        default="Qwen3-Embedding-0.6B",
        description="Display name of the loaded embedding model — must match MODEL_NAME env var in embedder container",
    )
    EMBED_BATCH_SIZE: int = Field(
        default=32,
        ge=1,
        le=512,
        description=(
            "Client-side embedding request batch size. Larger values reduce "
            "HTTP overhead and let local/MLX embedders use bigger internal "
            "encode batches, but raise memory pressure."
        ),
    )
    LOCAL_EMBEDDER_ENABLED: bool = Field(
        default=False,
        description="Whether the local Docker embedder profile is expected to be running.",
    )

    # === LOCAL ENRICHMENT (Pass-1 deterministic + Pass-2 SLM-residual) ===
    # Two independent flags; default off so cloud Ghost B behavior is unchanged.
    # Pass-1 is pure Python (services/ingestion/enrich.py) and bit-for-bit
    # reproducible. Pass-2 calls the host-native slm_enrich_mlx sidecar.
    LOCAL_PASS1_ENRICH_ENABLED: bool = Field(
        default=False,
        description="Run Pass-1 deterministic enrichment (numeric facts + in-text aliases) after Ghost B.",
    )
    LOCAL_SLM_ENRICH_ENABLED: bool = Field(
        default=False,
        description="Run Pass-2 SLM-residual enrichment (facets + out-of-text aliases + qualitative facts) via the slm_enrich_mlx sidecar.",
    )
    LOCAL_SLM_ENRICH_URL: str = Field(
        default="http://localhost:8083",
        description="slm_enrich_mlx sidecar URL. Use http://host.docker.internal:8083 when backend runs in Docker on the same Mac.",
    )
    LOCAL_SLM_ENRICH_TIMEOUT_S: float = Field(
        default=30.0,
        description="HTTP timeout for /enrich/* sidecar calls.",
    )

    # === MODAL CLOUD GPU (primary ingestion embed path) ===
    MODAL_ENABLED: bool = Field(
        default=False,
        description="Enable Modal cloud GPU embedding. Modal is the primary production path; local is fallback.",
    )
    MODAL_EMBEDDER_URL: str = Field(
        default="",
        description="Modal webhook URL for embedding — OpenAI-compatible /embeddings endpoint",
    )
    MODAL_API_KEY: str = Field(
        default="",
        description="Modal auth token. Server-side only; never round-tripped to frontend.",
    )
    MODAL_TIMEOUT_SECONDS: float = Field(
        default=300.0,
        description="HTTP timeout for Modal embed calls (cold starts can be 10-30s)",
    )

    # === SILICONFLOW CLOUD API (alternate cloud embedding provider) ===
    SILICONFLOW_ENABLED: bool = Field(
        default=False,
        description="Enable SiliconFlow cloud API for embedding. Primary production alternative to Modal.",
    )
    SILICONFLOW_EMBEDDER_URL: str = Field(
        default="https://api.siliconflow.com/v1/embeddings",
        description="SiliconFlow OpenAI-compatible /embeddings endpoint",
    )
    SILICONFLOW_API_KEY: str = Field(
        default="",
        description="SiliconFlow auth token (Bearer). Server-side only; never round-tripped to frontend.",
    )
    SILICONFLOW_TIMEOUT_SECONDS: float = Field(
        default=120.0,
        description="HTTP timeout for SiliconFlow embed calls (no cold-start penalty)",
    )
    EMBED_ALLOW_LOCAL_FALLBACK: bool = Field(
        default=False,
        description=(
            "When False, cloud/API embedding failures fail closed instead of "
            "silently falling back to the local GPU embedder."
        ),
    )
    INGEST_MAX_PARSE_JOBS: int = Field(
        default=2,
        ge=1,
        le=16,
        description="Process-local cap for concurrent parse/chunk phases.",
    )
    INGEST_BLOCK_NEAR_DUPLICATES: bool = Field(
        default=True,
        description=(
            "Skip ingesting a document that is a near-duplicate of one already "
            "in the same corpus (>= INGEST_NEAR_DUPLICATE_THRESHOLD lexical "
            "overlap). Stops the same book/file ingested in two formats (PDF + "
            "MD) from doubling a corpus's weight on those concepts."
        ),
    )
    INGEST_NEAR_DUPLICATE_THRESHOLD: float = Field(
        default=0.10,
        ge=0.02,
        le=1.0,
        description=(
            "Shingle (5-gram) Jaccard threshold to FLAG an incoming doc as a "
            "near-duplicate. Low on purpose — it only gates consideration; "
            "containment (below) decides whether to skip vs ingest-and-flag."
        ),
    )
    INGEST_NEAR_DUPLICATE_BLOCK_CONTAINMENT: float = Field(
        default=0.95,
        ge=0.50,
        le=1.0,
        description=(
            "Containment (|incoming ∩ existing| / |incoming|) at/above which an "
            "incoming doc is SKIPPED as a near-identical reformat. Below it, a "
            "near-duplicate is ingested and flagged for review instead of being "
            "dropped — so a distinct work that merely shares prose (e.g. a C++ vs "
            "Java edition, which reads as ~0.9 contained) is never silently lost."
        ),
    )
    INGEST_MAX_MODEL_PHASE_DOCS: int = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "Process-local cap for documents concurrently running LLM/embed "
            "model phases. Per-entry model concurrency still applies inside a slot."
        ),
    )
    INGEST_MAX_ACTIVE_JOBS: int = Field(
        default=16,
        ge=1,
        le=256,
        description=(
            "Process-local cap for active background ingest jobs retained in memory. "
            "Requests over this cap fail fast with 429 instead of holding uploaded bytes."
        ),
    )
    INGEST_BATCH_WORKERS: int = Field(
        default=1,
        ge=1,
        le=32,
        description=(
            "Default worker count for durable backend-owned local folder ingest batches."
        ),
    )
    INGEST_STALE_JOB_MINUTES: int = Field(
        default=30,
        ge=1,
        le=1440,
        description=(
            "Age after which durable ingest batch item leases are considered stale "
            "and can be marked failed_recoverable for resume."
        ),
    )
    INGEST_FILE_STORAGE_DIR: str = Field(
        default="/data/ingest-files",
        description=(
            "Durable backend-owned file spool used by local ingest batches when "
            "store_files=true."
        ),
    )
    INGEST_FILE_STORAGE_MAX_BYTES: int = Field(
        default=2 * 1024 * 1024 * 1024,
        ge=1,
        description="Maximum total bytes allowed in the durable ingest file spool.",
    )

    # === LOCAL MODELS DIR ===
    MODELS_DIR: str = Field(
        default="/models",
        description="Path to local HF model downloads (Docker volume → ./download)",
    )

    # === RERANKER ===
    RERANKER_URL: str = Field(
        default="http://reranker:8080",
        description="Reranker service URL (llama.cpp, MLX, or compatible sidecar).",
    )
    RERANKER_MODEL: str = Field(
        default="qwen3-reranker-0.6b-q8_0",
        description="Reranker model loaded by the local sidecar.",
    )
    RERANKER_SCORE_SCALE: Literal["logit", "cosine", "probability"] = Field(
        default="probability",
        description=(
            "Score scale returned by the reranker. logit supports negative "
            "low-confidence thresholds; cosine/probability are bounded 0..1."
        ),
    )
    RERANKER_LOW_CONFIDENCE_THRESHOLD: float = Field(
        default=-2.5,
        description=(
            "Top-score cutoff for dropping unrelated rerank results when "
            "RERANKER_SCORE_SCALE=logit and no query terms overlap."
        ),
    )
    RERANKER_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        ge=0.2,
        le=60.0,
        description=(
            "HTTP timeout for the reranker sidecar. Retrieval may call the "
            "reranker more than once during coverage repair, so this must stay "
            "short enough that a sick sidecar cannot stall a chat turn."
        ),
    )
    RERANKER_CIRCUIT_BREAKER_SECONDS: float = Field(
        default=120.0,
        ge=0.0,
        le=3600.0,
        description=(
            "After a reranker HTTP failure, skip sidecar calls for this many "
            "seconds and score-sort locally. Set 0 to disable the breaker."
        ),
    )
    LOCAL_RERANKER_ENABLED: bool = Field(
        default=False,
        description="Whether the local Docker reranker profile is expected to be running.",
    )

    # === AUTOMATION ===
    N8N_URL: str = Field(
        default="http://n8n:5678", description="n8n automation workflow URL"
    )

    # === DEFAULT MODELS ===
    # Phase 24 — empty by default. CLAUDE.md "never hardcode model names"
    # rule. The user's per-pool entries (Settings → Models) supply real
    # values; resolution falls back to the active chat model for HyDE /
    # Reasoning Cascade and raises a clear error when nothing is configured.
    DEFAULT_COMPLETION_MODEL: str = Field(
        default="",
        description=(
            "Last-resort fallback when neither the user's pool entry nor the "
            "active chat model is set. Leave blank — the resolver should never "
            "land here unless config is genuinely incomplete."
        ),
    )
    DEFAULT_EMBEDDING_MODEL: str = Field(
        default="tei/qwen3-embedding",
        description="Default embedding model — routes through LiteLLM to TEI embedder",
    )

    # === EMBEDDING SETTINGS ===
    EMBEDDING_DIMENSION: int = Field(
        default=1024,
        description="Embedding vector dimension — must match model output. Qwen3-Embedding-0.6B=1024. Changing requires full Qdrant re-index.",
    )

    # === CHUNKING SETTINGS ===
    CHUNK_SIZE: int = Field(
        default=1000,
        description=(
            "Legacy compatibility knob only. The ingestion hot path uses "
            "IngestionConfig.parent_chunk_tokens plus tier-aware heading/page/"
            "AST/table splitting, not fixed character chunking."
        ),
    )
    CHUNK_OVERLAP: int = Field(
        default=200,
        description=(
            "Legacy compatibility knob only. Current chunk overlap is resolved "
            "as parent-overlap tokens from IngestionConfig.chunk_overlap."
        ),
    )
    CHILD_CHUNK_SIZE: int = Field(
        default=300,
        description=(
            "Legacy compatibility knob only. Current child chunks use "
            "IngestionConfig.child_chunk_tokens with sentence-boundary merging "
            "and embedder safety caps."
        ),
    )

    # === TOKEN LIMITS ===
    MAX_CONTEXT_TOKENS: int = Field(
        default=4096, description="Maximum context window tokens"
    )
    MAX_COMPLETION_TOKENS: int = Field(
        default=16384, description="Maximum completion tokens"
    )
    RESERVE_TOKENS: int = Field(
        default=500, description="Tokens reserved for system prompt and response"
    )

    # === RETRIEVAL SETTINGS ===
    DEFAULT_RETRIEVAL_K: int = Field(
        default=5, description="Default number of chunks to retrieve"
    )
    SIMILARITY_THRESHOLD: float = Field(
        default=0.0, description="Minimum similarity score for retrieval; 0 disables the hard score gate"
    )

    # === QDRANT COLLECTION NAMES ===
    QDRANT_NAIVE: str = Field(
        default="polymath_naive",
        description="Qdrant collection for all-tier child + summary vectors",
    )
    QDRANT_HRAG: str = Field(
        default="polymath_hrag",
        description="Qdrant collection for Tier A/B heading-aware vectors only",
    )
    QDRANT_GRAPH: str = Field(
        default="polymath_graph",
        description="Qdrant collection for graph-aligned vectors (use_neo4j=True path)",
    )
    QDRANT_SCHEMAS: str = Field(
        default="polymath_schemas",
        description=(
            "Phase 14.2 — single collection for both entity-type and "
            "relation-predicate schema-term embeddings. payload.kind ∈ "
            "{entity_type, relation} discriminates."
        ),
    )
    QDRANT_COLLECTION_PREFIX: str = Field(
        default="corpus_",
        description=(
            "Phase 7.5 — per-corpus collection naming. Final name shape: "
            "f'{prefix}{corpus_id[:8]}_{kind}' where kind ∈ "
            "{naive,hrag,graph,schemas}. The legacy QDRANT_* names above are "
            "retained for the one-shot migration script's source-side reads."
        ),
    )

    # === GHOST B — UNIVERSAL SCHEMA ===
    FORCE_UNIVERSAL_SCHEMA: bool = Field(
        default=False,
        description=(
            "Lifespan admin lever. When False (default), corpora with a "
            "null/empty schema are patched to the universal schema; corpora "
            "with a legacy custom schema are preserved untouched. When True, "
            "ALL corpora are overwritten with the universal schema on startup."
        ),
    )

    # === GHOST B — ONTOLOGY-LITE (Phase 14.2) ===
    SCHEMA_INLINE_LIMIT: int = Field(
        default=30,
        description=(
            "Threshold above which the ghost_b prompt switches from inlining "
            "the full schema to retrieving top-K terms per chunk via Qdrant."
        ),
    )
    SCHEMA_RETRIEVAL_TOP_K: int = Field(
        default=10,
        description="Number of schema terms to retrieve per chunk when over SCHEMA_INLINE_LIMIT.",
    )
    SCHEMA_LENS_LLM_ENABLED: bool = Field(
        default=True,
        description=(
            "When True, the first Ghost B run for a corpus makes one bounded "
            "LLM call to profile a soft schema lens. The lens is cached on the "
            "corpus and still clamps all suggestions to the approved schema."
        ),
    )
    SCHEMA_LENS_SAMPLE_CHUNKS: int = Field(
        default=8,
        ge=1,
        le=32,
        description="Max child chunks sampled when creating the auto schema lens.",
    )
    SCHEMA_LENS_SAMPLE_CHARS: int = Field(
        default=6000,
        ge=1000,
        le=20000,
        description="Max text characters sent to the schema lens profiler.",
    )

    # === MCP — Phase 8 Integration ===
    MCP_HOST: str = Field(
        default="0.0.0.0",
        description="Bind host for the MCP sidecar's streamable-HTTP server.",
    )
    MCP_PORT: int = Field(
        default=8765,
        description="Port for the MCP sidecar (exposed inside docker-compose network).",
    )
    MCP_TRANSPORT: Literal["streamable-http", "stdio"] = Field(
        default="streamable-http",
        description=(
            "MCP transport selector. 'streamable-http' runs the ASGI server on "
            "MCP_HOST:MCP_PORT; 'stdio' attaches to stdin/stdout for local Claude "
            "Desktop integration via a host-side proxy."
        ),
    )
    MCP_DEFAULT_TOP_K: int = Field(
        default=5,
        description="Default top_k for polymath_search when client omits the param.",
    )
    MCP_REQUIRE_AUTH: bool = Field(
        default=True,
        description=(
            "When True, MCP tools reject requests without a valid JWT or API key. "
            "Set False only for trusted single-user local development."
        ),
    )
    MCP_API_KEY: str | None = Field(
        default=None,
        description=(
            "Static bearer token for system-level MCP access (no per-user corpus "
            "scoping — sees all corpora). When set, the auth middleware tries it "
            "BEFORE JWT validation via constant-time compare. Use for trusted "
            "agents (openclaw, cron jobs, internal tools) that don't manage user "
            "JWTs. Leave unset to require JWT for every request. Never commit; "
            "set via .env. Recommended: 32+ random bytes (`openssl rand -hex 32`)."
        ),
    )
    MCP_PUBLIC_URL: str = Field(
        default="",
        description=(
            "Public base URL for the MCP sidecar, for example a Cloudflare "
            "Tunnel hostname. Used only by /api/mcp/info to render client snippets."
        ),
    )
    # --- MCP write surface (corpus / document lifecycle tools) ---------------
    # The ingest-from-url and base64-upload tools share these limits. They
    # exist so an enthusiastic agent (OpenClaw, etc.) cannot drive the
    # ingestion pipeline harder than a normal HTTP client could.
    MCP_INGEST_MAX_BYTES: int = Field(
        default=50 * 1024 * 1024,
        ge=1024,
        description=(
            "Hard cap on document size for polymath_ingest_from_url and "
            "polymath_upload_document. Bigger files must go through the "
            "multipart HTTP endpoint instead."
        ),
    )
    MCP_INGEST_URL_TIMEOUT_SECONDS: float = Field(
        default=60.0,
        gt=0.0,
        description=(
            "Timeout for httpx GET in polymath_ingest_from_url. Short to keep "
            "agent loops responsive; large files should use the multipart "
            "endpoint anyway."
        ),
    )
    MCP_INGEST_URL_ALLOW_PRIVATE: bool = Field(
        default=False,
        description=(
            "When False (default), polymath_ingest_from_url blocks URLs that "
            "resolve to loopback, link-local, or RFC1918 private ranges. "
            "Prevents an agent from being tricked into SSRF against internal "
            "services. Flip to True only on isolated networks where private "
            "ingest sources are expected."
        ),
    )

    # === GHOST A — PARENT SUMMARIZATION ===
    SUMMARY_MAX_CONCURRENT: int = Field(
        default=1,
        description="Max concurrent LiteLLM calls for parent summarization (GHOST A)",
    )
    SUMMARY_MAX_TOKENS: int = Field(
        default=175,
        description="Token cap per parent summary output (GHOST A)",
    )

    # === GHOST B — ENTITY EXTRACTION ===
    EXTRACTION_MAX_CONCURRENT: int = Field(
        default=1,
        description="Max concurrent LiteLLM calls for entity extraction (GHOST B)",
    )
    EXTRACTION_MAX_TOKENS: int = Field(
        default=6144,
        ge=256,
        le=8192,
        description=(
            "Maximum completion tokens for the normal foreground entity extraction "
            "call (GHOST B). 6144 sits comfortably above realistic worst-case "
            "output (~1100-1500 tokens for dense relation-heavy chunks) and "
            "leaves headroom for reasoning-mode providers if thinking ever "
            "slips back on. Facts do not raise this cap; failed chunks switch "
            "to the rescue profile instead of retrying this exact prompt."
        ),
    )
    EXTRACTION_OUTPUT_MODE: Literal["auto", "json_object", "jsonl"] = Field(
        default="jsonl",
        description=(
            "Ghost B output transport. JSONL is enforced for foreground "
            "extraction; legacy auto/json_object values are accepted but "
            "treated as JSONL."
        ),
    )
    # Pt 8b — Pydantic Literal-typed validation is now a core / intrinsic
    # step in the extraction pipeline (not a flag). Removed in favor of
    # unconditional execution alongside the Pt 7f entity evidence gate and
    # the Phase B relation evidence gate. To roll back, revert the
    # validation block in services/ghost_b.py._parse — there is no env
    # var override.
    EXTRACTION_JSON_OBJECT_MAX_ENTITIES_PER_CHUNK: int = Field(
        default=8,
        ge=1,
        le=32,
        description=(
            "Legacy JSON-object cap retained for env compatibility. JSONL "
            "primary extraction ignores this value."
        ),
    )
    EXTRACTION_JSON_OBJECT_MAX_RELATIONS_PER_CHUNK: int = Field(
        default=12,
        ge=0,
        le=32,
        description=(
            "Legacy JSON-object cap retained for env compatibility. JSONL "
            "primary extraction ignores this value."
        ),
    )
    EXTRACTION_JSON_OBJECT_MAX_FACTS_PER_CHUNK: int = Field(
        default=4,
        ge=0,
        le=12,
        description=(
            "Legacy JSON-object cap retained for env compatibility. JSONL "
            "primary extraction ignores this value."
        ),
    )
    EXTRACTION_EVIDENCE_MAX_CHARS: int = Field(
        default=120,
        ge=20,
        le=500,
        description=(
            "Prompted maximum character length for Ghost B evidence phrases. "
            "Parser truncation remains a final safety net."
        ),
    )
    EXTRACTION_FACT_VALUE_MAX_CHARS: int = Field(
        default=160,
        ge=20,
        le=500,
        description=(
            "Prompted maximum character length for Ghost B fact values when "
            "facts are enabled."
        ),
    )
    EXTRACTION_RESCUE_MAX_TOKENS: int = Field(
        default=4096,
        ge=256,
        le=8192,
        description=(
            "Maximum completion tokens for Ghost B rescue mode after the first "
            "foreground extraction contract violation. Rescue must be >= the "
            "normal cap minus a small margin — a smaller rescue budget than "
            "the normal call defeats the point of the rescue: it can't recover "
            "any chunk the normal call couldn't fit."
        ),
    )
    EXTRACTION_MAX_INPUT_TOKENS: int = Field(
        default=700,
        ge=128,
        le=4096,
        description=(
            "Hard token cap for the child text span sent to Ghost B. The chunker "
            "should normally enforce this first; this is the extraction safety net."
        ),
    )
    TIER_CHUNKER_DOC_TIMEOUT_SECONDS: int = Field(
        default=600,
        ge=60,
        le=7200,
        description=(
            "Wall-clock cap on tier_chunker.chunk() per document. Pathological "
            "content (long code/math/table blocks with no sentence boundaries) "
            "can pin the chunker CPU-bound for many minutes; tripping this "
            "timeout marks the doc failed and lets the batch move on instead "
            "of stalling indefinitely."
        ),
    )
    EMBEDDER_SAFE_MAX_TOKENS: int = Field(
        default=960,
        ge=128,
        le=8192,
        description=(
            "Hard cap on tokens of any text sent to the embedder. "
            "sentence-transformers silently truncates inputs that exceed the "
            "model's max_seq_length; for code (no sentence-boundary fallback) "
            "that destroys retrieval fidelity. The code lane packs every "
            "child under this cap. Set conservatively below the model's true "
            "ceiling to absorb tokenizer drift between cl100k (used for "
            "budget math) and the model tokenizer (used at encode time)."
        ),
    )
    TIER_CHUNKER_CODE_LANE_ENABLED: bool = Field(
        default=True,
        description=(
            "Kill switch for the code-aware lane. When False, the early-"
            "intercept gate in parse_document and the fence walker in "
            "_markdown_sections both fall through to legacy prose behavior. "
            "Use to bisect regressions during rollout."
        ),
    )
    RERANKER_BYPASS_CODE: bool = Field(
        default=True,
        description=(
            "Code-aware reranking. Some cross-encoders systematically demote "
            "code-shaped chunks. When True, chunks with chunk_kind=code "
            "(detected via the `language` field) bypass the cross-encoder and "
            "keep their pre-rerank score; prose chunks are reranked normally. "
            "Both pools are min-max normalized before merge so neither side "
            "crowds the other out. Flip False if you swap to a code-aware "
            "reranker and live probes show code scores are reliable."
        ),
    )
    RETRIEVAL_GRAPH_RERANK_ENABLED: bool = Field(
        default=True,
        description=(
            "Sprint #1 — graph-based reranking. When True, the retrieval "
            "pipeline applies a PageRank-shaped degree multiplier to each "
            "candidate chunk's score AFTER Mode A expansion but BEFORE "
            "the rerank_top_n cap. The multiplier is "
            "1 + 0.15 * log1p(min(max_entity_degree, 50)), so a chunk "
            "that mentions a hub concept (high-degree :Entity) gets a "
            "structural boost. Only fires when the request uses the "
            "qdrant_mongo_graph tier AND Neo4j is enabled. Flip False "
            "to A/B test boost-vs-no-boost on the same query."
        ),
    )
    RETRIEVAL_CACHE_GRAPH_METRICS: bool = Field(
        default=True,
        description=(
            "Phase 5a — use the cached analytics.CorpusMetrics (top_pagerank) "
            "alongside live degree count when graph-reranking. Same code "
            "path as RETRIEVAL_GRAPH_RERANK_ENABLED: only fires on "
            "qdrant_mongo_graph tier with Neo4j enabled. The multiplier "
            "becomes 1 + alpha * log1p(MAX(local_degree, pagerank_pseudo_degree)) "
            "so a chunk gets boosted by EITHER local connectivity OR global "
            "structural importance, whichever is stronger. Cold cache "
            "(no metrics row, or no top_pagerank entries) falls back to "
            "the existing degree-only path. Default ON — every failure "
            "mode (no cache row, get_cached_metrics raises, cypher fails, "
            "entity not in top_pagerank) reverts to the pre-Phase-5a "
            "behavior exactly, and the MAX() semantics mean the "
            "multiplier can never go BELOW the degree-only value. "
            "Set to False to A/B test pure-degree vs metrics-aware on "
            "the same query. Requires Phase 4 auto-warm (or a manual "
            "/api/graph/cache/rebuild call) for cache population."
        ),
    )
    RETRIEVAL_CACHE_MODE_A_METRICS: bool = Field(
        default=True,
        description=(
            "Phase 5b — Mode A bonus expansion via cached bridge entries. "
            "When True AND the cache is warm AND the request is graph-tier, "
            "Mode A adds a third pass alongside its existing MENTIONS + CALLS "
            "expansion: for each seed entity that appears in a cache bridge "
            "(fragile_bridges / structural_analogies / terminological_gaps / "
            "transfer_candidates), pull chunks mentioning the OTHER endpoint. "
            "Bonus chunks are capped at max(2, expansion_cap // 4) to keep "
            "the existing mention/calls pool dominant 3:1. Synthetic scores "
            "are derived from the bridge entry's signal strength (path_count "
            "for fragile, topology_sim × neighbor_jaccard for analogies/"
            "terminological, topology_sim for transfers) so they land in the "
            "same 0.0-1.0 range as the existing Mode A scores. Cold cache "
            "(no metrics row, lookup raises, no bridges match seeds) → "
            "bonus pass returns []; behavior identical to pre-Phase-5b."
        ),
    )
    RETRIEVAL_CACHE_DECORATION_METRICS: bool = Field(
        default=True,
        description=(
            "Phase 5b — annotate GraphDecoration rows with cached structural "
            "signals (entity_betweenness, top_pagerank lookup, fragile_bridge "
            "membership). Pure additive metadata on read-only post-retrieval "
            "decoration; no ranking change. Six new optional fields land on "
            "each decoration: seed_entity_id, neighbor_entity_id, "
            "seed_betweenness, neighbor_betweenness, seed_pagerank, "
            "neighbor_pagerank, is_fragile_bridge. The prompt template "
            "in context_manager surfaces these to the LLM textually so "
            "synthesis can weigh which arrows represent structurally "
            "important bridges. Cold cache or any failure → fields are "
            "None / False, base decoration unchanged."
        ),
    )
    LIVE_WEB_SEARCH_ENABLED: bool = Field(
        default=True,
        description=(
            "Server capability for the opt-in chat Web toggle. When the user "
            "enables Web for a turn, chat may append bounded SearXNG results "
            "as additive context. False disables the lane globally."
        ),
    )
    SEARXNG_URL: str = Field(
        default="http://localhost:8080",
        description="Base URL for the SearXNG instance used by opt-in chat web search.",
    )
    SEARXNG_ENGINES: str = Field(
        default="duckduckgo,bing,mojeek,wikipedia",
        description="Comma-separated SearXNG engines requested for opt-in chat web search.",
    )
    SEARXNG_TIMEOUT_SECONDS: float = Field(
        default=6.0,
        ge=1.0,
        le=30.0,
        description="HTTP timeout for the opt-in chat SearXNG request.",
    )
    STRACT_SEARCH_ENABLED: bool = Field(
        default=True,
        description=(
            "Enable Stract's no-key JSON search API as a free independent-index "
            "lane alongside SearXNG for opt-in chat web search."
        ),
    )
    STRACT_SEARCH_URL: str = Field(
        default="https://stract.com/beta/api/search",
        description="Stract JSON search endpoint used by the free live-web search pool.",
    )
    STRACT_SEARCH_TIMEOUT_SECONDS: float = Field(
        default=4.0,
        ge=1.0,
        le=20.0,
        description="HTTP timeout for one Stract search request.",
    )
    LIVE_WEB_SEARCH_MAX_RESULTS: int = Field(
        default=7,
        ge=1,
        le=20,
        description=(
            "Maximum reranked live-web results returned to the model for one "
            "web_search tool call."
        ),
    )
    LIVE_WEB_SEARCH_CANDIDATE_RESULTS: int = Field(
        default=15,
        ge=1,
        le=40,
        description=(
            "Number of raw SearXNG candidates fetched before local reranking. "
            "Keep this above LIVE_WEB_SEARCH_MAX_RESULTS so the reranker can "
            "select the best websites instead of trusting search-engine order."
        ),
    )
    LIVE_WEB_QUERY_EXPANSION_TIMEOUT_SECONDS: float = Field(
        default=4.0,
        ge=0.25,
        le=10.0,
        description=(
            "Best-effort time budget for the Utility model to improve the "
            "opt-in live-web search query."
        ),
    )
    LIVE_WEB_SEARCH_FETCH_FULL_PAGES: bool = Field(
        default=True,
        description=(
            "When true, fetch top live-web result pages and inject cleaned page "
            "text instead of only SearXNG snippets. If OBSCURA_COMMAND is set, "
            "Obscura may be used as an allowlisted fallback after static "
            "extraction fails."
        ),
    )
    LIVE_WEB_FETCH_MAX_PAGES: int = Field(
        default=6,
        ge=1,
        le=20,
        description=(
            "Maximum live-web URLs to full-fetch after snippet reranking. "
            "Search can collect a wider pool, but extraction stays bounded so "
            "web search does not become a turn-latency bottleneck."
        ),
    )
    LIVE_WEB_PAGE_FETCHER: str = Field(
        default="auto",
        description=(
            "Static full-page extraction backend for live web results: native, "
            "trafilatura, or auto. Auto uses Trafilatura when installed and "
            "falls back to native httpx + BeautifulSoup."
        ),
    )
    LIVE_WEB_OBSCURA_DOMAINS: str = Field(
        default=(
            "civitai.com,create.roblox.com,gumroad.com,polymarket.com,"
            "producthunt.com,rolimons.com,tradingview.com"
        ),
        description=(
            "Comma-separated domains where the optional Obscura JS renderer may "
            "run after raw/source and static extraction fail. Empty disables "
            "Obscura fallback even when OBSCURA_COMMAND is set."
        ),
    )
    LIVE_WEB_FETCH_CACHE_TTL_SECONDS: int = Field(
        default=900,
        ge=0,
        le=86400,
        description=(
            "In-process TTL for successful live-web page extraction results. "
            "Set 0 to disable."
        ),
    )
    OBSCURA_COMMAND: str = Field(
        default="",
        description=(
            "Optional Obscura CLI command path. When set with "
            "LIVE_WEB_SEARCH_FETCH_FULL_PAGES=true, Polymath runs "
            "`obscura fetch <url> --dump markdown`; otherwise native HTML "
            "fetching is used."
        ),
    )
    OBSCURA_TIMEOUT_SECONDS: float = Field(
        default=10.0,
        ge=2.0,
        le=60.0,
        description="Wall-clock timeout for one optional Obscura page fetch.",
    )
    OBSCURA_MAX_CHARS: int = Field(
        default=4000,
        ge=500,
        le=20000,
        description="Maximum rendered markdown characters kept per Obscura result.",
    )
    LIVE_WEB_VIDEO_TRANSCRIPT_MIN_CHARS: int = Field(
        default=80,
        ge=0,
        le=2000,
        description=(
            "Minimum useful YouTube transcript evidence length. Shorter "
            "metadata-only results fall back to snippet-only evidence."
        ),
    )
    LIVE_WEB_VIDEO_TRANSCRIPT_MAX_CHARS: int = Field(
        default=12000,
        ge=500,
        le=50000,
        description=(
            "Maximum characters kept from one YouTube metadata/transcript "
            "fetch. Separate from OBSCURA_MAX_CHARS because transcripts are "
            "a distinct evidence type."
        ),
    )
    GRAPHIFY_AUGMENT_CODE_LANE: bool = Field(
        default=False,
        description=(
            "Phase 4.5 opt-in. When True, the ingestion worker invokes "
            "graphify (safishamsi/graphify, MIT) on code corpora after the "
            "Phase 1 chunker runs and writes the cross-file call/inheritance"
            "/import edges plus Leiden community labels into Neo4j alongside "
            "the existing Phase 4 entities. Defaults False so prose corpora "
            "and personal/emotional corpora are unaffected. Flip on per "
            "corpus via IngestionConfig override, not globally — graphify's "
            "LLM passes are inappropriate for private content (it can route "
            "doc/PDF extraction to external APIs unless Ollama is configured)."
        ),
    )
    GRAPHIFY_LLM_PROVIDER: str = Field(
        default="ollama",
        description=(
            "Provider for graphify's Pass 3 (LLM extraction over docs / PDFs "
            "/ images). 'ollama' keeps everything local — the safe default. "
            "Other values (claude, gemini, openai) route to external APIs "
            "and should only be used for non-sensitive code corpora. The "
            "augmenter reads this at call time; changing it requires no "
            "restart."
        ),
    )
    TIER_CHUNKER_CODE_SUPPORTED_LANGS: list[str] = Field(
        default=[
            # mainstream code
            "python",
            "javascript",
            "typescript",
            "tsx",
            "lua",
            "luau",
            "go",
            "rust",
            "java",
            "c",
            "cpp",
            "cuda",
            "ruby",
            "bash",
            "sql",
            "csharp",
            "kotlin",
            "swift",
            "php",
            "scala",
            "elixir",
            "haskell",
            "r",
            "dart",
            "nix",
            "objc",
            # shaders
            "glsl",
            "hlsl",
            # web frameworks
            "vue",
            "svelte",
            # markup + styling (parsed as code so structure survives splits)
            "html",
            "css",
            "xml",
            # data / config (parsed for atomic packing; symbols usually empty)
            "json",
            "yaml",
            "toml",
            "ini",
            # IaC / build / API
            "hcl",
            "dockerfile",
            "make",
            "cmake",
            "proto",
            "graphql",
        ],
        description=(
            "Languages where the AST packer is invoked. Outside this list, "
            "code chunks still get chunk_kind=CODE and are packed under the "
            "embedder cap via blank-line splitting, but no symbol metadata "
            "is extracted."
        ),
    )
    EXTRACTION_MAX_TOTAL_LINES: int = Field(
        default=55,
        ge=1,
        le=128,
        description=(
            "Maximum JSONL extraction item lines Ghost B may emit before the "
            "finished sentinel. Set 15 lines above the per-type theoretical "
            "max (14 entities + 20 relations + 5 facts + 1 sentinel = 40) so "
            "dense chunks never bump the parser line cap. Enforced in the "
            "parser; the prompt no longer carries an explicit line-cap rule."
        ),
    )
    EXTRACTION_RESCUE_MAX_TOTAL_LINES: int = Field(
        default=30,
        ge=1,
        le=64,
        description=(
            "Maximum JSONL item lines accepted from Ghost B rescue mode. Sits "
            "above the rescue per-type theoretical max (8 entities + 8 relations "
            "+ 5 facts + 1 sentinel = 22) for the same reason as the normal cap."
        ),
    )
    EXTRACTION_JSONL_MAX_CALLS: int = Field(
        default=2,
        ge=1,
        le=32,
        description=(
            "Legacy upper bound for sequential Ghost B JSONL calls. Foreground "
            "ingest is hard-clamped to two calls: one primary plus one repair."
        ),
    )
    EXTRACTION_FOREGROUND_MAX_CALLS: int = Field(
        default=2,
        ge=1,
        le=3,
        description=(
            "Hard foreground ingest limit for extraction calls per child chunk. "
            "Values above 2 are clamped: call 1 is primary JSONL, call 2 is "
            "the single repair/resume call."
        ),
    )
    EXTRACTION_GLOBAL_MAX_CONCURRENT: int = Field(
        default=180,
        ge=1,
        le=4096,
        description=(
            "Safety ceiling for simultaneous Ghost B provider requests. The "
            "effective process cap is also bounded by the configured extraction "
            "model lanes so per-model max_concurrent is not multiplied by docs."
        ),
    )
    EXTRACTION_MAX_ACTIVE_DOCS: int = Field(
        default=1,
        ge=1,
        le=16,
        description=(
            "Maximum documents allowed to run foreground Ghost B extraction at "
            "once. Default 1 keeps extraction concurrency targeted at one file's "
            "child queue instead of multiplying by model-phase document fanout."
        ),
    )
    EXTRACTION_FAILURE_PAUSE_PERCENT: float = Field(
        default=25.0,
        ge=0.0,
        le=100.0,
        description=(
            "Pause a document's foreground Ghost B queue when the failed chunk "
            "percentage reaches this value after the minimum sample size. Set "
            "100 to effectively disable early pause."
        ),
    )
    EXTRACTION_FAILURE_PAUSE_MIN_CHUNKS: int = Field(
        default=20,
        ge=1,
        le=10000,
        description=(
            "Minimum processed Ghost B chunks before the failure-percentage "
            "pause circuit can trip."
        ),
    )
    EXTRACTION_JSONL_DEBUG_RAW: bool = Field(
        default=False,
        description=(
            "When true, log raw Ghost B JSONL response lines at DEBUG level "
            "for ingestion troubleshooting. Disabled by default because "
            "lines can contain source-derived content."
        ),
    )
    EXTRACTION_ERROR_AUDIT_ENABLED: bool = Field(
        default=True,
        description=(
            "Persist sampled Ghost B extraction failure evidence to Mongo "
            "collection ghost_b_error_events."
        ),
    )
    EXTRACTION_ERROR_AUDIT_MAX_FAILED_ATTEMPTS_PER_DOC: int = Field(
        default=200,
        ge=0,
        le=2000,
        description=(
            "Maximum failed Ghost B attempt audit rows stored per document. "
            "Raised from 25 to 200 so a catastrophically-failing doc (e.g. "
            "the pre-thinking-disable Design Patterns case with 466 failures) "
            "is visible in the audit collection rather than masked by the "
            "cap. The authoritative count remains on "
            "documents.ghost_b_metrics.failed_chunks; this cap controls "
            "forensic detail only. 200 rows × 500 docs ≈ 100K rows, ~100 MB "
            "in Mongo — trivial."
        ),
    )
    EXTRACTION_ERROR_AUDIT_MAX_SUCCESS_ATTEMPTS_PER_DOC: int = Field(
        default=2,
        ge=0,
        le=100,
        description="Maximum successful Ghost B attempt audit rows stored per document.",
    )
    EXTRACTION_ERROR_AUDIT_RAW_FIRST_CHARS: int = Field(
        default=200,
        ge=0,
        le=2000,
        description="Characters kept from the beginning of failed Ghost B raw output.",
    )
    EXTRACTION_ERROR_AUDIT_RAW_LAST_CHARS: int = Field(
        default=400,
        ge=0,
        le=4000,
        description="Characters kept from the end of failed Ghost B raw output.",
    )
    EXTRACTION_MAX_ENTITIES_PER_CHUNK: int = Field(
        default=18,
        ge=1,
        le=64,
        description=(
            "Maximum entities Ghost B should return for a single child chunk. "
            "Raised from 14 to 18 after observing dense Design Patterns chunks "
            "(_0000, _0031, _0043) saturate the cap with post_entities==14, "
            "i.e. the model wanted to emit more. Recompute output budget: "
            "18 entities + 20 relations + 5 facts + 1 sentinel = 44 lines "
            "max, still under EXTRACTION_MAX_TOTAL_LINES=55."
        ),
    )
    EXTRACTION_MAX_RELATIONS_PER_CHUNK: int = Field(
        default=20,
        ge=0,
        le=64,
        description=(
            "Maximum relations Ghost B should return for a single child chunk. "
            "Bumped from 14 → 20 when canonicalization predicates "
            "(synonym_of, instance_of) joined the universal schema — those "
            "self-edges shouldn't crowd out real operational relations. If "
            "the cap ever squeezes out content, the next refactor should tier "
            "canonicalization predicates out of this count."
        ),
    )
    EXTRACTION_RESCUE_MAX_ENTITIES_PER_CHUNK: int = Field(
        default=8,
        ge=1,
        le=32,
        description="Maximum entities Ghost B rescue mode should return for a child chunk.",
    )
    EXTRACTION_RESCUE_MAX_RELATIONS_PER_CHUNK: int = Field(
        default=8,
        ge=0,
        le=32,
        description="Maximum relations Ghost B rescue mode should return for a child chunk.",
    )
    EXTRACTION_ENABLE_FACTS: bool = Field(
        default=True,
        description=(
            "When true, Ghost B also extracts capped structured facts/properties "
            "alongside entities and relations. Stored output remains normalized "
            "as ExtractionResult entities/relations/facts."
        ),
    )
    EXTRACTION_MAX_FACTS_PER_CHUNK: int = Field(
        default=5,
        ge=0,
        le=20,
        description=(
            "Maximum structured facts Ghost B should return for one child chunk "
            "when EXTRACTION_ENABLE_FACTS is true."
        ),
    )
    ENTITY_CONFIDENCE_THRESHOLD: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Min confidence to keep an extracted entity or relation (GHOST B)",
    )

    # === AGENTIC MODE ===
    AGENTIC_MODE_ENABLED: bool = Field(
        default=False,
        description="Global default for agentic mode. When True, queries route through AGENTIC_MODEL and tools can execute. User can override per-request via ModelOverrides.",
    )
    AGENTIC_MODEL: str = Field(
        default="",
        description=(
            "Phase 24: empty by default. Use Settings → Models → Tool-Capable "
            "Fallback to pick an entry. When unset and the active chat model "
            "can't tool-call, the resolver raises a clear error rather than "
            "silently degrading to a slow Ollama default."
        ),
    )

    # === HYDE (Phase 17) — dedicated cheap model for hypothetical answer generation ===
    HYDE_MODEL: str = Field(
        default="",
        description=(
            "Phase 24: empty by default. Use Settings → Models → HyDE to pick "
            "an entry. When unset, HyDE reuses the active chat model rather "
            "than silently degrading to a hardcoded default."
        ),
    )
    HYDE_TIMEOUT_SECONDS: float = Field(
        default=8.0,
        ge=1.0,
        le=60.0,
        description=(
            "Hard wall for the HyDE helper call. Keep bounded so retrieval can "
            "fall back to the raw query, but allow cloud models like Mistral "
            "enough room to succeed."
        ),
    )
    HYDE_MAX_TOKENS: int = Field(
        default=192,
        ge=32,
        le=1024,
        description="Maximum tokens for the short hypothetical answer used by HyDE.",
    )

    # === REASONING CASCADE (Phase 24) — analyst that digests retrieved chunks ===
    REASONING_MODEL: str = Field(
        default="",
        description=(
            "Model used for the reasoning cascade (opt-in per-request). "
            "Should be a strong reasoning/analysis model (DeepSeek R1, o1, "
            "claude-sonnet, etc). Empty falls back to DEFAULT_COMPLETION_MODEL. "
            "Cost ~20× of a Balanced query — use sparingly via per-request flag."
        ),
    )

    # === AUTHENTICATION ===
    # SECURITY (Phase 17 W1.1): both AUTH_SECRET_KEY and DEFAULT_ADMIN_PASSWORD
    # are now REQUIRED env vars with no default. Startup validators reject the
    # legacy sentinel values so that an insecure .env can never deploy silently.
    AUTH_SECRET_KEY: str = Field(
        default=...,
        description="Secret key for JWT signing. MUST be set in .env; startup rejects the legacy 'polymath-dev-secret-key-change-in-production' sentinel.",
    )
    AUTH_ALGORITHM: str = Field(default="HS256", description="JWT signing algorithm")
    AUTH_TOKEN_EXPIRE_DAYS: int = Field(
        default=7, description="JWT token expiration in days"
    )
    DEFAULT_ADMIN_USERNAME: str = Field(
        default="admin", description="Default admin username for zero-user bootstrap"
    )
    DEFAULT_ADMIN_PASSWORD: str = Field(
        default=...,
        description="Initial admin password for zero-user bootstrap. MUST be set in .env; startup rejects empty and the legacy 'changeme' sentinel.",
    )

    # === APPLICATION ===
    APP_ENV: str = Field(default="development", description="Application environment")
    LOG_LEVEL: str = Field(default="info", description="Logging level")

    # === MULTI-CORPUS EMERGENCY KILL SWITCH ===
    # Multi-corpus rollout PR 1 — backward compat is provided by Pydantic
    # input normalization (no runtime feature flag). This env var is a
    # deploy-time emergency lever: when "true"/"1"/"yes", utils.corpus_ids
    # rejects any request resolving to more than one corpus_id with a
    # MultiCorpusDisabledError → 400. Leave unset/false in normal operation.
    DISABLE_MULTI_CORPUS: bool = Field(
        default=False,
        description=(
            "Emergency kill switch. When true, any request with more than one "
            "corpus_id is rejected at the input normalization layer. Used only "
            "as a rollback lever if multi-corpus exhibits production issues."
        ),
    )

    @field_validator("LITELLM_MASTER_KEY")
    @classmethod
    def validate_litellm_key(cls, v):
        """Validate that LITELLM_MASTER_KEY is not empty."""
        if not v or not v.strip():
            raise ValueError("LITELLM_MASTER_KEY is required and cannot be empty")
        return v

    @field_validator("AUTH_SECRET_KEY")
    @classmethod
    def validate_auth_secret_key(cls, v):
        """
        Phase 17 W1.1 — reject empty or the legacy dev sentinel.

        JWT tokens signed with the sentinel are forgeable by anyone who has
        seen the source code. Hard fail at startup if it's still in place.
        """
        if not v or not v.strip():
            raise ValueError("AUTH_SECRET_KEY is required and cannot be empty")
        if v.strip() == "polymath-dev-secret-key-change-in-production":
            raise ValueError(
                "AUTH_SECRET_KEY is still the legacy dev sentinel. Generate a "
                'secure random key (e.g. `python -c "import secrets; print(secrets.token_urlsafe(64))"`) '
                "and set it in .env before starting."
            )
        return v

    @field_validator("DEFAULT_ADMIN_PASSWORD")
    @classmethod
    def validate_default_admin_password(cls, v):
        """
        Phase 17 W1.1 — reject empty or the legacy 'changeme' sentinel.
        The bootstrap admin account would otherwise be publicly guessable.
        """
        if not v or not v.strip():
            raise ValueError("DEFAULT_ADMIN_PASSWORD is required and cannot be empty")
        if v.strip().lower() == "changeme":
            raise ValueError(
                "DEFAULT_ADMIN_PASSWORD is still the legacy 'changeme' sentinel. "
                "Set a strong password in .env before starting."
            )
        return v


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached application settings.
    Usage: settings = get_settings()
    Then: settings.MONGODB_URI, settings.QDRANT_URL, etc.
    """
    return Settings()
