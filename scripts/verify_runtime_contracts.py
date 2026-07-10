#!/usr/bin/env python3
"""Verify Polymath's deterministic runtime wiring.

This is a static, dependency-free contract check for a fresh checkout. It does
not prove the current data is healthy; it proves the repo still contains the
startup hooks, worker triggers, cleanup guards, and streaming UI/backend paths
that make new installs and future ingests converge to the intended end state.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Requirement:
    id: str
    description: str
    path: str
    needles: tuple[str, ...]
    regex: bool = False


@dataclass(frozen=True)
class Result:
    id: str
    status: str
    path: str
    description: str
    missing: tuple[str, ...] = ()


REQUIREMENTS: tuple[Requirement, ...] = (
    Requirement(
        id="backend-lifespan-core",
        description="FastAPI startup connects core services, creates indexes, bootstraps auth, and runs idempotent migrations.",
        path="backend/main.py",
        needles=(
            "await conversation_service.connect()",
            "await create_all_indexes(conversation_service._db)",
            "await auth_service.bootstrap()",
            "await ingestion_service.connect(conversation_service._db)",
            "await ingestion_service.migrate_universal_schema(",
            "await ingestion_service.migrate_bare_model_names()",
            "settings_service.attach(conversation_service._db)",
            "model_pool_service.attach(conversation_service._db)",
            "query_prefs_service.attach(conversation_service._db)",
            "recover_local_batch_runners",
        ),
    ),
    Requirement(
        id="ingestion-service-boot",
        description="Ingestion service startup runs the retrieval-readiness repair sweep before new jobs resume.",
        path="backend/services/ingestion_service.py",
        needles=(
            "repair_retrieval_readiness_for_all_corpora",
            "Retrieval readiness repair",
            "neo4j_schema_ready",
            "from services.retrieval_readiness import",
            "from services.ingestion.worker import run_ingest_job",
        ),
    ),
    Requirement(
        id="retrieval-readiness-contract",
        description="Retrieval readiness idempotently prepares Qdrant route collections and Neo4j retrieval indexes for startup, corpus creation, and worker setup.",
        path="backend/services/retrieval_readiness.py",
        needles=(
            "async def ensure_corpus_retrieval_ready",
            "async def repair_retrieval_readiness_for_all_corpora",
            "ensure_collections_for_corpus",
            "ensure_neo4j_retrieval_schema",
            "wait_for_retrieval_indexes",
            "qdrant_route_collections",
            "neo4j_required",
        ),
    ),
    Requirement(
        id="qdrant-new-corpus-hybrid-layout",
        description="New corpora get named dense+sparse Qdrant collections and payload indexes so Fast/Hybrid/Graph retrieval can use semantic plus exact-token lanes.",
        path="backend/services/storage/qdrant_writer.py",
        needles=(
            'vectors_config={"dense": VectorParams',
            'sparse_vectors_config={"sparse": SparseVectorParams',
            "Modifier.IDF",
            "_collection_layout",
            "upsert_children",
            "upsert_summaries",
        ),
    ),
    Requirement(
        id="neo4j-retrieval-schema",
        description="Neo4j schema creates graph-retrieval constraints, RELATES_TO indexes, and Entity/Fact full-text indexes, then waits for them online.",
        path="backend/services/graph/schema.py",
        needles=(
            "entity_name_ft",
            "fact_text_ft",
            "wait_for_retrieval_indexes",
            "RELATES_TO",
            "r.predicate",
            "r.confidence",
        ),
    ),
    Requirement(
        id="ingest-worker-retrieval-readiness-gate",
        description="Worker enforces retrieval storage readiness before chunk/vector/graph writes and marks setup failures durably.",
        path="backend/services/ingestion/worker.py",
        needles=(
            "ensure_corpus_retrieval_ready",
            "phase=retrieval_setup ok=true",
            "retrieval setup failed",
            'stage="setup_failed"',
        ),
    ),
    Requirement(
        id="post-ingest-retrieval-verification",
        description="Post-ingest verification checks Mongo, Qdrant child/summary/text contract, and Neo4j retrieval indexes before a document can finish done.",
        path="backend/services/ingestion/verify.py",
        needles=(
            "async def verify_ingest",
            "qdrant.summary_count",
            "_verify_qdrant_text_contract",
            "wait_for_retrieval_indexes",
            "neo4j.retrieval_indexes",
            "probe.scroll",
        ),
    ),
    Requirement(
        id="transcript-ingestion-normalizer",
        description="Timestamped transcript exports route through a deterministic parser that moves video header fields into metadata and keeps speech/time ranges as retrieval text.",
        path="backend/services/ingestion/docling_adapter.py",
        needles=(
            "_parse_transcript_text_document",
            "_TRANSCRIPT_SEGMENT_RE",
            "youtube_transcript",
            "Transcript range",
            "time_start",
            "time_end",
        ),
    ),
    Requirement(
        id="transcript-chunk-metadata-preserved",
        description="The chunker preserves transcript_block metadata into parent/child chunks instead of coalescing timestamp evidence away.",
        path="backend/services/ingestion/tier_chunker.py",
        needles=(
            "transcript_block",
            "timestamped BODY blocks",
            "and not cur_meta",
            "and not next_meta",
        ),
    ),
    Requirement(
        id="transcript-ingestion-regression-test",
        description="A YouTube Transcript API-style sample proves metadata headers are not indexed as the first speech chunk.",
        path="backend/tests/test_tier_chunker.py",
        needles=(
            "test_youtube_transcript_metadata_is_not_indexed_as_first_speech_chunk",
            "How to build a Modern EDITABLE Grid",
            "YouTube Transcript API",
            "time_start",
            "https://youtu.be",
        ),
    ),
    Requirement(
        id="source-identity-youtube-guard",
        description="Ingestion has deterministic source identity for YouTube/transcript URLs, canonical URLs, content hashes, and filename fallback.",
        path="backend/services/ingestion/source_identity.py",
        needles=(
            "extract_youtube_video_id",
            "extract_declared_source_url",
            "canonicalize_source_url",
            "build_deterministic_filename",
            "extract_source_title",
            "build_source_identity",
            "youtube_video",
            "source_identity.v1",
        ),
    ),
    Requirement(
        id="source-identity-indexes",
        description="Startup creates Mongo indexes for source/video lookup so duplicate tracking is fast on fresh installs.",
        path="backend/services/ingestion_service.py",
        needles=(
            "_ensure_source_identity_indexes",
            "documents_source_identity_key_corpus_idx",
            "documents_youtube_video_corpus_idx",
            "build_deterministic_filename",
            "build_source_identity(",
            "deterministic_filename",
            "source_identity=source_identity",
        ),
    ),
    Requirement(
        id="source-identity-worker-persistence",
        description="The ingest worker persists source identity at parse, chunk, and final metadata checkpoints.",
        path="backend/services/ingestion/worker.py",
        needles=(
            "source_identity_doc_fields",
            "source_url: str | None = None",
            "source_identity: dict | None = None",
            "source_url=source_url",
            "source_identity=source_identity",
        ),
    ),
    Requirement(
        id="mcp-source-duplicate-guard",
        description="MCP exposes source lookup and skips already-tracked URL/transcript uploads unless duplicate_policy='allow'.",
        path="backend/polymath_mcp/tools.py",
        needles=(
            "polymath_check_source",
            "_find_existing_source_matches",
            "build_deterministic_filename",
            "duplicate_policy",
            "filename_policy",
            "already_exists",
            "existing_source_matches",
        ),
    ),
    Requirement(
        id="html-ingestion-prose-default",
        description="HTML document exports default to local prose extraction rather than source-code chunking, so navigation/script/style markup does not poison RAG chunks.",
        path="backend/services/ingestion/docling_adapter.py",
        needles=(
            "HTML uploads default to the local_html prose extractor",
            "_looks_like_html",
            "source_format=\"local_html\"",
            "from services.ingestion.format_router import route",
        ),
    ),
    Requirement(
        id="html-ingestion-regression-test",
        description="A local HTML sample proves the parser keeps readable body text while stripping script/header/footer noise.",
        path="backend/tests/test_tier_chunker.py",
        needles=(
            "test_html_upload_defaults_to_prose_extraction_not_code_lane",
            "parser_strategy(\"power-apps-grid.html\", \"text/html\") == \"local_html\"",
            "console.log",
            "site navigation",
        ),
    ),
    Requirement(
        id="spreadsheet-ingestion-local-table",
        description="CSV/TSV and Excel uploads route through deterministic local table parsers instead of requiring the Docling sidecar.",
        path="backend/services/ingestion/docling_adapter.py",
        needles=(
            "_parse_delimited_table_document",
            "_parse_xlsx_table_document",
            "_TABLE_PARSE_MAX_ROWS_PER_SHEET",
            "local_csv",
            "local_xlsx",
            "openpyxl",
            "element_type=\"table\"",
        ),
    ),
    Requirement(
        id="spreadsheet-ingestion-regression-tests",
        description="Adapter tests prove CSV and XLSX uploads parse as local table sections without calling Docling.",
        path="backend/tests/test_docling_adapter.py",
        needles=(
            "test_csv_upload_parses_as_local_table_without_docling",
            "test_xlsx_upload_parses_as_local_table_without_docling",
            "products.csv",
            "inventory.xlsx",
            "Docling sidecar should not be called for CSV",
            "Docling sidecar should not be called for XLSX",
        ),
    ),
    Requirement(
        id="summary-indexing-guard",
        description="Worker tracks summary indexing separately from child-vector writes and verifies Qdrant summary points before resume-skip.",
        path="backend/services/ingestion/worker.py",
        needles=(
            "summaries_indexed",
            "_qdrant_has_summary_points",
            "upsert_summaries",
            'write_updates["summaries_indexed"]',
            "summary_complete",
        ),
    ),
    Requirement(
        id="graph-cache-post-ingest-trigger",
        description="Successful Mongo + Qdrant + Neo4j ingest schedules the debounced graph analytics/cache warm worker.",
        path="backend/services/ingestion/worker.py",
        needles=(
            "summary_complete_for_cleanup = (not summary_gate_required) or ws.summaries_indexed",
            "if ws.qdrant_written and ws.neo4j_written and summary_complete_for_cleanup:",
            "from services.graph.orchestrator import schedule_graph_discovery_cache_warm",
            "schedule_graph_discovery_cache_warm(",
        ),
    ),
    Requirement(
        id="graph-cache-worker",
        description="Graph cache warmup is debounced, active-ingest-aware, and best-effort so it cannot break ingestion.",
        path="backend/services/graph/cache_warmup.py",
        needles=(
            "_PENDING_WARMUP_TASKS",
            "should_defer_warmup_for_active_ingest",
            "schedule_metrics_warmup_after_ingest",
            "from services.graph.analytics import emerge_domains",
            "asyncio.wait_for(",
            "emerge_domains(qdrant, neo4j_driver, db, corpus_id)",
        ),
    ),
    Requirement(
        id="gap-profile-router",
        description="Graph Gap mode has a deterministic query-derived profile for prediction/business/stocks/process/market/structural analysis frames.",
        path="backend/services/gap_profile.py",
        needles=(
            "def build_gap_profile",
            "primary_domain",
            "prediction",
            "business",
            "stocks",
            "process",
            "market",
            "structural",
            "Do not compute quantitative metrics",
        ),
    ),
    Requirement(
        id="gap-profile-synthesis-wiring",
        description="Graph synthesis attaches the GapProfile to trace/LLM context and renders it into Gap mode prompts without making unsupported calculations.",
        path="backend/services/graph/orchestrator.py",
        needles=(
            "build_gap_profile(query)",
            "\"gap_profile\"",
            "Gap analysis profile:",
            "Metrics to look for, not invent",
            "Calculation policy:",
            "QUERY-DETERMINED ANALYSIS FRAME",
        ),
    ),
    Requirement(
        id="gap-profile-regression-tests",
        description="Regression tests prove deterministic routing across prediction, business, stocks, process, market, structural, and mixed-domain queries.",
        path="backend/tests/graph/test_gap_profile.py",
        needles=(
            "test_gap_profile_routes_prediction_query",
            "test_gap_profile_routes_business_query",
            "test_gap_profile_routes_stock_query_without_prediction_advice",
            "test_gap_profile_routes_process_query",
            "test_gap_profile_routes_market_query",
            "test_gap_profile_defaults_to_structural_for_corpus_connection_query",
            "test_gap_profile_keeps_mixed_prediction_business_lenses",
        ),
    ),
    Requirement(
        id="brain-view-new-ingest-visual-payload",
        description="Brain View emits document anchors plus typed top entity records so newly ingested documents automatically enter the corpus visual grammar.",
        path="backend/services/graph/queries.py",
        needles=(
            "top_entity_records",
            "primary_entity_type",
            "observed_entity_types",
            "definitional_phrase",
            "dominant_entity_type",
            "top_entities",
        ),
    ),
    Requirement(
        id="frontend-brain-view-genome-contract",
        description="Corpus overview computes deterministic book/category genomes from top_entity_records and colors property nodes without special per-device state.",
        path="frontend/src/components/graph/GraphViewer.tsx",
        needles=(
            "graphCategoryGenome",
            "top_entity_records",
            "graphDocumentNodeColor()",
            "graphGenomePropertyColor(",
            "visual_category_genome",
            "visual_dominant_category",
        ),
    ),
    Requirement(
        id="frontend-graph-color-genome-contract",
        description="Graph color helpers expose deterministic ingestion-schema categories and keep Product/Person away from document-amber node identity.",
        path="frontend/src/lib/graph-colors.ts",
        needles=(
            "export type GraphGeneticCategory",
            "GRAPH_GENETIC_CATEGORIES",
            "Product: 174",
            "Person: 326",
            "graphCategoryGenome",
            "graphGenomePropertyColor",
            "graphDocumentNodeColor",
        ),
    ),
    Requirement(
        id="frontend-graph-adapter-visual-metadata",
        description="Graph adapter preserves visual color, glow, and genome metadata from query/brain-view payloads into Sigma nodes.",
        path="frontend/src/lib/polymath-graph-adapter.ts",
        needles=(
            "visual_color",
            "visual_glow",
            "visual_glow_strength",
            "visual_category_genome",
            "visual_dominant_category",
            "raw.visual_color",
        ),
    ),
    Requirement(
        id="frontend-book-glow-shader-contract",
        description="Book/document nodes render with a bounded deterministic glow strength instead of relying on device-specific canvas styling.",
        path="frontend/src/lib/sigma-programs/BookGlowProgram.ts",
        needles=(
            "a_glowStrength",
            "visual_glow_strength",
            "effectiveGlow",
            "customGlow",
        ),
    ),
    Requirement(
        id="entity-cleaning-shared",
        description="One deterministic entity-junk rule set is shared by ingestion, query filtering, and historical cleanup.",
        path="backend/services/graph/entity_cleaning.py",
        needles=(
            "JUNK_ENTITY_EXACT_LOWER",
            "JUNK_ENTITY_NAME_RE",
            "def normalize_entity_surface",
            "def is_junk_entity_name",
            "def is_junk_extracted_entity",
        ),
    ),
    Requirement(
        id="entity-cleaning-write-path",
        description="Neo4j writer rejects junk entities at write time so new ingests do not recreate graph debris.",
        path="backend/services/graph/neo4j_writer.py",
        needles=(
            "from services.graph.entity_cleaning import is_junk_extracted_entity",
            "if is_junk_extracted_entity(entity.canonical_name, entity.surface_form):",
        ),
    ),
    Requirement(
        id="entity-cleaning-query-path",
        description="Graph query still filters historical junk as a read-path safety net.",
        path="backend/services/graph/graph_query.py",
        needles=(
            "from services.graph.entity_cleaning import",
            "JUNK_ENTITY_EXACT_LOWER",
            "JUNK_ENTITY_NAME_PATTERN",
            "is_junk_entity_name",
            "def _is_junk_entity_row",
        ),
    ),
    Requirement(
        id="entity-cleanup-cli",
        description="Historical junk entities have a deterministic dry-run/apply cleanup command.",
        path="backend/services/graph/junk_cleanup.py",
        needles=(
            "description=\"Clean historical junk Entity nodes from Neo4j.\"",
            "parser.add_argument(\"--apply\"",
            "DETACH DELETE node",
            "JUNK_ENTITY_NAME_PATTERN",
        ),
    ),
    Requirement(
        id="summary-heal-cli",
        description="Cross-corpus summary health can be scanned and repaired without model calls.",
        path="backend/services/ingestion/summary_backfill.py",
        needles=(
            "async def heal_all",
            "ap.add_argument(\"--heal-all\"",
            "ap.add_argument(\"--apply-heal\"",
            "auto-index orphaned summaries",
        ),
    ),
    Requirement(
        id="retrieval-query-grounding",
        description="Hybrid/graph retrieval deterministically recalls and ranks evidence that covers the user's core query concepts.",
        path="backend/services/retriever/__init__.py",
        needles=(
            "apply_query_grounding",
            "ranked_query_grounded",
            "hydrate_rerank_texts",
        ),
    ),
    Requirement(
        id="retrieval-tier-diagnostics-contract",
        description="Retriever returns observable tier contracts, lane counts, timings, and final source-tier mix.",
        path="backend/services/retriever/__init__.py",
        needles=(
            "_retrieval_store_contract",
            "store_contract",
            "final_source_tiers",
            "diagnostics=_diagnostics",
        ),
    ),
    Requirement(
        id="three-tier-retrieval-e2e-runner",
        description="Fresh environments include a live validator for the three UI routes, retrieval budgets, source timing marks, and Graph Advantage checks.",
        path="scripts/retrieval_three_tier_eval.py",
        needles=(
            "ROUTES = _validation.ROUTES",
            "ROUTE_LATENCY_BUDGETS = _validation.ROUTE_LATENCY_BUDGETS",
            "evaluate_route_result = _validation.evaluate_route_result",
            "retrieval_done_sources",
            "stop_after_sources",
        ),
    ),
    Requirement(
        id="three-tier-retrieval-route-contracts",
        description="Three-route validation preserves UI route names and route-specific retrieval budgets for Fast, Hybrid, and Graph Augmentation.",
        path="backend/services/retriever/three_tier_eval.py",
        needles=(
            '"ui_name": "Fast Search"',
            '"ui_name": "Hybrid Search"',
            '"ui_name": "Graph Augmentation"',
            "ROUTE_LATENCY_BUDGETS",
            "evaluate_route_result",
            "graph_advantage",
        ),
    ),
    Requirement(
        id="lexical-concept-coverage",
        description="Lexical recall adds bounded per-concept coverage so one common term cannot crowd out the rest of a multi-concept query.",
        path="backend/services/retriever/lexical.py",
        needles=(
            "_concept_coverage_recall",
            "concept_groups(query",
            '"retriever": "lexical_coverage"',
        ),
    ),
    Requirement(
        id="reranker-input-cap",
        description="Reranker requests cap document text below the llama.cpp physical-batch failure point.",
        path="backend/services/reranker.py",
        needles=(
            "RERANKER_MAX_DOC_CHARS",
            '"1000"',
            "when a pair overflows it",
            "query_guided_excerpt",
        ),
    ),
    Requirement(
        id="reranker-bypass-score-scale",
        description="Code-bypass reranker scores are clamped before merging with bounded prose rerank scores.",
        path="backend/services/reranker.py",
        needles=(
            "_clamp_bounded_scores_inplace",
            "_score_scale_is_bounded",
            "absolute trump card",
        ),
    ),
    Requirement(
        id="retrieval-mixed-score-tail-guard",
        description="Bounded rerank tail trimming refuses mixed-scale pools and never narrows hydrated tiers before final selection.",
        path="backend/services/retriever/__init__.py",
        needles=(
            "mixed-scale pools",
            "Hydrated tiers are different",
            "richer tiers narrower than",
            "RetrievalTier.qdrant_mongo_graph",
            "float(chunk.score or 0.0) > 1.0001",
        ),
    ),
    Requirement(
        id="graph-rerank-window-preserves-semantic-core",
        description="Graph Augmented keeps a wider post-expansion rerank window so graph neighbors cannot crowd out vector/lexical evidence before hydration.",
        path="backend/services/retriever/__init__.py",
        needles=(
            "GRAPH_PREFILTER_POOL",
            "graph_prefilter_pool",
            "GRAPH_MLX_RERANK_POOL",
            "full RAG",
            "rerank_top_n_graph_cap",
            "before the cross-encoder sees hydrated text",
        ),
    ),
    Requirement(
        id="graph-expansion-timeout",
        description="Graph Augmentation bounds Neo4j Mode A expansion and degrades to hybrid seeds on timeout.",
        path="backend/services/retriever/__init__.py",
        needles=(
            "GRAPH_EXPANSION_TIMEOUT_SECONDS",
            "asyncio.wait_for",
            "graph_expansion_timed_out",
            "continuing with hybrid seeds",
        ),
    ),
    Requirement(
        id="reranker-diagnostics",
        description="Retriever diagnostics reveal whether reranking used the sidecar, bypassed code, hit a circuit breaker, or score-sorted fallback.",
        path="backend/services/reranker.py",
        needles=(
            "def diagnostics",
            "_record_status",
            "fallback_score_sort",
            "used_with_code_bypass",
            "circuit_open",
        ),
    ),
    Requirement(
        id="parent-merge-preserves-exact-child",
        description="Parent-level candidate merge keeps exact child identity over summaries while preserving the strongest parent score.",
        path="backend/services/retriever/merge.py",
        needles=(
            "_representative_priority",
            "merged_parent_representatives",
            "best_score",
            "most exact evidence text",
        ),
    ),
    Requirement(
        id="graph-discover-web-response-contract",
        description="Graph discover normal responses echo corpus_ids and expose a separate top-level web_evidence lane.",
        path="backend/routers/graph.py",
        needles=(
            "_discover_result_corpus_ids",
            "_discover_result_web_evidence",
            "corpus_ids=_discover_result_corpus_ids",
            "web_evidence=_discover_result_web_evidence",
        ),
    ),
    Requirement(
        id="chat-frontmatter-evidence-filter",
        description="Chat evidence filtering rejects front matter, table-of-contents, reviewer, and Discord chunks before prompt assembly.",
        path="backend/services/chat_orchestrator.py",
        needles=(
            "about the reviewers?",
            "join our (?:book'?s |community'?s )?",
            "table of contents",
            "title page",
        ),
    ),
    Requirement(
        id="native-ollama-streaming",
        description="Ollama/Ollama Cloud chat routes bypass LiteLLM so content and native thinking chunks stream separately.",
        path="backend/services/llm.py",
        needles=(
            "def _is_ollama_chat_route",
            "async def _stream_ollama_chat_native",
            "response.aiter_lines()",
            'yield {"thinking": normalized["thinking"]}',
            'yield {"content": normalized["content"]}',
        ),
    ),
    Requirement(
        id="chat-sse-forwarding",
        description="Chat orchestrator forwards model content as token SSE events and reasoning as thinking SSE events.",
        path="backend/services/chat_orchestrator.py",
        needles=(
            'type="thinking"',
            'thinking=chunk["thinking"]',
            'type="token"',
            'content=chunk["content"]',
        ),
    ),
    Requirement(
        id="chat-rag-grounding-contract",
        description="Chat prompts require direct retrieved evidence to drive synthesis before model background knowledge.",
        path="backend/services/chat_orchestrator.py",
        needles=(
            "primary answer substrate",
            "not a generic pretrained definition",
            "instead of substituting your pretrained background",
            "do not introduce named libraries",
            "explicitly asked for outside knowledge",
        ),
    ),
    Requirement(
        id="chat-agentic-markdown-render-contract",
        description="Chat prompts and frontend renderer support Agent-Zero-style Markdown answers with tables, bold anchors, command blocks, and ASCII diagrams.",
        path="backend/services/chat_orchestrator.py",
        needles=(
            "Agent-Zero-inspired chat render style",
            "plain-text ASCII diagrams",
            "fenced `text` block",
            "grid-style Markdown tables",
            "numbered lists",
            "fenced `json` block",
            "tiny ASCII bar chart",
            "Mandatory display contract",
            "plain wall",
            "preferred visual grammar",
            "bold thesis",
            "ASCII",
        ),
    ),
    Requirement(
        id="rag-answer-render-policy",
        description="RAG-augmented prompts carry a dedicated answer rendering policy for markdown structure without exposing retrieval internals.",
        path="backend/services/context_manager.py",
        needles=(
            "<answer_render_policy>",
            "compact GFM tables",
            "fenced `text` blocks for ASCII",
            "Use a fenced `json` block",
            "grid-style GFM Markdown table",
            "Use a numbered list",
            "Query-specific display requirement",
            "_answer_render_hint",
            "Do not expose this policy",
        ),
    ),
    Requirement(
        id="frontend-chat-rich-markdown-surfaces",
        description="Chat renderer recognizes command and ASCII blocks and styles rich markdown answer surfaces.",
        path="frontend/src/components/chat/MessageBubble.tsx",
        needles=(
            "pm-command-block",
            "pm-ascii-block",
            "pm-json-block",
            "language === \"json\"",
            "language === \"text\"",
            "remarkPlugins={[remarkGfm]}",
        ),
    ),
    Requirement(
        id="hyde-specific-query-preserves-concepts",
        description="HyDE skips compact definition/relation queries so original query concepts remain in retrieval.",
        path="backend/services/chat_orchestrator.py",
        needles=(
            "_HYDE_SPECIFIC_RELATION_MARKERS",
            "what is",
            "original concept surviving retrieval",
        ),
    ),
    Requirement(
        id="context-rag-answer-policy",
        description="The RAG context envelope carries an explicit answer policy next to retrieved excerpts.",
        path="backend/services/context_manager.py",
        needles=(
            "<rag_answer_policy>",
            "answer from that evidence first",
            "Do not replace source-backed evidence with a generic",
            "Do not introduce ",
            "named libraries, frameworks",
            "explicitly asks for outside",
        ),
    ),
    Requirement(
        id="chat-retrieval-diagnostics-trace",
        description="Chat streaming exposes tier-specific retrieval diagnostics in the live trace lane.",
        path="backend/services/chat_orchestrator.py",
        needles=(
            "_format_retrieval_diagnostics_trace",
            "[Retrieval tier trace]",
            "retrieval_diagnostics",
            "final_source_tiers",
        ),
    ),
    Requirement(
        id="chat-tier-synthesis-lenses",
        description="Chat injects a tier-specific synthesis lens so Vector, Hybrid, and Graph answer the same query through different RAG contracts.",
        path="backend/services/chat_orchestrator.py",
        needles=(
            "_format_retrieval_tier_synthesis_contract",
            "<retrieval_synthesis_lens>",
            "semantic overview",
            "hydrated corpus synthesis",
            "relationship map",
            "Synthesis lens",
        ),
    ),
    Requirement(
        id="chat-retrieval-nuance-digest",
        description="Chat synthesis receives a deduped, capped, leak-guarded salient-context hint from the final retrieval packet.",
        path="backend/services/chat_orchestrator.py",
        needles=(
            "_build_retrieval_nuance_digest",
            "<retrieval_nuance_digest>",
            "high_frequency_context",
            "Retrieval nuance",
            "salient_terms",
            "NEVER output these terms as a list",
        ),
    ),
    Requirement(
        id="frontend-stream-consumer",
        description="Frontend consumes token/thinking SSE events and flushes them into the live streaming message.",
        path="frontend/src/App.tsx",
        needles=(
            'case "token":',
            "chat.updateStreamingContent(tokenChunk)",
            'case "thinking":',
            "chat.updateStreamingThinking(thinkingChunk)",
            "scheduleStreamingFlush",
            "window.setTimeout(flushStreamingBuffers, 16)",
        ),
    ),
    Requirement(
        id="frontend-live-stream-ui",
        description="Chat UI mounts the assistant stream immediately, anchors it, and surfaces live reasoning before answer text arrives.",
        path="frontend/src/components/chat/MessageBubble.tsx",
        needles=(
            "function LiveAnswerDraft",
            "pm-live-answer-draft",
            "ProcessTimeline",
            "defaultOpen={!hasAssistantContent}",
            "manualClosedIds",
            "(!isStreaming && defaultOpen && !manualClosedIds.has(group.id))",
        ),
    ),
    Requirement(
        id="frontend-stream-shell",
        description="Chat window renders the streaming assistant message even before first token and avoids the old dead generating placeholder.",
        path="frontend/src/components/chat/ChatWindow.tsx",
        needles=(
            "streamingMessageRef",
            "latestAssistantMessageRef",
            "previousStreamingRef",
            "justFinishedStreaming",
            "showWorkingIndicator = isLoading && !isStreaming && !hasStreamingOutput",
            "{isStreaming && (",
            "scrollIntoView({",
        ),
    ),
    Requirement(
        id="frontend-immediate-query-trace",
        description="Starting a chat turn creates an immediate visible process item before backend trace/token events arrive.",
        path="frontend/src/stores/chatStore.ts",
        needles=(
            "Preparing retrieval",
            "Opening the live query stream and preparing corpus retrieval.",
            "streamingProcessTimeline: [",
        ),
    ),
    Requirement(
        id="compose-health-and-workers",
        description="Compose declares healthchecks, autoheal, and backend worker/cache environment defaults.",
        path="docker-compose.yml",
        needles=(
            "autoheal:",
            'healthcheck:',
            "INGEST_BATCH_WORKERS:",
            "GRAPH_CACHE_WARMUP_SKIP_DURING_ACTIVE_INGEST:",
            "http://localhost:8000/api/health/live",
        ),
    ),
    Requirement(
        id="profile-aware-download-cli",
        description="Fresh installs have one deterministic download/setup CLI that detects Apple MLX, RTX, or CPU/cloud profiles and writes an auditable plan.",
        path="scripts/polymath_download.py",
        needles=(
            'choices=["auto", "apple-mlx", "rtx", "cpu-cloud"]',
            "def detect_profile()",
            "mlx-community/Qwen3-Embedding-0.6B-mxfp8",
            "ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF",
            "docker-compose.apple-mlx.yml",
            "polymath-download-plan.json",
            "scripts/bootstrap-runtime.sh",
            "scripts/bootstrap-runtime.ps1",
            "scripts/setup_apple_mlx.sh",
            "scripts/check-install.ps1",
        ),
    ),
    Requirement(
        id="install-check-runs-contracts",
        description="Fresh install checks invoke this runtime contract verifier.",
        path="scripts/check-install.sh",
        needles=("verify_runtime_contracts.py",),
    ),
    Requirement(
        id="windows-install-check-runs-contracts",
        description="Windows fresh install checks invoke this runtime contract verifier.",
        path="scripts/check-install.ps1",
        needles=("verify_runtime_contracts.py",),
    ),
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _read(root: Path, rel: str) -> str | None:
    path = root / rel
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _check(requirement: Requirement, root: Path) -> Result:
    text = _read(root, requirement.path)
    if text is None:
        return Result(
            id=requirement.id,
            status="FAIL",
            path=requirement.path,
            description=requirement.description,
            missing=("file missing",),
        )

    missing: list[str] = []
    for needle in requirement.needles:
        if requirement.regex:
            if not re.search(needle, text, flags=re.MULTILINE | re.DOTALL):
                missing.append(needle)
        elif needle not in text:
            missing.append(needle)

    return Result(
        id=requirement.id,
        status="FAIL" if missing else "PASS",
        path=requirement.path,
        description=requirement.description,
        missing=tuple(missing),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Polymath runtime setup/worker/trigger contracts."
    )
    parser.add_argument(
        "--repo-root",
        default=str(_repo_root()),
        help="Repository root. Defaults to the parent of this script.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON.",
    )
    args = parser.parse_args()

    root = Path(args.repo_root).resolve()
    results = [_check(req, root) for req in REQUIREMENTS]
    failed = [result for result in results if result.status != "PASS"]

    if args.json:
        print(
            json.dumps(
                {
                    "repo_root": str(root),
                    "passed": len(results) - len(failed),
                    "failed": len(failed),
                    "results": [asdict(result) for result in results],
                },
                indent=2,
            )
        )
    else:
        print("Polymath runtime contract check")
        print(f"Repo: {root}")
        for result in results:
            prefix = "[ OK ]" if result.status == "PASS" else "[FAIL]"
            print(f"{prefix} {result.id} — {result.description}")
            if result.status != "PASS":
                print(f"       file: {result.path}")
                for missing in result.missing:
                    print(f"       missing: {missing}")
        print()
        print(f"Summary: {len(results) - len(failed)} passed, {len(failed)} failed")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
