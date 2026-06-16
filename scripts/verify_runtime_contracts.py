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
        description="Ingestion service startup repairs per-corpus Qdrant collections and initializes Neo4j schema constraints.",
        path="backend/services/ingestion_service.py",
        needles=(
            "ensure_collections_for_corpus",
            "from services.graph.schema import initialize_schema",
            "await initialize_schema(self._neo4j)",
            "from services.ingestion.worker import run_ingest_job",
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
            "if ws.qdrant_written and ws.neo4j_written:",
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
            "await emerge_domains(qdrant, neo4j_driver, db, corpus_id)",
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
            "physical batch",
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
            "rerank_top_n_graph_floor",
            "full RAG",
            "graph_floor = max",
            "before the cross-encoder sees hydrated text",
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
            "formatLiveThinkingPreview",
            "pm-live-reasoning-preview",
            "ProcessTimeline",
            "defaultOpen={!hasAssistantContent}",
            "manualClosedIds",
            "(isStreaming || defaultOpen) && !manualClosedIds.has(group.id)",
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
