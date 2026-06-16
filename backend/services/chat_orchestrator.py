# backend/services/chat_orchestrator.py
# Chat orchestrator service - moves business logic from router to service layer
# Orchestrates: conversation loading, message creation, trimming, LLM streaming, saving
# All functions are async. Import: from services.chat_orchestrator import chat_orchestrator

import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime
from time import perf_counter
from typing import Any, AsyncGenerator
from urllib.parse import urlparse

from bson import ObjectId
from config import get_settings
from models.schemas import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ModelConfig,
    ModelOverrides,
    RetrievalTier,
    SourceChunk,
)
from services.context_manager import context_manager
from services.conversation import conversation_service
from services.facets import (
    FacetCandidate,
    matching_ingest_facets,
    matching_vector_facets,
    metadata_facet_terms,
    normalize_facet_id,
    select_facet_final,
)
from services.llm import llm_service
from services.retriever import retriever_orchestrator
from services.tool_registry import tool_registry
# Phase 24 perf — hoist hot-path imports to module level so each chat turn
# doesn't pay the import-resolution cost (was previously inside `try:` blocks
# in process_chat_request).
from services.skills_registry import skills_registry
from services.reasoning_cascade import analyze as reasoning_cascade_analyze
from services.query_model_resolver import (
    resolve as resolve_query_model_kind,
    resolve_by_entry_id,
)
from services.settings import settings_service
from utils.streaming import build_sse_chunk
from utils.tokens import count_tokens

logger = logging.getLogger(__name__)
settings = get_settings()

HYDE_FAILURE_TTL_SECONDS = 600.0
_HYDE_FAILURE_CACHE: dict[str, float] = {}
_MAX_PERSISTED_SOURCE_PREVIEWS = 10
_MAX_PERSISTED_WEB_SOURCE_PREVIEWS = 20
_MAX_PERSISTED_SOURCE_TEXT_CHARS = 900
_MAX_PERSISTED_SOURCE_SUMMARY_CHARS = 500
_MAX_TOOL_CALLS_PER_TURN = 5
_MAX_WEB_SEARCH_CALLS_PER_TURN = 3
_MAX_WEB_SEARCH_RESULTS_PER_CALL = 20
_DEFAULT_EVIDENCE_MAX_SOURCES = 9
_CHAT_COVERAGE_MAX_DYNAMIC_SUPPLEMENTS = 4
_CHAT_COVERAGE_THRESHOLD = 4
_CHAT_COVERAGE_WEAK_THRESHOLD = 2
_CHAT_COVERAGE_SOURCE_CAP = 8
# Per-domain cap on the final context set for BROAD (global) queries — at most
# N chunks from any single emergent domain/cluster, so an overview answer spans
# disciplines instead of clustering on the reranker's favorite domain. Only
# enforced when search_mode == "global" and chunks carry a domain label.
_CHAT_COVERAGE_DOMAIN_CAP = 3
# Global/overview queries should use more of the (often 30+ doc) retrieval pool
# than a focused query — widen the coverage budget AND the distinct-doc cap for
# global mode so an overview answer spans more documents/domains. Local unaffected.
# (Eval: overview pools held ~35 docs but answers used only 6-7 at the default cap.)
_GLOBAL_OVERVIEW_BUDGET = 12
# Cross-model fallback for the in-process stream retry: when the primary synthesis
# model fails BEFORE producing any content (e.g. a lapsed Ollama Cloud model 500s),
# retry once on this known-good backup so the answer is never blank. litellm v1.60
# does not auto-fallback on a streamed error (Issue #6532). Routes via litellm's
# deepseek/* group (DEEPSEEK_API_KEY in .env); no per-request credentials.
_CHAT_FALLBACK_MODEL = "deepseek/deepseek-chat"

# Phase 4 — lightweight LLM intent classifier. The marker router
# (search_mode.infer_search_mode) misses natural overview questions that lack a
# marker phrase; this is a cached second-chance: when auto-mode resolved to
# "local", ask the backup model whether the query is a broad/overview question
# and, if so, upgrade to "global" so the domain-stratified breadth path fires.
# The marker result is the fallback on any error/timeout. Cached per query.
_INTENT_CACHE: dict[str, str] = {}
_INTENT_SYSTEM = (
    "Classify the user's question as exactly one lowercase word.\n"
    "Answer 'overview' ONLY when the user wants a broad, corpus-wide synthesis "
    "spanning many documents — e.g. 'main themes', 'what's in my whole library', "
    "'summarize everything', 'recurring patterns across my notes', 'big picture'.\n"
    "Answer 'specific' for anything targeting a particular fact, document, entity, "
    "how-to, debugging, error, code, or single topic — e.g. 'how do I fix X', "
    "'what does Y do', 'where is Z', 'explain this error'.\n"
    "When in doubt, answer 'specific'. Reply with only the single word."
)
_OVERVIEW_CLASSIFIER_HINTS = (
    "overview",
    "summarize",
    "summary",
    "main themes",
    "key themes",
    "main ideas",
    "key ideas",
    "big picture",
    "high level",
    "high-level",
    "across",
    "overall",
    "whole corpus",
    "library",
    "what topics",
    "what subjects",
    "patterns",
    "recurring",
)
_SIMPLE_DEFINITION_RE = re.compile(
    r"^\s*(what\s+is|what\s+are|define|explain)\b",
    re.IGNORECASE,
)


def _should_run_overview_intent_classifier(message: str) -> bool:
    """Only pay the LLM classifier tax when the query looks plausibly broad."""

    text = (message or "").strip()
    if not text:
        return False
    lower = text.lower()
    tokens = re.findall(r"[a-z0-9]+", lower)
    if _SIMPLE_DEFINITION_RE.search(text):
        return any(hint in lower for hint in _OVERVIEW_CLASSIFIER_HINTS)
    return len(tokens) >= 12 or any(hint in lower for hint in _OVERVIEW_CLASSIFIER_HINTS)


async def _classify_overview_intent(message: str) -> str:
    """Return 'global' if the LLM judges the query an overview/thematic question,
    else 'local'. Cached per normalized query; best-effort (any failure -> 'local')."""
    key = (message or "").strip().lower()[:300]
    if not key:
        return "local"
    cached = _INTENT_CACHE.get(key)
    if cached is not None:
        return cached
    result = "local"
    try:
        answer = await llm_service.complete_sync(
            messages=[
                {"role": "system", "content": _INTENT_SYSTEM},
                {"role": "user", "content": message[:2000]},
            ],
            model=_CHAT_FALLBACK_MODEL,
            temperature=0,
            max_tokens=4,
            timeout=8.0,
        )
        if "overview" in (answer or "").strip().lower():
            result = "global"
    except Exception as exc:
        logger.debug("intent classifier skipped: %s", exc)
    _INTENT_CACHE[key] = result
    return result
_CHAT_EVIDENCE_MIN_KEEP_AFTER_FILTER = 4
_RAW_TOOL_REQUEST_MARKERS = (
    "<｜｜dsml｜｜tool_calls",
    "<tool_calls",
    "tool_calls>",
    "invoke name=",
    "\"tool_calls\"",
    "'tool_calls'",
)

_REFERENCE_QUERY_RE = re.compile(
    r"\b(reference|references|bibliography|citation|citations|works cited|"
    r"related work|literature review|acknowledg(?:e)?ments?)\b",
    re.IGNORECASE,
)
_LOW_VALUE_EVIDENCE_RE = re.compile(
    r"\b(references?|bibliography|works cited|acknowledg(?:e)?ments?|"
    r"about the reviewers?|about the authors?|join our (?:book'?s |community'?s )?"
    r"discord|discord workspace|table of contents|title page|preface)\b",
    re.IGNORECASE,
)
_FRONTMATTER_RE = re.compile(r"\A\s*---\s*\n.*?\n---\s*\n", re.DOTALL)

_CHAT_COVERAGE_FACETS: tuple[dict[str, Any], ...] = (
    {
        "name": "on_device_llm",
        "label": "on-device AI / local LLM",
        "triggers": (
            "on-device ai",
            "on device ai",
            "on-device llm",
            "on device llm",
            "on-device assistant",
            "on device assistant",
            "local llm",
            "local ai",
            "small language model",
            "small language models",
            "edge inference",
            "private inference",
            "local inference",
            "offline model",
            "offline ai",
            "runs on device",
            "data stays on device",
        ),
        "support_terms": (
            "on-device AI",
            "on-device LLM",
            "local LLM",
            "local AI",
            "small language model",
            "edge inference",
            "private inference",
            "local inference",
            "offline model",
            "mobile AI",
            "device-local processing",
            "data stays on device",
        ),
    },
    {
        "name": "privacy",
        "label": "privacy / user data control",
        "triggers": (
            "privacy",
            "private",
            "privacy-preserving",
            "privacy preserving",
            "data privacy",
            "user data",
            "data stays on device",
            "local-first",
            "local first",
            "data minimization",
            "consent",
            "confidential",
        ),
        "support_terms": (
            "privacy",
            "privacy-preserving",
            "data privacy",
            "private user data",
            "sensitive data",
            "local-first",
            "data minimization",
            "consent",
            "user data control",
            "data stays on device",
        ),
    },
    {
        "name": "knowledge_graph",
        "label": "knowledge graph / graph RAG",
        "triggers": (
            "knowledge graph",
            "knowledge graphs",
            "graph rag",
            "graph database",
            "neo4j",
            "rdf",
            "ontology",
            "schema",
            "nodes and edges",
        ),
        "support_terms": (
            "knowledge graph",
            "graph RAG",
            "graph database",
            "RDF triples",
            "ontology",
            "schema",
            "linked data",
            "entity relationship",
            "semantic network",
            "concept map",
            "graph-based reasoning",
            "personal knowledge graph",
        ),
    },
    {
        "name": "user_modeling",
        "label": "user modeling / profiling",
        "triggers": (
            "user modeling",
            "user model",
            "user profile",
            "user profiling",
            "adaptive system",
            "personalization",
        ),
        "support_terms": (
            "user modeling",
            "user model",
            "private user model",
            "personal user model",
            "user profile",
            "user profiling",
            "user representation",
            "user preferences",
            "adaptive systems",
            "personalization",
            "cognitive profile",
            "student model",
            "persona inference",
            "adaptive user representation",
        ),
    },
    {
        "name": "psychometrics",
        "label": "psychometrics / measurement",
        "triggers": (
            "psychometrics",
            "psychometric",
            "measurement",
            "measure",
            "validity",
            "latent variable",
            "assessment",
            "score",
        ),
        "support_terms": (
            "psychometrics",
            "psychometric",
            "measurement",
            "test validity",
            "latent variable",
            "assessment",
            "score",
        ),
    },
    {
        "name": "neuro_narrative",
        "label": "neuro-narrative therapy",
        "triggers": (
            "neuro narrative",
            "neuro-narrative",
            "narrative therapy",
            "neuroscience",
            "embodied",
            "affective",
            "affect theory",
        ),
        "support_terms": (
            "neuro-narrative therapy",
            "narrative therapy",
            "neuroscience",
            "embodiment",
            "affect",
            "emotion-filled conversations",
            "narrative reconstruction",
            "autobiographical memory",
            "self story",
            "narrative identity",
            "trauma narrative",
            "re-authoring",
        ),
    },
    {
        "name": "socialization",
        "label": "socialization / professional world",
        "triggers": (
            "socialization",
            "secondary socialization",
            "professional world",
            "institutional world",
            "sub-world",
            "home world",
            "significant others",
        ),
        "support_terms": (
            "secondary socialization",
            "professional world",
            "institutional sub-worlds",
            "significant others",
            "social stock of knowledge",
            "reality maintenance",
        ),
    },
    {
        "name": "identity_narrative",
        "label": "identity / narrative formation",
        "triggers": (
            "identity",
            "narrative",
            "self narrative",
            "hero journey",
            "meaning making",
            "values",
            "choices",
        ),
        "support_terms": (
            "identity",
            "narrative",
            "narrative identity",
            "narrative construction",
            "narrative construction of reality",
            "self narrative",
            "self story",
            "life story",
            "Jerome Bruner",
            "hero's journey",
            "meaning making",
            "meaning-making",
            "values",
            "choices",
        ),
    },
    {
        "name": "design_system",
        "label": "design / system intervention",
        "triggers": (
            "design",
            "interface",
            "ux",
            "user experience",
            "affordance",
            "prototype",
            "system intervention",
        ),
        "support_terms": (
            "design principles",
            "interface",
            "user experience",
            "affordance",
            "prototype",
            "system intervention",
        ),
    },
    {
        "name": "platform_ecosystem",
        "label": "platform ecosystem",
        "triggers": (
            "platform ecosystem",
            "platform revolution",
            "network effects",
            "marketplace",
            "producer",
            "consumer",
        ),
        "support_terms": (
            "platform ecosystem",
            "network effects",
            "multi-sided market",
            "marketplace",
            "producer consumer",
        ),
    },
)

_CHAT_COMPOUND_QUERY_FACET_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("privacy-preserving on-device ai", ("privacy", "on_device_llm")),
    ("privacy preserving on device ai", ("privacy", "on_device_llm")),
    ("privacy-preserving on-device llm", ("privacy", "on_device_llm")),
    ("privacy preserving on device llm", ("privacy", "on_device_llm")),
    ("privacy-preserving local llm", ("privacy", "on_device_llm")),
    ("privacy preserving local llm", ("privacy", "on_device_llm")),
    ("data stays on device", ("privacy", "on_device_llm")),
    ("data remains on device", ("privacy", "on_device_llm")),
    ("keep data on device", ("privacy", "on_device_llm")),
    ("keeps data on device", ("privacy", "on_device_llm")),
    ("runs on device", ("on_device_llm",)),
    ("on-device ai", ("on_device_llm",)),
    ("on device ai", ("on_device_llm",)),
    ("on-device llm", ("on_device_llm",)),
    ("on device llm", ("on_device_llm",)),
    ("local llm", ("on_device_llm",)),
    ("small language model", ("on_device_llm",)),
    ("small language models", ("on_device_llm",)),
    ("edge inference", ("on_device_llm",)),
    ("private inference", ("privacy", "on_device_llm")),
    ("local inference", ("on_device_llm",)),
    ("privacy-preserving", ("privacy",)),
    ("privacy preserving", ("privacy",)),
    ("local-first", ("privacy",)),
    ("local first", ("privacy",)),
)


def _hyde_failure_key(model: str | None, api_base: str | None) -> str:
    """Group HyDE failures by endpoint so one bad helper model doesn't tax every query."""
    return f"{api_base or '(litellm)'}::{model or '(default)'}"


_HYDE_SOURCE_CONSTRAINT_MARKERS = (
    "retrieved excerpts",
    "provided excerpts",
    "provided context",
    "direct textual support",
    "direct support",
    "distinguish direct",
    "distinguish textual",
    "verbatim",
    "quote",
    "quoted",
    "cite",
    "citation",
)

_HYDE_SPECIFIC_RELATION_MARKERS = (
    "how does",
    "how do",
    "relate to",
    "related to",
    "relationship between",
    "relation between",
    "difference between",
    "compare",
    "versus",
    " vs ",
)


def _should_skip_hyde_for_query(query: str) -> bool:
    """Avoid query rewriting when the user is auditing source support.

    HyDE is valuable for broad cross-domain discovery, but source-constrained
    questions need the original wording preserved. A hypothetical answer can
    accidentally smuggle in the very bridge the user is asking us to verify.
    Likewise, compact definition/relation questions often depend on every
    original concept surviving retrieval ("what is X and how does Y relate?").
    """
    text = (query or "").lower()
    if not text:
        return False
    if any(marker in text for marker in _HYDE_SOURCE_CONSTRAINT_MARKERS):
        return True
    if (
        ("what is" in text or "define " in text or "explain " in text)
        and any(marker in text for marker in _HYDE_SPECIFIC_RELATION_MARKERS)
    ):
        return True
    return ("based on" in text or "according to" in text) and (
        "inferred" in text
        or "textual" in text
        or "evidence" in text
        or "support" in text
    )


def _clip_source_text(value: Any, max_chars: int) -> str | None:
    """Return a bounded text preview for persisted source snippets."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _source_to_dict(source: Any) -> dict[str, Any] | None:
    if hasattr(source, "model_dump"):
        return source.model_dump(mode="json")
    if isinstance(source, dict):
        return dict(source)
    return None


def _source_metadata(data: dict[str, Any]) -> dict[str, Any]:
    metadata = data.get("metadata")
    return metadata if isinstance(metadata, dict) else {}


def _is_web_source_data(data: dict[str, Any]) -> bool:
    return (
        data.get("source_tier") == "web_search"
        or data.get("corpus_id") == "live-web"
        or str(data.get("chunk_id") or "").startswith("web:")
    )


def _web_source_key(source: Any) -> str | None:
    data = _source_to_dict(source)
    if not data or not _is_web_source_data(data):
        return None
    metadata = _source_metadata(data)
    key = metadata.get("url") or data.get("doc_id") or data.get("chunk_id")
    return str(key).strip() if key else None


def _append_deduped_web_sources(existing: list[Any], pending: list[Any]) -> list[Any]:
    """Append web/tool sources while keeping one entry per URL."""
    if not pending:
        return existing

    seen = {
        key
        for source in existing
        if (key := _web_source_key(source))
    }
    merged = list(existing)
    for source in pending:
        key = _web_source_key(source)
        if key:
            if key in seen:
                continue
            seen.add(key)
        merged.append(source)
    return merged


def _cap_web_sources_for_turn(sources: list[Any]) -> list[Any]:
    """Keep web source cards bounded across repeated web searches in one turn."""
    capped: list[Any] = []
    web_count = 0
    for source in sources:
        data = _source_to_dict(source)
        if data and _is_web_source_data(data):
            if web_count >= _MAX_PERSISTED_WEB_SOURCE_PREVIEWS:
                continue
            web_count += 1
        capped.append(source)
    return capped


def _safe_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _resolve_web_evidence_options(request: ChatRequest | None) -> dict[str, Any]:
    """Resolve the four user-facing web knobs into bounded runtime budgets."""
    overrides = request.overrides if request is not None else None
    research_mode = bool(getattr(overrides, "web_research_mode", None))
    raw_depth = str(getattr(overrides, "web_fetch_depth", None) or "normal").lower()
    fetch_depth = raw_depth if raw_depth in {"snippets", "normal", "deep"} else "normal"
    if research_mode and fetch_depth == "normal":
        fetch_depth = "deep"

    youtube_value = getattr(overrides, "web_youtube_transcripts", None)
    youtube_transcripts = True if youtube_value is None else bool(youtube_value)

    requested_sources = _safe_int(
        getattr(overrides, "web_max_sources", None),
        _DEFAULT_EVIDENCE_MAX_SOURCES,
        minimum=3,
        maximum=_MAX_WEB_SEARCH_RESULTS_PER_CALL,
    )
    effective_sources = requested_sources * (2 if research_mode else 1)
    effective_sources = max(3, min(effective_sources, _MAX_WEB_SEARCH_RESULTS_PER_CALL))

    configured_fetch_pages = _safe_int(
        getattr(settings, "LIVE_WEB_FETCH_MAX_PAGES", 6),
        6,
        minimum=0,
        maximum=20,
    )
    max_fetch_pages = configured_fetch_pages * (2 if research_mode else 1)
    max_fetch_pages = max(0, min(max_fetch_pages, 20))

    configured_candidates = _safe_int(
        getattr(settings, "LIVE_WEB_SEARCH_CANDIDATE_RESULTS", effective_sources),
        effective_sources,
        minimum=effective_sources,
        maximum=40,
    )
    candidate_limit = max(
        effective_sources,
        configured_candidates,
        effective_sources * (2 if research_mode else 1),
    )
    candidate_limit = max(effective_sources, min(candidate_limit, 40))

    return {
        "fetch_depth": fetch_depth,
        "research_mode": research_mode,
        "youtube_transcripts": youtube_transcripts,
        "requested_max_sources": requested_sources,
        "max_sources": effective_sources,
        "candidate_limit": candidate_limit,
        "max_fetch_pages": max_fetch_pages,
    }


def _source_title(data: dict[str, Any]) -> str:
    metadata = _source_metadata(data)
    for value in (
        data.get("doc_name"),
        metadata.get("title"),
        metadata.get("filename"),
        metadata.get("url"),
        data.get("doc_id"),
        data.get("chunk_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "source"


def _source_excerpt(data: dict[str, Any], *, max_chars: int) -> str:
    text = str(data.get("text") or data.get("summary") or "").strip()
    marker = "\nContent: "
    if marker in text:
        text = text.split(marker, 1)[1]
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _dedupe_for_evidence_packet(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate by URL/domain/content fingerprint, not score shape."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for data in items:
        metadata = _source_metadata(data)
        url = str(metadata.get("url") or data.get("doc_id") or "").strip().lower()
        title = _source_title(data).lower()
        text = " ".join(str(data.get("text") or "").split()).lower()
        fingerprint = f"{url}|{title[:120]}|{text[:240]}"
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(data)
    return deduped


def _collect_web_run_summaries(request: ChatRequest | None) -> list[dict[str, Any]]:
    if request is None:
        return []
    runs = getattr(request, "_web_evidence_runs", None)
    return list(runs) if isinstance(runs, list) else []


def _record_web_evidence_run(
    request: ChatRequest | None,
    summary: dict[str, Any],
) -> None:
    if request is None:
        return
    runs = _collect_web_run_summaries(request)
    runs.append(summary)
    object.__setattr__(request, "_web_evidence_runs", runs[-6:])


def _web_health_from_runs(runs: list[dict[str, Any]]) -> tuple[str, list[str]]:
    if not runs:
        return "no_web", []
    errors: list[str] = []
    total_results = 0
    for run in runs:
        total_results += int(run.get("result_count") or 0)
        for item in run.get("engine_errors") or []:
            text = str(item or "").strip()
            if text and text not in errors:
                errors.append(text)
    if total_results <= 0:
        return "failed_search", errors
    if errors or any(run.get("degraded") for run in runs):
        return "degraded_search", errors
    return "ok", []


_EVIDENCE_SCORE_STOPWORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "can",
    "does",
    "for",
    "from",
    "how",
    "into",
    "its",
    "latest",
    "like",
    "more",
    "should",
    "that",
    "the",
    "their",
    "then",
    "this",
    "use",
    "using",
    "what",
    "when",
    "where",
    "why",
    "with",
}


def _evidence_score_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+#-]*", value.lower())
        if len(token) >= 3 and token not in _EVIDENCE_SCORE_STOPWORDS
    }


def _query_coverage(query: str, text: str) -> float:
    query_tokens = _evidence_score_tokens(query)
    if not query_tokens:
        return 0.0
    text_tokens = _evidence_score_tokens(text)
    if not text_tokens:
        return 0.0
    denominator = min(len(query_tokens), 8)
    return round(min(1.0, len(query_tokens & text_tokens) / denominator), 3)


def _score_web_evidence_chunk(
    *,
    query: str,
    chunk: Any,
    seen_domains: set[str],
    seen_types: set[str],
) -> dict[str, Any]:
    data = _source_to_dict(chunk) or {}
    metadata = _source_metadata(data)
    url = str(metadata.get("url") or data.get("doc_id") or "")
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    source_type = str(metadata.get("source_type") or "webpage")
    evidence_mode = str(metadata.get("evidence_mode") or "snippet_only")
    fetch_method = str(metadata.get("fetch_method") or "snippet")
    text = str(data.get("text") or "")
    title = _source_title(data)

    try:
        rerank_score = float(data.get("score") or getattr(chunk, "score", 0.0) or 0.0)
    except (TypeError, ValueError):
        rerank_score = 0.0
    relevance = max(0.0, min(1.0, rerank_score))
    coverage = _query_coverage(query, f"{title} {text}")
    relevance = round(max(relevance, coverage), 3)

    if evidence_mode == "full_page":
        completeness = 0.92
    elif fetch_method == "yt_dlp" or source_type == "video":
        completeness = 0.88
    elif str(metadata.get("search_provider") or "").lower() == "wikipedia":
        completeness = 0.84
    elif evidence_mode == "snippet_fetch_failed":
        completeness = 0.28
    else:
        completeness = 0.48

    intent_fit = coverage
    if any(
        marker in query.lower()
        for marker in ("official", "docs", "documentation", "api", "reference")
    ):
        if any(
            marker in url.lower()
            for marker in ("docs", "developer", "reference", "github.com")
        ):
            intent_fit = max(intent_fit, 0.9)
    if any(marker in query.lower() for marker in ("tutorial", "demo", "walkthrough")):
        if source_type == "video" or "youtube" in url.lower():
            intent_fit = max(intent_fit, 0.85)

    diversity_bonus = 0.0
    if domain and domain not in seen_domains:
        diversity_bonus += 0.08
    if source_type and source_type not in seen_types:
        diversity_bonus += 0.07
    diversity_bonus = round(diversity_bonus, 3)

    penalty = 0.0
    if metadata.get("fetch_failed"):
        penalty += 0.15
    if metadata.get("engine_errors"):
        penalty += 0.05
    if metadata.get("content_truncated"):
        penalty += 0.03

    final = round(
        max(
            0.0,
            min(
                1.0,
                relevance * 0.5
                + completeness * 0.25
                + intent_fit * 0.15
                + diversity_bonus
                - penalty,
            ),
        ),
        3,
    )
    return {
        "final": final,
        "relevance": relevance,
        "completeness": round(completeness, 3),
        "intent_fit": round(intent_fit, 3),
        "diversity_bonus": diversity_bonus,
        "penalty": round(penalty, 3),
        "domain": domain,
        "source_type": source_type,
    }


def _annotate_web_evidence_scores(query: str, chunks: list[Any]) -> list[dict[str, Any]]:
    seen_domains: set[str] = set()
    seen_types: set[str] = set()
    scores: list[dict[str, Any]] = []
    for chunk in chunks:
        score = _score_web_evidence_chunk(
            query=query,
            chunk=chunk,
            seen_domains=seen_domains,
            seen_types=seen_types,
        )
        scores.append(score)
        if score.get("domain"):
            seen_domains.add(str(score["domain"]))
        if score.get("source_type"):
            seen_types.add(str(score["source_type"]))
        metadata = dict(getattr(chunk, "metadata", None) or {})
        metadata["evidence_score"] = score
        try:
            chunk.metadata = metadata
        except Exception:
            pass
    return scores


def _classify_web_evidence_sufficiency(
    *,
    chunks: list[Any],
    scores: list[dict[str, Any]],
    engine_errors: list[str],
    pipeline: dict[str, Any],
) -> dict[str, Any]:
    """Hard web-evidence grade for the final model and UI telemetry."""
    result_count = len(chunks)
    best_score = max((float(score.get("final") or 0.0) for score in scores), default=0.0)
    avg_score = (
        sum(float(score.get("final") or 0.0) for score in scores) / len(scores)
        if scores
        else 0.0
    )
    full_page_successes = int(pipeline.get("full_page_fetch_successes") or 0)
    snippet_score = float(pipeline.get("snippet_sufficiency_score") or 0.0)
    degraded = bool(engine_errors)

    if result_count == 0:
        grade = "insufficient"
        reason = "no_final_web_sources"
    elif best_score >= 0.72 and avg_score >= 0.55 and result_count >= 3 and not degraded:
        grade = "confident"
        reason = "multiple_relevant_sources"
    elif (
        best_score >= 0.68
        and result_count >= 2
        and (full_page_successes > 0 or snippet_score >= 0.72)
    ):
        grade = "confident" if not degraded else "partial"
        reason = "strong_relevance_with_page_or_rich_snippet_evidence"
    elif best_score >= 0.45 or result_count >= 2:
        grade = "partial"
        reason = "some_relevant_evidence_but_thin_or_degraded"
    else:
        grade = "insufficient"
        reason = "low_relevance_or_too_little_evidence"

    return {
        "grade": grade,
        "reason": reason,
        "best_score": round(best_score, 3),
        "avg_score": round(avg_score, 3),
        "result_count": result_count,
        "degraded": degraded,
    }


def _build_backend_retry_query(
    *,
    search_query: str,
    original_query: str | None,
) -> str | None:
    """Deterministic alternate query for one backend-owned recovery attempt."""
    base = re.sub(r"\b(site:[^\s]+|![a-z]+\s*)", " ", original_query or search_query)
    base = re.sub(r"[^A-Za-z0-9.+#_/-]+", " ", base)
    tokens = [
        token
        for token in base.split()
        if len(token) >= 3 and token.lower() not in _EVIDENCE_SCORE_STOPWORDS
    ]
    anchors = re.findall(r"\b[A-Z][A-Za-z0-9_]*(?:Service|API|SDK|DB|RAG|LLM)?\b", original_query or search_query)
    ordered: list[str] = []
    for token in [*anchors, *tokens]:
        cleaned = token.strip(".,;:()[]{}")
        if not cleaned or cleaned.lower() in {item.lower() for item in ordered}:
            continue
        ordered.append(cleaned)
        if len(ordered) >= 10:
            break
    if not ordered:
        return None
    retry = " ".join(ordered)
    lower_original = (original_query or search_query).lower()
    if any(marker in lower_original for marker in ("official", "docs", "documentation", "api", "reference")):
        if "official" not in retry.lower():
            retry = f"{retry} official documentation"
    if retry.lower() == search_query.lower():
        retry = f"{retry} guide reference"
    return retry[:300]


def _format_evidence_packet_block(
    *,
    sources: list[Any] | None,
    request: ChatRequest | None,
) -> str:
    """Build the explicit evidence contract shown to the final chat model."""
    source_dicts = [
        data
        for source in (sources or [])
        if (data := _source_to_dict(source)) is not None
    ]
    if not source_dicts and not _collect_web_run_summaries(request):
        return ""

    options = _resolve_web_evidence_options(request)
    runs = _collect_web_run_summaries(request)
    web_sources = _dedupe_for_evidence_packet(
        [data for data in source_dicts if _is_web_source_data(data)]
    )
    corpus_sources = _dedupe_for_evidence_packet(
        [data for data in source_dicts if not _is_web_source_data(data)]
    )
    web_limit = int(options["max_sources"])
    corpus_limit = min(8, len(corpus_sources))
    web_selected = web_sources[:web_limit]
    corpus_selected = corpus_sources[:corpus_limit]

    web_health, engine_errors = _web_health_from_runs(runs)
    sufficiency = next(
        (
            run.get("sufficiency")
            for run in reversed(runs)
            if isinstance(run.get("sufficiency"), dict)
        ),
        None,
    )
    web_modes = {
        str(_source_metadata(data).get("evidence_mode") or "unknown")
        for data in web_selected
    }
    evidence_mode = (
        "none"
        if not web_selected
        else next(iter(web_modes))
        if len(web_modes) == 1
        else "mixed"
    )
    obscura_rendered = any(
        bool(_source_metadata(data).get("js_rendered")) for data in web_selected
    )
    obscura_attempted = any(
        bool(_source_metadata(data).get("obscura_attempted")) for data in web_selected
    )
    obscura_skips = [
        str(_source_metadata(data).get("obscura_skipped_reason"))
        for data in web_selected
        if _source_metadata(data).get("obscura_skipped_reason")
    ]
    youtube_ok = sum(
        1
        for data in web_selected
        if _source_metadata(data).get("transcript_status") == "ok"
    )
    wikipedia_count = sum(
        1
        for data in web_selected
        if str(_source_metadata(data).get("search_provider") or "").lower()
        == "wikipedia"
    )
    if obscura_rendered:
        obscura_status = "rendered"
    elif obscura_attempted:
        obscura_status = "attempted_no_render"
    elif obscura_skips:
        obscura_status = f"skipped ({', '.join(dict.fromkeys(obscura_skips))})"
    else:
        obscura_status = "not_needed_or_no_allowlisted_failure"

    lines = [
        "[EVIDENCE PACKET]",
        f"Web health: {web_health}",
        f"Web sufficiency: {(sufficiency or {}).get('grade', 'not_assessed')}",
        f"Fetch depth: {options['fetch_depth']}",
        f"Research mode: {str(bool(options['research_mode'])).lower()}",
        f"Evidence mode: {evidence_mode}",
        f"Obscura: {obscura_status}",
        (
            "YouTube transcripts: "
            f"{'enabled' if options['youtube_transcripts'] else 'disabled'}"
            f"; successes={youtube_ok}"
        ),
        f"Wikipedia entity extracts: {wikipedia_count}",
        f"Corpus sources included: {len(corpus_selected)}",
        f"Web sources included: {len(web_selected)} of requested {web_limit}",
    ]
    if engine_errors:
        lines.append(f"Search engine issues: {'; '.join(engine_errors[:5])}")
    if sufficiency:
        lines.append(
            "Sufficiency reason: "
            f"{sufficiency.get('reason')} "
            f"(best={sufficiency.get('best_score')}, avg={sufficiency.get('avg_score')})"
        )
    if runs:
        query_lines = []
        for run in runs[-3:]:
            query = _clip_trace_value(run.get("query"), 140)
            result_count = run.get("result_count")
            query_lines.append(f"- {query} -> {result_count} result(s)")
        lines.append("Search attempts:\n" + "\n".join(query_lines))
    lines.append(
        "Use relevance first. Treat source type as metadata, not privilege. "
        "If web health is degraded or evidence is snippet-only, lower confidence "
        "for web-dependent claims and say what could not be verified."
    )

    if corpus_selected:
        lines.append("\n[Corpus Evidence]")
        for idx, data in enumerate(corpus_selected, start=1):
            metadata = _source_metadata(data)
            label = _source_title(data)
            score = data.get("score")
            kind = data.get("source_tier") or metadata.get("chunk_kind") or "corpus"
            excerpt = _source_excerpt(data, max_chars=700)
            lines.append(
                f"{idx}. {label} | kind={kind} | score={score}\n"
                f"   {excerpt or '(no excerpt)'}"
            )

    if web_selected:
        lines.append("\n[Web Evidence]")
        for idx, data in enumerate(web_selected, start=1):
            metadata = _source_metadata(data)
            label = _source_title(data)
            url = str(metadata.get("url") or data.get("doc_id") or "").strip()
            method = metadata.get("fetch_method") or "snippet"
            mode = metadata.get("evidence_mode") or "unknown"
            source_type = metadata.get("source_type") or "webpage"
            transcript = metadata.get("transcript_status")
            provider = metadata.get("search_provider") or metadata.get("source")
            score = metadata.get("evidence_score") or {}
            final_score = (
                score.get("final")
                if isinstance(score, dict)
                else None
            )
            excerpt = _source_excerpt(data, max_chars=1100)
            lines.append(
                f"{idx}. {label} | {url} | provider={provider or 'unknown'} "
                f"| type={source_type} | mode={mode} | fetch={method}"
                f"{f' | transcript={transcript}' if transcript else ''}"
                f"{f' | evidence_score={final_score}' if final_score is not None else ''}\n"
                f"   {excerpt or '(no excerpt)'}"
            )

    return "\n".join(lines).strip()


def _source_identity_key(source: Any) -> str | None:
    """Stable key for exact source-card dedupe.

    This intentionally does not collapse every chunk from the same document:
    two different sections can both be useful evidence. It does remove the
    same chunk/source card when it enters through multiple retrieval lanes.
    """
    data = _source_to_dict(source)
    if not data:
        return None
    if _is_web_source_data(data):
        web_key = _web_source_key(data)
        return f"web:{web_key}" if web_key else None
    chunk_id = str(data.get("chunk_id") or "").strip()
    if chunk_id:
        return f"chunk:{chunk_id}"
    parent_id = str(data.get("parent_id") or "").strip()
    doc_id = str(data.get("doc_id") or "").strip()
    if parent_id or doc_id:
        return f"parent:{doc_id}:{parent_id}"
    text = " ".join(str(data.get("text") or "").split())[:240]
    return f"text:{text}" if text else None


def _source_exact_text_key(source: Any) -> str | None:
    """Deduplicate same-document chunks that hydrate to identical text."""
    data = _source_to_dict(source)
    if not data or _is_web_source_data(data):
        return None
    text = " ".join(str(data.get("text") or "").split())
    if len(text) < 80:
        return None
    corpus_id = str(data.get("corpus_id") or "").strip()
    doc_id = str(data.get("doc_id") or "").strip()
    return f"text:{corpus_id}:{doc_id}:{len(text)}:{text[:512]}"


def _dedupe_sources_for_context(sources: list[Any] | None) -> list[Any]:
    """Preserve order while removing exact duplicate source cards."""
    if not sources:
        return []
    deduped: list[Any] = []
    seen: set[str] = set()
    seen_exact_text: set[str] = set()
    duplicates = 0
    for source in sources:
        key = _source_identity_key(source)
        if key and key in seen:
            duplicates += 1
            continue
        text_key = _source_exact_text_key(source)
        if text_key and text_key in seen_exact_text:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        if text_key:
            seen_exact_text.add(text_key)
        deduped.append(source)
    if duplicates:
        logger.info("source dedupe removed %d duplicate source card(s)", duplicates)
    return deduped


def _query_allows_reference_evidence(query: str) -> bool:
    return bool(_REFERENCE_QUERY_RE.search(str(query or "")))


def _clean_chat_source_text(source: SourceChunk) -> SourceChunk:
    """Remove source frontmatter/metadata noise before the LLM sees a chunk."""
    text = str(source.text or "")
    cleaned = _FRONTMATTER_RE.sub("", text, count=1).lstrip()
    if cleaned == text:
        return source
    data = source.model_dump()
    data["text"] = cleaned
    return SourceChunk(**data)


def _chat_source_is_low_value(source: SourceChunk, query: str) -> bool:
    """Detect citation/acknowledgement chunks that are poor answer evidence."""
    if _query_allows_reference_evidence(query):
        return False
    heading_text = " ".join(str(h) for h in (source.heading_path or []))
    text_head = str(source.text or "")[:1200]
    summary_head = str(source.summary or "")[:500]
    haystack = f"{heading_text}\n{summary_head}\n{text_head}"
    if _LOW_VALUE_EVIDENCE_RE.search(haystack):
        return True
    if re.search(r"\brelated work\b", heading_text, re.IGNORECASE):
        citation_hits = len(re.findall(r"\b[A-Z][A-Za-z-]+ et al\.\s*\(\d{4}", haystack))
        year_hits = len(re.findall(r"\(\d{4}[a-z]?\)", haystack))
        if citation_hits >= 3 or year_hits >= 5:
            return True
    return False


def _prepare_chat_evidence_sources(
    sources: list[SourceChunk],
    *,
    query: str,
    min_keep: int = _CHAT_EVIDENCE_MIN_KEEP_AFTER_FILTER,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Clean and lightly filter chunks before coverage/final prompt assembly."""
    cleaned = [_clean_chat_source_text(source) for source in (sources or [])]
    if not cleaned:
        return [], {"filtered_low_value": 0, "cleaned_frontmatter": 0}

    kept: list[SourceChunk] = []
    low_value: list[SourceChunk] = []
    for source in cleaned:
        if _chat_source_is_low_value(source, query):
            low_value.append(source)
        else:
            kept.append(source)

    if len(kept) < max(1, min_keep):
        needed = max(1, min_keep) - len(kept)
        kept.extend(low_value[:needed])
        low_value = low_value[needed:]

    cleaned_frontmatter = sum(
        1
        for before, after in zip(sources or [], cleaned)
        if str(before.text or "") != str(after.text or "")
    )
    return kept, {
        "filtered_low_value": len(low_value),
        "cleaned_frontmatter": cleaned_frontmatter,
    }


def _chat_coverage_norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _chat_coverage_facet_definitions() -> dict[str, dict[str, Any]]:
    return {str(facet.get("name") or ""): facet for facet in _CHAT_COVERAGE_FACETS}


def _chat_coverage_query_row(
    facet: dict[str, Any],
    *,
    matched: list[str],
    source: str,
    first_match_pos: int,
) -> dict[str, Any]:
    triggers = tuple(str(t).lower() for t in facet.get("triggers") or ())
    return {
        "name": str(facet.get("name") or ""),
        "label": str(facet.get("label") or facet.get("name") or ""),
        "matched": matched[:5],
        "query_explicit": True,
        "query_matched": True,
        "source": source,
        "first_match_pos": first_match_pos,
        "support_terms": [str(t) for t in (facet.get("support_terms") or []) if t],
        "triggers": [str(t) for t in triggers if t],
    }


def _chat_compound_query_facets(query: str) -> list[dict[str, Any]]:
    """Promote user-written compound ideas into explicit retrieval lanes.

    Dynamic corpus facets are great at finding what exists in the corpus. This
    layer handles the other side of the contract: phrases the user explicitly
    asked for, especially multi-word concepts such as "privacy-preserving
    on-device AI" that should not be demoted to optional dynamic coverage.
    """

    query_norm = _chat_coverage_norm(query)
    if not query_norm:
        return []
    definitions = _chat_coverage_facet_definitions()
    rows: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for phrase, facet_names in _CHAT_COMPOUND_QUERY_FACET_ALIASES:
        phrase_norm = _chat_coverage_norm(phrase)
        if not phrase_norm or phrase_norm not in query_norm:
            continue
        first_match_pos = query_norm.find(phrase_norm)
        for facet_name in facet_names:
            facet = definitions.get(facet_name)
            if not facet:
                continue
            key = (facet_name, phrase_norm)
            if key in seen_pairs:
                continue
            seen_pairs.add(key)
            rows.append(
                _chat_coverage_query_row(
                    facet,
                    matched=[phrase],
                    source="compound_query_phrase",
                    first_match_pos=first_match_pos if first_match_pos >= 0 else 999999,
                )
            )
    return rows


def _chat_coverage_facets_for_query(query: str) -> list[dict[str, Any]]:
    haystack = str(query or "").lower()
    literal_facets: list[dict[str, Any]] = []
    for facet in _CHAT_COVERAGE_FACETS:
        triggers = tuple(str(t).lower() for t in facet.get("triggers") or ())
        matched = [term for term in triggers if term and term in haystack]
        if not matched:
            continue
        literal_facets.append(
            _chat_coverage_query_row(
                facet,
                matched=matched,
                source="query_deconstruction",
                first_match_pos=min(
                    [haystack.find(term) for term in matched if haystack.find(term) >= 0]
                    or [999999]
                ),
            )
        )
    return _merge_chat_coverage_facets(
        _chat_compound_query_facets(query),
        literal_facets,
    )


def _is_weak_ingest_profile_lane(facet: dict[str, Any]) -> bool:
    """True when a long stored document facet only weakly overlaps the query."""

    if str(facet.get("source") or "") != "ingest_facet_profile":
        return False
    name_tokens = [
        token for token in str(facet.get("name") or "").split("_") if token
    ]
    matched = [str(item) for item in (facet.get("matched") or []) if str(item)]
    try:
        match_score = float(facet.get("match_score") or 0.0)
    except (TypeError, ValueError):
        match_score = 0.0
    return len(name_tokens) >= 5 and len(matched) <= 2 and match_score < 8.0


def _merge_chat_coverage_facets(
    base: list[dict[str, Any]],
    dynamic: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for row in [*base, *dynamic]:
        name = str(row.get("name") or "")
        if not name:
            continue
        existing = merged.get(name)
        if existing is None:
            merged[name] = dict(row)
            continue
        existing["matched"] = list(
            dict.fromkeys([*(existing.get("matched") or []), *(row.get("matched") or [])])
        )[:8]
        existing["support_terms"] = list(
            dict.fromkeys(
                [*(existing.get("support_terms") or []), *(row.get("support_terms") or [])]
            )
        )[:12]
        existing["triggers"] = list(
            dict.fromkeys([*(existing.get("triggers") or []), *(row.get("triggers") or [])])
        )[:12]
        existing["source"] = existing.get("source") or row.get("source")
        existing["query_matched"] = bool(
            existing.get("query_matched") or row.get("query_matched")
        )
        existing["query_explicit"] = bool(
            existing.get("query_explicit") or row.get("query_explicit")
        )
        existing["semantic_matched"] = bool(
            existing.get("semantic_matched") or row.get("semantic_matched")
        )
        existing["match_score"] = max(
            float(existing.get("match_score") or 0.0),
            float(row.get("match_score") or 0.0),
        )
        if row.get("vector_score") is not None:
            existing["vector_score"] = max(
                float(existing.get("vector_score") or 0.0),
                float(row.get("vector_score") or 0.0),
            )
        if row.get("facet_doc_ids"):
            existing["facet_doc_ids"] = list(
                dict.fromkeys(
                    [*(existing.get("facet_doc_ids") or []), *(row.get("facet_doc_ids") or [])]
                )
            )[:8]
        if row.get("facet_docs"):
            existing["facet_docs"] = [
                *list(existing.get("facet_docs") or []),
                *[
                    doc
                    for doc in (row.get("facet_docs") or [])
                    if doc not in (existing.get("facet_docs") or [])
                ],
            ][:8]
        existing["first_match_pos"] = min(
            int(existing.get("first_match_pos") or 999999),
            int(row.get("first_match_pos") or 999999),
        )
    rows = list(merged.values())
    rows.sort(
        key=lambda item: (
            0 if item.get("query_explicit") else 1,
            int(item.get("first_match_pos") or 999999),
            0 if float(item.get("match_score") or 0.0) >= 4.0 else 1,
            -float(item.get("match_score") or 0.0),
            -len(item.get("matched") or []),
            str(item.get("name") or ""),
        )
    )
    return rows[:8]


async def _chat_coverage_facets_for_query_with_corpus(
    query: str,
    corpus_ids: list[str] | None,
) -> list[dict[str, Any]]:
    base = _chat_coverage_facets_for_query(query)
    db = conversation_service._db
    try:
        dynamic = await matching_ingest_facets(db, query, corpus_ids, limit=8)
    except Exception as exc:
        logger.debug("chat ingest facet match skipped: %s", exc)
        dynamic = []
    try:
        from services.embedder import embed_query
        from services.ingestion_service import ingestion_service

        qdrant = ingestion_service.qdrant_client
        if qdrant is None:
            from qdrant_client import AsyncQdrantClient

            qdrant = AsyncQdrantClient(
                url=settings.QDRANT_URL,
                timeout=settings.QDRANT_TIMEOUT_SECONDS,
            )
        query_vector = await embed_query(
            query,
            await retriever_orchestrator._embedding_config_for_query(corpus_ids),
        )
        vector_dynamic = await matching_vector_facets(
            db,
            qdrant,
            query,
            query_vector,
            corpus_ids,
            limit=8,
        )
    except Exception as exc:
        logger.debug("chat vector facet match skipped: %s", exc)
        vector_dynamic = []
    return _merge_chat_coverage_facets(base, [*dynamic, *vector_dynamic])


def _chat_coverage_facet_terms(facet: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for key in ("matched", "support_terms", "triggers"):
        for raw in facet.get(key) or []:
            term = _chat_coverage_norm(raw)
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    return terms


def _chat_source_text(source: SourceChunk) -> str:
    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    values: list[Any] = [
        source.doc_name,
        source.doc_id,
        source.source_tier,
        source.summary,
        source.text,
        " ".join(str(v) for v in (source.heading_path or [])),
        metadata.get("title"),
        metadata.get("filename"),
        metadata.get("section"),
        " ".join(metadata_facet_terms(metadata)),
    ]
    return _chat_coverage_norm(" ".join(str(v) for v in values if v))


def _chat_facet_coverage_score(source: SourceChunk, facet: dict[str, Any]) -> int:
    text = _chat_source_text(source)
    if not text:
        return 0
    score = 0
    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    support_facet = metadata.get("support_facet") if isinstance(metadata.get("support_facet"), dict) else {}
    if str(support_facet.get("name") or "") == str(facet.get("name") or ""):
        score += 8
    high_text = _chat_coverage_norm(
        " ".join(
            str(v)
            for v in (
                source.doc_name,
                source.doc_id,
                source.source_tier,
                " ".join(str(h) for h in (source.heading_path or [])),
            )
            if v
        )
    )
    # Fold ingestion-tagged content-facet terms (the alias bridge, e.g.
    # "rag" -> "retrieval augmented generation") into the scored text, so a
    # semantic body hit that lacks the literal facet keyword still counts toward
    # grounding instead of scoring 0 on body-only matches.
    summary_text = _chat_coverage_norm(
        " ".join([str(source.summary or ""), *metadata_facet_terms(metadata)])
    )
    body_text = _chat_coverage_norm(source.text or "")
    for term in _chat_coverage_facet_terms(facet):
        is_phrase = " " in term
        if term in high_text:
            score += 4 if is_phrase else 2
        if term in summary_text:
            score += 2 if is_phrase else 1
        if term in body_text:
            score += 1 if is_phrase else 0
    return score


def _chat_query_fit_score(source: SourceChunk, query: str, facet: dict[str, Any]) -> int:
    query_terms = [
        term
        for term in re.findall(r"[a-z0-9][a-z0-9\-]{3,}", str(query or "").lower())
        if term
        not in {
            "could",
            "would",
            "should",
            "with",
            "from",
            "that",
            "this",
            "they",
            "them",
            "someone",
            "combine",
            "helps",
            "help",
            "over",
            "time",
        }
    ]
    terms = list(dict.fromkeys([*query_terms, *_chat_coverage_facet_terms(facet)]))[:32]
    text = _chat_source_text(source)
    return sum(1 for term in terms if _chat_coverage_norm(term) in text)


def _chat_support_query_variants(
    facet: dict[str, Any],
    original_query: str,
) -> list[str]:
    """Build bounded support queries for a missing facet.

    The first query is the precise lane query. Later variants widen the net
    with triggers, matched query text, and salient user terms. This keeps the
    support lane deterministic and offline, while avoiding the previous one-
    shot failure mode where a missing lane could disappear silently.
    """

    support_terms = [str(t) for t in (facet.get("support_terms") or []) if t]
    matched = [str(t) for t in (facet.get("matched") or []) if t]
    triggers = [str(t) for t in (facet.get("triggers") or []) if t]
    label = str(facet.get("label") or facet.get("name") or "").strip()
    query_terms = [
        term
        for term in re.findall(r"[a-z0-9][a-z0-9\-]{4,}", str(original_query or "").lower())
        if term
        not in {
            "could",
            "would",
            "should",
            "their",
            "there",
            "where",
            "about",
            "after",
            "before",
            "through",
            "combine",
            "build",
            "helps",
            "someone",
            "personal",
            "reflection",
            "over",
            "time",
        }
    ]

    variants: list[list[str]] = [
        support_terms[:12] or matched[:8] or triggers[:8] or [label],
        [label, *matched[:4], *support_terms[:14], *triggers[:8]],
        [label, *support_terms[:8], *query_terms[:8]],
    ]
    queries: list[str] = []
    seen: set[str] = set()
    for parts in variants:
        text = " ".join(str(part).strip() for part in parts if str(part).strip())
        text = " ".join(text.split())
        key = text.lower()
        if text and key not in seen:
            queries.append(text)
            seen.add(key)
    return queries[:3]


def _chat_coverage_candidate_snapshot(
    chunk: SourceChunk,
    *,
    facet: dict[str, Any],
    original_query: str,
    reason: str,
) -> dict[str, Any]:
    cleaned = _clean_chat_source_text(chunk)
    return {
        "chunk_id": str(cleaned.chunk_id or ""),
        "doc_id": str(cleaned.doc_id or ""),
        "doc_name": str(cleaned.doc_name or ""),
        "score": float(cleaned.score or 0.0),
        "facet_score": _chat_facet_coverage_score(cleaned, facet),
        "query_fit": _chat_query_fit_score(cleaned, original_query, facet),
        "reason": reason,
    }


def _chat_coverage_scores(
    sources: list[SourceChunk],
    facets: list[dict[str, Any]],
) -> dict[str, int]:
    return {
        str(facet.get("name") or ""): max(
            (_chat_facet_coverage_score(source, facet) for source in sources),
            default=0,
        )
        for facet in facets
    }


def _chat_query_facet_breakdown(
    facets: list[dict[str, Any]],
    scores: dict[str, int],
) -> list[dict[str, Any]]:
    """Model- and UI-facing view of the user's query decomposition."""

    rows: list[dict[str, Any]] = []
    for facet in facets:
        name = str(facet.get("name") or "")
        if not name:
            continue
        score = int(scores.get(name, 0) or 0)
        rows.append(
            {
                "name": name,
                "label": str(facet.get("label") or name.replace("_", " ")),
                "matched": facet.get("matched") or [],
                "source": str(facet.get("source") or ""),
                "query_explicit": bool(facet.get("query_explicit")),
                "semantic_matched": bool(facet.get("semantic_matched")),
                "coverage_score": score,
                "coverage_status": (
                    "grounded" if score >= _CHAT_COVERAGE_THRESHOLD else "needs_support"
                ),
            }
        )
    return rows


def _choose_chat_coverage_candidate_with_report(
    candidates: list[SourceChunk],
    *,
    facet: dict[str, Any],
    original_query: str,
    existing_chunk_ids: set[str],
    existing_doc_ids: set[str],
) -> tuple[SourceChunk | None, dict[str, Any]]:
    best_new_doc: tuple[float, SourceChunk] | None = None
    best_any_doc: tuple[float, SourceChunk] | None = None
    best_lane_new_doc: tuple[float, SourceChunk] | None = None
    best_lane_any_doc: tuple[float, SourceChunk] | None = None
    best_weak_new_doc: tuple[float, SourceChunk] | None = None
    best_weak_any_doc: tuple[float, SourceChunk] | None = None
    rejected: dict[str, int] = {
        "low_value": 0,
        "duplicate": 0,
        "below_facet_floor": 0,
    }
    sampled: list[dict[str, Any]] = []
    substantive = [
        chunk
        for chunk in candidates
        if not _chat_source_is_low_value(_clean_chat_source_text(chunk), original_query)
    ]
    rejected["low_value"] = max(0, len(candidates or []) - len(substantive))
    pool = substantive or list(candidates or [])
    for chunk in pool:
        chunk = _clean_chat_source_text(chunk)
        chunk_id = str(chunk.chunk_id or "")
        if not chunk_id or chunk_id in existing_chunk_ids:
            rejected["duplicate"] += 1
            if len(sampled) < 4:
                sampled.append(
                    _chat_coverage_candidate_snapshot(
                        chunk,
                        facet=facet,
                        original_query=original_query,
                        reason="duplicate",
                    )
                )
            continue
        facet_score = _chat_facet_coverage_score(chunk, facet)
        query_fit = _chat_query_fit_score(chunk, original_query, facet)
        # Vector escape hatch: a chunk retrieved for a semantically-matched lane
        # (matching_vector_facets cosine activation, cosine floor 0.42) is
        # genuinely relevant even when the lexical facet_score is low. Promote it
        # to the grounded floor for SELECTION so a semantic body hit competes as
        # real support instead of being demoted purely for lacking the literal
        # keyword. (The chosen chunk's reported strength is still recomputed from
        # the raw lexical score below, so it stays honestly labeled.)
        if facet.get("semantic_matched") and facet_score < _CHAT_COVERAGE_THRESHOLD:
            facet_score = _CHAT_COVERAGE_THRESHOLD
        doc_id = str(chunk.doc_id or "")
        new_doc_bonus = 4.0 if doc_id and doc_id not in existing_doc_ids else 0.0
        # Reranker/retrieval score enters only as a bounded tiebreaker: cap its
        # contribution to [0, 1.6] so a single very high raw score (rerank scores
        # are not 0-1 normalized — observed values 0.7 .. 1400+) cannot drown out
        # facet_score (*10) and query_fit (*2), which are the grounding signals.
        bounded_score = min(max(float(chunk.score or 0.0), 0.0), 0.8) * 2.0
        final_score = (facet_score * 10.0) + (query_fit * 2.0) + bounded_score + new_doc_bonus
        if facet_score < _CHAT_COVERAGE_WEAK_THRESHOLD:
            # Layer-2 retention fix: the lexical facet_score scores 0 for body-only
            # semantic matches and for vector-probe lanes whose terms are the document
            # title. These candidates already passed retrieval/rerank for this lane, so
            # retain the best as weakest-tier support instead of hard-dropping — a lane
            # with real candidates is never falsely reported "uncovered". Above-floor
            # candidates still take strict priority via the chosen_tuple ordering below.
            rejected["below_facet_floor"] += 1
            if len(sampled) < 4:
                sampled.append(
                    _chat_coverage_candidate_snapshot(
                        chunk,
                        facet=facet,
                        original_query=original_query,
                        reason="below_facet_floor",
                    )
                )
            current = (final_score, chunk)
            if best_weak_any_doc is None or final_score > best_weak_any_doc[0]:
                best_weak_any_doc = current
            if doc_id and doc_id not in existing_doc_ids:
                if best_weak_new_doc is None or final_score > best_weak_new_doc[0]:
                    best_weak_new_doc = current
            continue
        if facet_score >= _CHAT_COVERAGE_THRESHOLD and query_fit > 0:
            current = (final_score, chunk)
            if best_any_doc is None or final_score > best_any_doc[0]:
                best_any_doc = current
            if doc_id and doc_id not in existing_doc_ids:
                if best_new_doc is None or final_score > best_new_doc[0]:
                    best_new_doc = current
        elif facet_score >= _CHAT_COVERAGE_THRESHOLD:
            # Lane fallback: the support query already targeted this missing
            # facet. If the chunk cleanly covers the lane but not much of the
            # full multi-part query, keep it as partial evidence instead of
            # letting global relevance erase the facet.
            current = (final_score, chunk)
            if best_lane_any_doc is None or final_score > best_lane_any_doc[0]:
                best_lane_any_doc = current
            if doc_id and doc_id not in existing_doc_ids:
                if best_lane_new_doc is None or final_score > best_lane_new_doc[0]:
                    best_lane_new_doc = current
        else:
            # Weak fallback: enough lane signal to be useful, but not enough to
            # pretend the lane is fully grounded. The chunk can enter the
            # packet with metadata that tells the model and UI it is weak
            # support.
            current = (final_score, chunk)
            if best_weak_any_doc is None or final_score > best_weak_any_doc[0]:
                best_weak_any_doc = current
            if doc_id and doc_id not in existing_doc_ids:
                if best_weak_new_doc is None or final_score > best_weak_new_doc[0]:
                    best_weak_new_doc = current

    chosen_tuple = (
        best_new_doc
        or best_any_doc
        or best_lane_new_doc
        or best_lane_any_doc
        or best_weak_new_doc
        or best_weak_any_doc
        or (None, None)
    )
    chosen = chosen_tuple[1]
    if chosen is None:
        return None, {
            "status": "uncovered",
            "candidate_count": len(candidates or []),
            "substantive_count": len(substantive),
            "rejected": rejected,
            "sampled_rejections": sampled,
            "reason": "no_candidate_passed_lane_floor",
        }

    facet_score = _chat_facet_coverage_score(chosen, facet)
    strength = "strong" if facet_score >= _CHAT_COVERAGE_THRESHOLD else "weak"
    return chosen, {
        "status": "selected",
        "strength": strength,
        "candidate_count": len(candidates or []),
        "substantive_count": len(substantive),
        "rejected": rejected,
        "selected": _chat_coverage_candidate_snapshot(
            chosen,
            facet=facet,
            original_query=original_query,
            reason=f"{strength}_support",
        ),
    }


def _choose_chat_coverage_candidate(
    candidates: list[SourceChunk],
    *,
    facet: dict[str, Any],
    original_query: str,
    existing_chunk_ids: set[str],
    existing_doc_ids: set[str],
) -> SourceChunk | None:
    chosen, _ = _choose_chat_coverage_candidate_with_report(
        candidates,
        facet=facet,
        original_query=original_query,
        existing_chunk_ids=existing_chunk_ids,
        existing_doc_ids=existing_doc_ids,
    )
    return chosen


def _mark_chat_coverage_chunk(
    chunk: SourceChunk,
    *,
    facet: dict[str, Any],
    support_query: str,
    original_query: str,
    support_strength: str = "strong",
) -> SourceChunk:
    chunk = _clean_chat_source_text(chunk)
    data = chunk.model_dump()
    metadata = dict(data.get("metadata") or {})
    support_query_score = float(data.get("score") or 0.0)
    facet_score = _chat_facet_coverage_score(chunk, facet)
    query_fit = _chat_query_fit_score(chunk, original_query, facet)
    selection_score = min(
        0.95,
        (min(facet_score, 16) / 16.0 * 0.55)
        + (min(query_fit, 12) / 12.0 * 0.35)
        + (max(0.0, min(support_query_score, 1.0)) * 0.10),
    )
    data["score"] = round(selection_score, 6)
    metadata["support_role"] = "chat_semantic_facet_coverage"
    metadata["support_lane"] = f"facet:{facet.get('name') or ''}"
    metadata["support_query"] = _clip_trace_value(support_query, 220)
    metadata["support_query_score"] = support_query_score
    metadata["support_selection_score"] = round(selection_score, 6)
    metadata["support_facet_score"] = facet_score
    metadata["support_query_fit"] = query_fit
    metadata["support_strength"] = support_strength
    metadata["support_facet"] = {
        "name": str(facet.get("name") or ""),
        "label": str(facet.get("label") or ""),
        "matched": facet.get("matched") or [],
    }
    data["metadata"] = metadata
    return SourceChunk(**data)


def _chat_source_candidate_lanes(
    source: SourceChunk,
    facets: list[dict[str, Any]],
) -> set[str]:
    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    lanes: set[str] = set()

    support_lane = str(metadata.get("support_lane") or "")
    if support_lane.startswith("facet:"):
        lanes.add(support_lane.split(":", 1)[1])
    support_facet = (
        metadata.get("support_facet")
        if isinstance(metadata.get("support_facet"), dict)
        else {}
    )
    if support_facet.get("name"):
        lanes.add(str(support_facet.get("name")))

    semantic = (
        metadata.get("semantic_facets")
        if isinstance(metadata.get("semantic_facets"), dict)
        else {}
    )
    raw_ids: list[Any] = []
    raw_ids.extend(semantic.get("facet_ids") or [])
    raw_ids.extend(semantic.get("doc_facet_ids") or [])
    raw_ids.extend(semantic.get("content_facet_ids") or [])
    raw_ids.extend([semantic.get("content_facet_text") or ""])
    raw_ids.extend(metadata.get("facet_ids") or [])
    raw_ids.extend(metadata.get("doc_facet_ids") or [])
    raw_ids.extend(metadata.get("content_facet_ids") or [])
    raw_ids.extend([metadata.get("content_facet_text") or ""])
    raw_ids.append(source.doc_name or "")
    raw_ids.append(source.doc_id or "")
    normalized_ids = {normalize_facet_id(value) for value in raw_ids if value}

    for facet in facets:
        name = str(facet.get("name") or "")
        if not name:
            continue
        name_norm = normalize_facet_id(name)
        if name_norm and name_norm in normalized_ids:
            lanes.add(name)
            continue
        # Fallback for hand-authored/static facets whose lane names do not
        # exist as ingest-time facet ids. This lets the final selector see that
        # a chunk materially covers the facet even if it came from normal
        # retrieval rather than a support lane.
        if _chat_facet_coverage_score(source, facet) >= _CHAT_COVERAGE_THRESHOLD:
            lanes.add(name)

    return lanes


def _chat_selector_candidates(
    sources: list[SourceChunk],
    *,
    facets: list[dict[str, Any]],
    original_query: str,
) -> list[FacetCandidate]:
    candidates: list[FacetCandidate] = []
    for order, source in enumerate(sources or []):
        cleaned = _clean_chat_source_text(source)
        chunk_id = str(cleaned.chunk_id or "")
        key = f"chunk:{chunk_id}" if chunk_id else ""
        candidates.append(
            FacetCandidate(
                item=cleaned,
                score=float(cleaned.score or 0.0),
                lanes=_chat_source_candidate_lanes(cleaned, facets),
                key=key,
                doc_id=str(cleaned.doc_id or ""),
                domain=str(getattr(cleaned, "domain", "") or ""),
                junk=_chat_source_is_low_value(cleaned, original_query),
                order=order,
            )
        )
    return candidates


def _merge_chat_coverage_sources(
    base_sources: list[SourceChunk],
    support_sources: list[SourceChunk],
    *,
    max_sources: int,
) -> tuple[list[SourceChunk], int]:
    return _select_chat_coverage_sources(
        base_sources,
        support_sources,
        facets=[],
        missing_lanes=[],
        priority_lanes=[],
        original_query="",
        max_sources=max_sources,
    )[:2]


def _select_chat_coverage_sources(
    base_sources: list[SourceChunk],
    support_sources: list[SourceChunk],
    *,
    facets: list[dict[str, Any]],
    missing_lanes: list[str],
    priority_lanes: list[str] | None = None,
    original_query: str,
    max_sources: int,
    source_cap: int | None = None,
    max_per_domain: int | None = None,
) -> tuple[list[SourceChunk], int, dict[str, Any]]:
    max_sources = max(1, int(max_sources or len(base_sources) or 1))
    all_sources = [*base_sources, *support_sources]
    candidates = _chat_selector_candidates(
        all_sources,
        facets=facets,
        original_query=original_query,
    )
    selected, selector_meta = select_facet_final(
        candidates,
        missing_lanes=missing_lanes,
        priority_lanes=priority_lanes or [],
        max_items=max_sources,
        lane_budget=1,
        source_cap=source_cap or _CHAT_COVERAGE_SOURCE_CAP,
        max_per_domain=max_per_domain,
    )
    actual_support = [
        source
        for source in selected
        if isinstance(getattr(source, "metadata", None), dict)
        and source.metadata.get("support_role") == "chat_semantic_facet_coverage"
    ]
    return selected, len(actual_support), selector_meta


def _format_chat_coverage_prompt_note(meta: dict[str, Any]) -> str | None:
    """Internal RAG guardrail for the final model call.

    This note is deliberately *not* a user-facing report. It keeps the chat
    answer honest about weak/missing evidence without priming the model to
    narrate retrieval lanes the way Graph Query does.
    """

    selected = [str(name) for name in (meta.get("selected_facets") or []) if name]
    breakdown = (
        meta.get("query_facet_breakdown")
        if isinstance(meta.get("query_facet_breakdown"), list)
        else []
    )
    explicit = [
        row
        for row in breakdown
        if isinstance(row, dict) and row.get("query_explicit")
    ]
    if not selected and not explicit:
        return None
    lane_counts = meta.get("coverage_lane_counts") if isinstance(meta.get("coverage_lane_counts"), dict) else {}
    uncovered = [str(name) for name in (meta.get("coverage_uncovered_lanes") or []) if name]
    reports = meta.get("lane_reports") if isinstance(meta.get("lane_reports"), list) else []
    weak = [
        str(report.get("lane") or "")
        for report in reports
        if report.get("status") == "selected" and report.get("strength") == "weak"
    ]
    covered = [
        name
        for name in selected
        if int(lane_counts.get(name, 0) or 0) > 0 and name not in uncovered
    ]
    lines = ["Internal RAG evidence guardrail (do not mention this block):"]
    if explicit:
        facet_parts = []
        for row in explicit[:8]:
            name = str(row.get("name") or "")
            status = str(row.get("coverage_status") or "")
            if name:
                facet_parts.append(f"{name}={status}")
        if facet_parts:
            lines.append(
                "- The user question required these evidence areas: "
                f"{', '.join(facet_parts)}."
            )
    if covered:
        lines.append(f"- Source-backed areas: {', '.join(covered[:8])}.")
    if weak:
        lines.append(
            "- Weakly source-backed areas: "
            f"{', '.join(dict.fromkeys(weak))}. Treat these as partial evidence."
        )
    if uncovered:
        uncovered_text = ", ".join(uncovered[:8])
        lines.append(f"- Not source-backed in this retrieval packet: {uncovered_text}.")
        lines.append(
            "- HARD LIMIT: the retrieved chunks have no source-backed evidence for "
            f"these areas: {uncovered_text}."
        )
        lines.append(
            "- Do not state these areas as existing capabilities, proven "
            "mechanisms, established design facts, or source-backed conclusions."
        )
        lines.append(
            "- If one of these areas matters to the answer, handle it briefly at "
            "the exact point where the caveat matters. Do not open with a corpus "
            "audit or a list of covered/uncovered areas."
        )
    lines.append(
        "- Chat RAG answer rule: answer the user's question directly. Use "
        "retrieved evidence for source-backed claims. If retrieved evidence "
        "directly answers the question, the answer must be a synthesis of that "
        "evidence, not a generic pretrained definition. Use general knowledge "
        "only for small bridges the sources do not cover, and caveat material "
        "unsupported parts. Do not introduce named libraries, frameworks, "
        "products, papers, metrics, or examples unless they appear in the "
        "retrieved evidence or the user explicitly asked for outside knowledge. "
        "For corpus-specific claims, do not guess. Do not "
        "expose internal terms like facets, lanes, coverage contract, packet, "
        "retrieval tier, graph query, or chunks unless the user explicitly asks "
        "about retrieval diagnostics."
    )
    lines.append(
        "- For unsupported requested ideas, use cautious wording such as "
        "'the retrieved sources do not establish this part' or 'this would need "
        "additional evidence' only where necessary."
    )
    return "\n".join(lines)


async def _enforce_chat_query_coverage(
    *,
    original_query: str,
    retrieval_query: str,
    sources: list[SourceChunk],
    corpus_ids: list[str] | None,
    retrieval_tier: RetrievalTier,
    collections: list[str] | None,
    retrieval_k: int | None,
    rerank_enabled: bool,
    top_k_summary: int | None,
    rerank_top_n: int | None,
    similarity_threshold: float | None,
    neo4j_expansion_cap: int | None,
    max_corpora_per_query: int | None,
    fact_seed_limit: int | None,
    final_top_k: int | None,
    source_cap: int | None = None,
    search_mode: str,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Add missing query-facet evidence using the same chat retrieval tier.

    This is query satisfaction, not source diversity for its own sake. Normal
    retrieval runs first. Only query-stated facets with weak coverage get a
    small support retrieval, and selected chunks must still fit the original
    query/facet before they enter the final prompt.
    """

    base_sources = list(sources or [])
    facets = await _chat_coverage_facets_for_query_with_corpus(
        original_query,
        corpus_ids,
    )
    meta: dict[str, Any] = {
        "detected_facets": [],
        "selected_facets": [],
        "added": 0,
        "support_doc_ids": [],
        "support_lanes": [],
        "lane_reports": [],
        "effective_tier": str(getattr(retrieval_tier, "value", retrieval_tier)),
    }

    base_sources, evidence_meta = _prepare_chat_evidence_sources(
        base_sources,
        query=original_query,
    )
    meta.update(evidence_meta)
    if not base_sources:
        return [], meta
    if not facets:
        return base_sources, meta

    scores = _chat_coverage_scores(base_sources, facets)
    query_explicit_missing: list[dict[str, Any]] = []
    inferred_missing: list[dict[str, Any]] = []
    meta["detected_facets"] = [
        {
            "name": str(facet.get("name") or ""),
            "label": str(facet.get("label") or ""),
            "matched": facet.get("matched") or [],
            "source": str(facet.get("source") or ""),
            "query_explicit": bool(facet.get("query_explicit")),
            "semantic_matched": bool(facet.get("semantic_matched")),
            "coverage_score": scores.get(str(facet.get("name") or ""), 0),
        }
        for facet in facets
    ]
    meta["query_facet_breakdown"] = _chat_query_facet_breakdown(facets, scores)
    explicit_priority_lanes = [
        str(facet.get("name") or "")
        for facet in facets
        if facet.get("query_explicit") and str(facet.get("name") or "")
    ]
    meta["priority_lanes"] = explicit_priority_lanes
    for facet in facets:
        if scores.get(str(facet.get("name") or ""), 0) >= _CHAT_COVERAGE_THRESHOLD:
            continue
        if facet.get("query_explicit") and not _is_weak_ingest_profile_lane(facet):
            query_explicit_missing.append(facet)
        else:
            inferred_missing.append(facet)
    # Vector-facet probes are breadth hints. In local/specific chat they should
    # not trigger extra support retrievals; otherwise a simple definition
    # question can fan out into several book-title searches before answering.
    dynamic_missing = (
        inferred_missing[:_CHAT_COVERAGE_MAX_DYNAMIC_SUPPLEMENTS]
        if str(search_mode or "").lower() == "global"
        else []
    )
    skipped_dynamic = (
        inferred_missing[_CHAT_COVERAGE_MAX_DYNAMIC_SUPPLEMENTS:]
        if str(search_mode or "").lower() == "global"
        else inferred_missing
    )
    missing = [*query_explicit_missing, *dynamic_missing]
    if not missing:
        return base_sources, meta
    meta["selected_facets"] = [str(facet.get("name") or "") for facet in missing]
    meta["explicit_missing_facets"] = [
        str(facet.get("name") or "") for facet in query_explicit_missing
    ]
    meta["dynamic_missing_facets"] = [
        str(facet.get("name") or "") for facet in dynamic_missing
    ]
    meta["skipped_dynamic_facets"] = [
        str(facet.get("name") or "") for facet in skipped_dynamic
    ]
    meta["support_search_mode"] = "local"

    existing_chunk_ids = {str(source.chunk_id or "") for source in base_sources}
    existing_doc_ids = {str(source.doc_id or "") for source in base_sources if source.doc_id}
    support_sources: list[SourceChunk] = []

    for facet in missing:
        facet_name = str(facet.get("name") or "")
        lane_report: dict[str, Any] = {
            "lane": facet_name,
            "label": str(facet.get("label") or facet_name),
            "query_explicit": bool(facet.get("query_explicit")),
            "source": str(facet.get("source") or ""),
            "coverage_score": scores.get(facet_name, 0),
            "status": "uncovered",
            "attempts": [],
        }
        chosen: SourceChunk | None = None
        chosen_report: dict[str, Any] = {}
        support_query = ""
        for support_query in _chat_support_query_variants(facet, original_query):
            attempt: dict[str, Any] = {
                "query": _clip_trace_value(support_query, 220),
                "search_mode": "local",
                "returned": 0,
                "status": "started",
            }
            try:
                result = await retriever_orchestrator.retrieve(
                    query=support_query,
                    corpus_ids=corpus_ids,
                    retrieval_tier=retrieval_tier,
                    collections=collections,
                    retrieval_k=max(24, min(int(retrieval_k or 40), 48)),
                    rerank_enabled=rerank_enabled,
                    ranking_query=support_query,
                    top_k_summary=top_k_summary,
                    rerank_top_n=max(12, min(int(rerank_top_n or 24), 32)),
                    similarity_threshold=similarity_threshold,
                    neo4j_expansion_cap=neo4j_expansion_cap,
                    max_corpora_per_query=max_corpora_per_query,
                    final_top_k=6,
                    fact_seed_limit=fact_seed_limit,
                    search_mode="local",
                )
            except Exception as exc:
                attempt["status"] = "retrieval_error"
                attempt["error"] = _clip_trace_value(exc, 180)
                lane_report["attempts"].append(attempt)
                logger.debug("chat coverage support retrieval skipped for %s: %s", facet_name, exc)
                continue
            candidates = list(getattr(result, "chunks", []) or [])
            attempt["returned"] = len(candidates)
            chosen, chosen_report = _choose_chat_coverage_candidate_with_report(
                candidates,
                facet=facet,
                original_query=original_query,
                existing_chunk_ids=existing_chunk_ids,
                existing_doc_ids=existing_doc_ids,
            )
            attempt.update(
                {
                    "status": chosen_report.get("status", "uncovered"),
                    "strength": chosen_report.get("strength"),
                    "selected": chosen_report.get("selected"),
                    "rejected": chosen_report.get("rejected"),
                    "sampled_rejections": chosen_report.get("sampled_rejections"),
                    "reason": chosen_report.get("reason"),
                }
            )
            lane_report["attempts"].append(attempt)
            if chosen:
                break

        if not chosen:
            lane_report["reason"] = "no_support_chunk_selected_after_fallbacks"
            meta["lane_reports"].append(lane_report)
            continue

        strength = str(chosen_report.get("strength") or "strong")
        marked = _mark_chat_coverage_chunk(
            chosen,
            facet=facet,
            support_query=support_query,
            original_query=original_query,
            support_strength=strength,
        )
        lane_report["status"] = "selected"
        lane_report["strength"] = strength
        lane_report["selected_doc_id"] = str(marked.doc_id or "")
        lane_report["selected_doc_name"] = str(marked.doc_name or "")
        lane_report["selected_chunk_id"] = str(marked.chunk_id or "")
        support_sources.append(marked)
        meta["lane_reports"].append(lane_report)
        existing_chunk_ids.add(str(marked.chunk_id or ""))
        if marked.doc_id:
            existing_doc_ids.add(str(marked.doc_id))

    # final_top_k is the baseline chunk budget; when source_cap is raised above
    # it, expand the budget so the extra distinct documents can actually appear
    # (otherwise increasing source_cap would be clamped by final_top_k and have
    # no effect). source_cap then caps distinct docs within this budget.
    _base_budget = int(final_top_k or len(base_sources) or 8)
    max_sources = max(_base_budget, int(source_cap or 0))
    # Overview/global queries span more of the pool than focused queries: widen both
    # the chunk budget and the distinct-doc cap so the final packet uses more docs
    # (still ≤ _CHAT_COVERAGE_DOMAIN_CAP per domain). Local mode is unaffected.
    if str(search_mode or "").lower() == "global":
        max_sources = max(max_sources, _GLOBAL_OVERVIEW_BUDGET)
        source_cap = max(int(source_cap or 0), _GLOBAL_OVERVIEW_BUDGET)
    merged, added, selector_meta = _select_chat_coverage_sources(
        base_sources,
        support_sources,
        facets=facets,
        missing_lanes=meta["selected_facets"],
        priority_lanes=explicit_priority_lanes,
        original_query=original_query,
        max_sources=max_sources,
        source_cap=source_cap,
        max_per_domain=(
            _CHAT_COVERAGE_DOMAIN_CAP
            if str(search_mode or "").lower() == "global"
            else None
        ),
    )
    actual_support = [
        source
        for source in merged
        if isinstance(source.metadata, dict)
        and source.metadata.get("support_role") == "chat_semantic_facet_coverage"
    ]
    meta["added"] = len(actual_support)
    meta["support_doc_ids"] = [str(source.doc_id or "") for source in actual_support if source.doc_id]
    meta["support_lanes"] = [
        str(source.metadata.get("support_lane") or "")
        for source in actual_support
        if isinstance(source.metadata, dict)
    ]
    meta["final_chunks"] = len(merged)
    meta["final_selector"] = selector_meta
    meta["coverage_lane_counts"] = selector_meta.get("lane_counts", {})
    meta["coverage_uncovered_lanes"] = selector_meta.get("uncovered_lanes", [])
    meta["coverage_priority_lanes"] = selector_meta.get("priority_lanes", [])
    meta["coverage_uncovered_priority_lanes"] = selector_meta.get("uncovered_priority_lanes", [])
    for report in meta.get("lane_reports", []):
        lane = str(report.get("lane") or "")
        if lane in meta["coverage_uncovered_lanes"] and report.get("status") == "selected":
            report["status"] = "selected_but_not_in_final_packet"
    return merged, meta


def _clip_trace_value(value: Any, max_chars: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _format_web_retrieval_decision_trace(
    web_result: str,
) -> tuple[str, dict[str, Any]] | None:
    """Summarize observable web retrieval decisions for the UI trace lane.

    This is intentionally not hidden chain-of-thought. It exposes the bounded,
    deterministic decisions Polymath made while searching: snippet sufficiency,
    page-fetch choice, Obscura usage, reranking, and selected evidence.
    """
    try:
        payload = json.loads(web_result)
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    pipeline = payload.get("pipeline")
    if not isinstance(pipeline, dict):
        return None

    sufficiency = (
        pipeline.get("snippet_sufficiency")
        if isinstance(pipeline.get("snippet_sufficiency"), dict)
        else {}
    )
    fetches = pipeline.get("fetches") if isinstance(pipeline.get("fetches"), list) else []
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    selected_urls = (
        pipeline.get("selected_full_page_urls")
        if isinstance(pipeline.get("selected_full_page_urls"), list)
        else []
    )
    search_queries = (
        pipeline.get("search_queries")
        if isinstance(pipeline.get("search_queries"), list)
        else [payload.get("query")]
    )
    js_render = (
        pipeline.get("js_render") if isinstance(pipeline.get("js_render"), dict) else {}
    )
    sufficiency = (
        pipeline.get("evidence_sufficiency")
        if isinstance(pipeline.get("evidence_sufficiency"), dict)
        else {}
    )
    backend_retry = (
        pipeline.get("backend_retry")
        if isinstance(pipeline.get("backend_retry"), dict)
        else {}
    )

    method_counts: dict[str, int] = {}
    for item in fetches:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or item.get("status") or "unknown")
        method_counts[method] = method_counts.get(method, 0) + 1
    method_summary = ", ".join(
        f"{method}={count}" for method, count in sorted(method_counts.items())
    )

    top_sources: list[str] = []
    for item in results[:3]:
        if not isinstance(item, dict):
            continue
        title = _clip_trace_value(item.get("title"), 90)
        url = _clip_trace_value(item.get("url"), 120)
        fetch_method = item.get("fetch_method") or (
            "snippet" if not item.get("full_page_fetched") else "page"
        )
        top_sources.append(f"- {title} [{fetch_method}] {url}")

    snippet_only = bool(pipeline.get("snippet_only"))
    fetch_attempts = pipeline.get("full_page_fetch_attempts") or 0
    fetch_successes = pipeline.get("full_page_fetch_successes") or 0
    final_results = (
        pipeline.get("final_reranked_results")
        or payload.get("reranked_results")
        or 0
    )
    final_limit = pipeline.get("final_result_limit") or _MAX_WEB_SEARCH_RESULTS_PER_CALL

    content = (
        "[Web retrieval decision trace]\n"
        f"query: {_clip_trace_value(payload.get('query'), 260)}\n"
        f"search_queries: {_clip_trace_value('; '.join(str(q) for q in search_queries if q), 320)}\n"
        f"candidates: {pipeline.get('candidate_results') or payload.get('candidate_results') or 0} "
        f"of requested {pipeline.get('candidate_limit_requested') or 'unknown'}\n"
        f"snippet_decision: {'use_snippets_only' if snippet_only else 'fetch_pages_or_enrich_snippets'} "
        f"| score={pipeline.get('snippet_sufficiency_score', 'unknown')} "
        f"| reason={pipeline.get('snippet_sufficiency_reason') or pipeline.get('skipped_full_page_fetch_reason') or 'unknown'}\n"
        f"snippet_evidence: useful_chars={sufficiency.get('useful_snippet_chars', 'unknown')}, "
        f"top3_chars={sufficiency.get('top3_snippet_chars', 'unknown')}, "
        f"useful_count={sufficiency.get('useful_snippet_count', 'unknown')}, "
        f"domains={sufficiency.get('distinct_domains', 'unknown')}, "
        f"query_coverage={sufficiency.get('query_coverage', 'unknown')}, "
        f"stronger_evidence_required={str(bool(sufficiency.get('stronger_evidence_required'))).lower()}\n"
        f"page_fetch: attempts={fetch_attempts}, successes={fetch_successes}, "
        f"selected_urls={len(selected_urls)}, skipped_reason={pipeline.get('skipped_full_page_fetch_reason') or 'none'}\n"
        f"fetch_methods: {method_summary or 'none'}\n"
        f"obscura: configured={str(bool(js_render.get('configured'))).lower()}, "
        f"attempted={str(bool(js_render.get('attempted'))).lower()}, "
        f"rendered={str(bool(js_render.get('rendered'))).lower()}\n"
        f"web_sufficiency: grade={sufficiency.get('grade', 'unknown')}, "
        f"reason={sufficiency.get('reason', 'unknown')}, "
        f"best={sufficiency.get('best_score', 'unknown')}, "
        f"avg={sufficiency.get('avg_score', 'unknown')}\n"
        f"backend_retry: attempted={str(bool(backend_retry.get('attempted'))).lower()}, "
        f"selected_query={_clip_trace_value(backend_retry.get('selected_query'), 180)}\n"
        f"reranker: {pipeline.get('ranked_by') or payload.get('ranked_by') or 'unknown'} "
        f"selected={final_results}/{final_limit}\n"
        "top_selected_sources:\n"
        f"{chr(10).join(top_sources) if top_sources else '- none'}"
    )
    metadata = {
        "query": payload.get("query"),
        "candidate_results": pipeline.get("candidate_results")
        or payload.get("candidate_results"),
        "snippet_only": snippet_only,
        "snippet_sufficiency_score": pipeline.get("snippet_sufficiency_score"),
        "snippet_sufficiency_reason": pipeline.get("snippet_sufficiency_reason"),
        "full_page_fetch_attempts": fetch_attempts,
        "full_page_fetch_successes": fetch_successes,
        "obscura_attempted": bool(js_render.get("attempted")),
        "obscura_rendered": bool(js_render.get("rendered")),
        "ranked_by": pipeline.get("ranked_by") or payload.get("ranked_by"),
        "final_reranked_results": final_results,
        "final_result_limit": final_limit,
        "web_sufficiency": sufficiency.get("grade"),
        "backend_retry_attempted": bool(backend_retry.get("attempted")),
        "raw_chain_of_thought": False,
    }
    return content, metadata


def _format_model_api_trace(
    *,
    name: str,
    model: str | None,
    status: str,
    purpose: str,
    duration_s: float | None = None,
    detail: str | None = None,
) -> str:
    lines = [
        "[Model API call]",
        f"name: {name}",
        f"model: {model or 'resolved at runtime'}",
        f"status: {status}",
        f"purpose: {purpose}",
    ]
    if duration_s is not None:
        lines.append(f"duration_s: {duration_s:.2f}")
    if detail:
        lines.append(f"detail: {_clip_trace_value(detail, 320)}")
    return "\n".join(lines)


def _web_chunk_content_preview(chunk: Any, *, max_chars: int = 1600) -> str:
    text = str(getattr(chunk, "text", "") or "")
    marker = "\nContent: "
    if marker in text:
        text = text.split(marker, 1)[1]
    return text[:max_chars].strip()


def _tool_schema_name(schema: dict[str, Any]) -> str:
    fn = schema.get("function") if isinstance(schema, dict) else None
    return str((fn or {}).get("name") or "")


def _tool_call_name(call: dict[str, Any]) -> str:
    fn = call.get("function") if isinstance(call, dict) else None
    return str((fn or {}).get("name") or "")


def _web_search_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the live web. When the Web toggle is enabled, call this "
                "before giving the final answer, then inspect the returned "
                "snippets, fetched-page evidence, domains, and telemetry. If the "
                "evidence is not sufficient, call web_search again with a refined "
                "query or call fetch_page for a specific URL. Query rules: use "
                "keywords, names, exact phrases, model/version numbers, dates, "
                "and domains; do not write a natural-language question; omit "
                "filler such as what/who/tell me/find information; use 3-10 "
                "high-signal terms. Preserve the user's technical anchors and "
                "acronyms. Do not include local corpus names, file names, or "
                "internal project labels. Prefer official docs, vendor/developer "
                "blogs, framework docs, and production guides unless the user "
                "asks for papers. The server executes controlled SearXNG search, "
                "deterministic page fetching including Obscura fallback for "
                "niche JS-render cases, and local reranking within the user's "
                "configured source budget."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The web search query to run.",
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": _MAX_WEB_SEARCH_RESULTS_PER_CALL,
                        "description": "Maximum final reranked web results to return.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _fetch_page_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "Fetch one specific URL when search snippets are not enough. "
                "Use this for pages you need to inspect more deeply, especially "
                "JS-heavy pages where Obscura may be needed. The runtime decides "
                "deterministically whether raw HTTP, static extraction, yt-dlp, "
                "or Obscura is appropriate; the model only chooses the URL and "
                "why it is needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The http(s) URL to fetch.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Brief reason this full page is needed, such as "
                            "official docs, missing detail, JS-rendered page, "
                            "or source verification."
                        ),
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    }


def _response_tool_schema() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "response",
            "description": (
                "Finish the turn once you have enough RAG and/or web evidence. "
                "Call this only after required web searching is complete when "
                "the Web toggle is enabled. The text must be the complete "
                "user-facing answer, not JSON or tool syntax."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The final answer to show the user.",
                    },
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    }


def _extract_response_tool_text(
    tool_calls: list[dict[str, Any]],
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]]]:
    remaining: list[dict[str, Any]] = []
    response_call: dict[str, Any] | None = None
    response_text: str | None = None
    for call in tool_calls:
        if _tool_call_name(call) != "response":
            remaining.append(call)
            continue
        if response_call is not None:
            continue
        response_call = call
        try:
            args = json.loads((call.get("function") or {}).get("arguments") or "{}")
        except Exception:
            args = {}
        text = str(args.get("text") or "").strip()
        if text:
            response_text = text
    return response_text, response_call, remaining


def _looks_like_raw_tool_request_content(content: str) -> bool:
    """Detect tool-call syntax leaked as text without parsing/executing it."""
    text = (content or "").strip().lower()
    if not text:
        return False
    if any(marker in text for marker in _RAW_TOOL_REQUEST_MARKERS):
        return True
    return "web_search" in text and (
        "<" in text
        or "{" in text
        or "invoke" in text
        or "parameter" in text
    )


def _is_web_search_enabled_for_request(request: ChatRequest) -> bool:
    """True when this turn should expose the native web_search tool."""
    return bool(
        settings.LIVE_WEB_SEARCH_ENABLED
        and request.overrides
        and getattr(request.overrides, "web_search_enabled", None)
    )


def _available_tool_schemas(
    tool_schemas: list[dict[str, Any]],
    *,
    web_search_call_count: int,
    force_initial_web_search: bool = False,
) -> list[dict[str, Any]]:
    available = (
        tool_schemas
        if web_search_call_count < _MAX_WEB_SEARCH_CALLS_PER_TURN
        else [
            schema
            for schema in tool_schemas
            if _tool_schema_name(schema) != "web_search"
        ]
    )
    if force_initial_web_search:
        web_only = [
            schema
            for schema in available
            if _tool_schema_name(schema) == "web_search"
        ]
        return web_only or available
    return available


def _force_tool_choice(tool_name: str) -> dict[str, Any]:
    """OpenAI-compatible forced tool-choice shape."""
    return {"type": "function", "function": {"name": tool_name}}


def _tool_schemas_contain(
    tool_schemas: list[dict[str, Any]],
    tool_name: str,
) -> bool:
    return any(_tool_schema_name(schema) == tool_name for schema in tool_schemas)


def _tool_schema_names(tool_schemas: list[dict[str, Any]]) -> list[str]:
    return [
        name
        for schema in tool_schemas
        if (name := _tool_schema_name(schema))
    ]


def _partition_known_tool_calls(
    tool_calls: list[dict[str, Any]],
    active_tool_schemas: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Split tool calls into (recognized, dropped_names).

    A call is recognized only if it has a non-empty name matching an active tool
    schema or the always-valid 'response' finish tool. Empty/garbage names — for
    example minimax-m2.7's spurious ``{"name": ""}`` call emitted alongside a
    complete answer — are dropped here so they never trigger a not-found tool
    execution and an extra generation pass that duplicates the answer in the
    live stream.
    """
    valid_names = set(_tool_schema_names(active_tool_schemas))
    valid_names.add("response")
    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    for call in tool_calls:
        name = _tool_call_name(call)
        if name and name in valid_names:
            kept.append(call)
        else:
            dropped.append(name or "<empty>")
    return kept, dropped


def _limit_tool_calls_for_turn(
    tool_calls: list[dict[str, Any]],
    *,
    remaining_tool_calls: int,
    web_search_call_count: int,
) -> tuple[list[dict[str, Any]], int, bool, bool]:
    """Keep bounded web_search calls per turn while preserving other tools."""
    allowed: list[dict[str, Any]] = []
    selected_web_search_calls = 0
    dropped_for_tool_limit = False
    dropped_for_web_limit = False
    remaining_web_search_calls = max(
        0,
        _MAX_WEB_SEARCH_CALLS_PER_TURN - web_search_call_count,
    )

    for call in tool_calls:
        if len(allowed) >= remaining_tool_calls:
            dropped_for_tool_limit = True
            continue

        if _tool_call_name(call) == "web_search":
            if selected_web_search_calls >= remaining_web_search_calls:
                dropped_for_web_limit = True
                continue
            selected_web_search_calls += 1

        allowed.append(call)

    return (
        allowed,
        selected_web_search_calls,
        dropped_for_tool_limit,
        dropped_for_web_limit,
    )


def _compact_source_previews(sources: list[Any] | None) -> list[dict[str, Any]] | None:
    """Persist small source previews so reloaded chat messages keep citations.

    Full hydrated chunks can be large, especially with parent-document RAG. The
    frontend only needs enough text to make a reloaded RetrievalBadge useful.
    Web sources are retained deliberately instead of being clipped out by a
    full corpus chunk list.
    """
    if not sources:
        return None

    corpus_previews: list[dict[str, Any]] = []
    web_previews: list[dict[str, Any]] = []
    seen_web_keys: set[str] = set()

    for source in sources:
        data = _source_to_dict(source)
        if data is None:
            continue

        data["text"] = _clip_source_text(
            data.get("text"), _MAX_PERSISTED_SOURCE_TEXT_CHARS
        ) or ""
        if data.get("summary"):
            data["summary"] = _clip_source_text(
                data.get("summary"), _MAX_PERSISTED_SOURCE_SUMMARY_CHARS
            )
        if isinstance(data.get("provenance"), list):
            data["provenance"] = data["provenance"][:5]

        if _is_web_source_data(data):
            key = _web_source_key(data)
            if key and key in seen_web_keys:
                continue
            if key:
                seen_web_keys.add(key)
            if len(web_previews) < _MAX_PERSISTED_WEB_SOURCE_PREVIEWS:
                web_previews.append(data)
            continue

        if len(corpus_previews) < _MAX_PERSISTED_SOURCE_PREVIEWS:
            corpus_previews.append(data)

    web_slots = min(len(web_previews), _MAX_PERSISTED_SOURCE_PREVIEWS)
    corpus_slots = max(0, _MAX_PERSISTED_SOURCE_PREVIEWS - web_slots)
    previews = corpus_previews[:corpus_slots] + web_previews[:web_slots]

    return previews or None


def _is_graph_augmented_tier(tier: Any) -> bool:
    """True only for the Neo4j-backed Graph Augmented retrieval tier."""
    value = getattr(tier, "value", tier)
    return value == RetrievalTier.qdrant_mongo_graph.value


def _retrieval_tier_value(tier: Any) -> str:
    value = getattr(tier, "value", tier)
    return str(value or RetrievalTier.qdrant_mongo.value)


def _retrieval_tier_lens_name(tier: Any) -> str:
    value = _retrieval_tier_value(tier)
    if value == RetrievalTier.qdrant_only.value:
        return "Semantic overview"
    if value == RetrievalTier.qdrant_mongo_graph.value:
        return "Relationship map"
    return "Corpus synthesis"


def _format_retrieval_tier_synthesis_contract(
    tier: Any,
    diagnostics: dict[str, Any] | None = None,
) -> str:
    """Prompt contract that makes each retrieval tier synthesize differently."""

    value = _retrieval_tier_value(tier)
    lens = _retrieval_tier_lens_name(value)
    diag = diagnostics or {}
    counts = diag.get("counts") if isinstance(diag.get("counts"), dict) else {}
    final_mix = diag.get("final_source_tiers")
    if not isinstance(final_mix, dict):
        final_mix = {}

    header = [
        "<retrieval_synthesis_lens>",
        f"selected_lens: {lens}",
        (
            "This is an internal answer-shaping contract. Do not name this "
            "contract or the retrieval tier unless the user explicitly asks "
            "for retrieval diagnostics; express the lens through the structure "
            "and emphasis of the answer."
        ),
    ]
    if counts or final_mix:
        header.append(
            "retrieval_signal: "
            f"facts={counts.get('facts', 0)}; "
            f"graph_expanded={counts.get('graph_expanded', 0)}; "
            f"lexical={counts.get('lexical', 0)}; "
            f"final_mix={final_mix or 'n/a'}."
        )

    if value == RetrievalTier.qdrant_only.value:
        body = [
            "Required answer shape: semantic overview.",
            (
                "Answer the user's question from the strongest semantic matches. "
                "Favor a clean definition and broad conceptual framing."
            ),
            (
                "Ground the overview in the retrieved passages: build the answer "
                "from how THIS corpus frames the concept, and fold in at least "
                "one concrete term, phrasing, or example that actually appears "
                "in the retrieved context. Do not fall back to a purely generic "
                "textbook definition that ignores the retrieved evidence."
            ),
            (
                "Keep it a broad overview — do not perform source-by-source "
                "comparison, corpus-wide adjudication, or relationship/gap "
                "analysis. Keep the answer compact unless the user asks for "
                "depth. If the retrieved context genuinely does not address the "
                "term, give a short plain definition and say the corpus does not "
                "cover it — in one inline clause, not a separate section."
            ),
        ]
    elif value == RetrievalTier.qdrant_mongo_graph.value:
        body = [
            "Required answer shape: relationship map.",
            (
                "This lens overrides the default short-answer compression. Even "
                "for a simple definition query, the final answer must visibly "
                "organize the concept as relationships rather than as only a "
                "generic definition."
            ),
            (
                "Preferred shape: a short definition, then the concept's core "
                "relationships, then where the evidence is thin. Use plain, "
                "content-driven headings that fit the actual question — do not "
                "paste the fixed labels 'Core node', 'Connected ideas', or "
                "'Weak or missing links' verbatim as section headers."
            ),
            (
                "Define the core concept briefly, then explain how it connects "
                "to adjacent concepts, tools, methods, documents, entities, or "
                "tensions visible in the retrieved evidence."
            ),
            (
                "When graph facts or graph decoration are present, prioritize "
                "explicit relationships and bridge concepts. When no graph facts "
                "are present, still use the graph-expanded context as a network "
                "lens: distinguish core node, connected ideas, and missing or "
                "weak links without inventing unsupported edges."
            ),
            (
                "The answer should feel structurally different from Vector and "
                "Hybrid: relationship-first, not merely a definition or a list "
                "of source excerpts."
            ),
        ]
    else:
        body = [
            "Required answer shape: hydrated corpus synthesis.",
            (
                "This lens overrides the default short-answer compression. Do "
                "not stop at the same generic definition Vector Base would give."
            ),
            (
                "Lead with the corpus-grounded synthesis, then give 2-4 "
                "evidence-backed details drawn from the hydrated parent/lexical "
                "passages. Open naturally with the answer itself — do NOT use a "
                "fixed opener like 'Across the selected sources'."
            ),
            (
                "Answer as what the selected corpus evidence specifically says. "
                "Use hydrated parent/lexical evidence to add precision, terms, "
                "examples, and caveats beyond the broad semantic definition."
            ),
            (
                "Reconcile the strongest retrieved passages when they frame the "
                "concept differently, but avoid graph/network claims unless graph "
                "evidence is actually present."
            ),
            (
                "The answer should feel deeper than Vector Base: more grounded, "
                "more corpus-specific, more exact. Only note an evidence gap when "
                "it materially changes the answer, and phrase it as a brief "
                "inline caveat — never a standing 'what the corpus does not "
                "establish' section or a '→ the retrieved corpus does not "
                "establish ...' line."
            ),
            (
                "If the question is simple, the answer may still be concise, "
                "but it must include at least two concrete retrieved terms, "
                "phrases, examples, or distinctions that are not merely a "
                "generic textbook definition."
            ),
        ]

    return "\n".join([*header, *body, "</retrieval_synthesis_lens>"])


def _format_retrieval_tier_lens_trace(
    tier: Any,
    diagnostics: dict[str, Any] | None = None,
) -> str:
    value = _retrieval_tier_value(tier)
    lens = _retrieval_tier_lens_name(value)
    diag = diagnostics or {}
    counts = diag.get("counts") if isinstance(diag.get("counts"), dict) else {}
    if value == RetrievalTier.qdrant_only.value:
        contract = "semantic overview: broad vector evidence, concise definition."
    elif value == RetrievalTier.qdrant_mongo_graph.value:
        contract = (
            "relationship map: use graph-expanded context to surface connections, "
            "bridges, weak links, and concept neighborhoods."
        )
    else:
        contract = (
            "hydrated corpus synthesis: use Mongo lexical/parent context for "
            "more exact corpus-grounded detail."
        )
    return (
        f"{lens}\n"
        f"{contract}\n"
        f"signals: lexical={counts.get('lexical', 0)} · "
        f"facts={counts.get('facts', 0)} · "
        f"graph_expanded={counts.get('graph_expanded', 0)}"
    )


_RETRIEVAL_NUANCE_TOKEN_RE = re.compile(r"[a-z][a-z0-9+#-]{2,}")
_RETRIEVAL_NUANCE_STOPWORDS = frozenset(
    """
    about above across after again against all almost along already also although
    always among and another any are around because been before being below between
    both but can cannot could did does doing done down during each either else
    even ever every few for from further had has have having here how however
    into its itself just later less like likely made many may might more most
    much must neither nor not now often only other our out over own per same
    should since some still such than that the their them then there these they
    this those through too under until upon use used using very was were what
    when where which while who whom why will with within without would your
    source sources chunk chunks corpus retrieved retrieval context evidence
    section chapter page pages figure table appendix article xmlns kobospan class
    header title kobo span xhtml html href http https www com org pdf markdown
    text note notes example examples following previous including include includes
    """
    .split()
)


def _retrieval_nuance_source_text(source: Any) -> tuple[str, str]:
    data = _source_to_dict(source)
    if not data:
        return "", ""
    title = _source_title(data)
    heading = " ".join(str(part) for part in (data.get("heading_path") or []) if part)
    metadata = _source_metadata(data)
    parts = [
        heading,
        str(data.get("summary") or ""),
        str(metadata.get("title") or ""),
        str(metadata.get("section") or ""),
        str(data.get("text") or "")[:2600],
    ]
    text = "\n".join(part for part in parts if part)
    return title, text


def _retrieval_nuance_tokens(text: str) -> list[str]:
    normalized = str(text or "").lower()
    normalized = re.sub(r"`{1,3}[^`]*`{1,3}", " ", normalized)
    normalized = re.sub(r"[_/|=<>()[\]{}.,;:\"'!?*]+", " ", normalized)
    tokens: list[str] = []
    for token in _RETRIEVAL_NUANCE_TOKEN_RE.findall(normalized):
        cleaned = token.strip("-+#")
        if (
            len(cleaned) < 3
            or cleaned in _RETRIEVAL_NUANCE_STOPWORDS
            or cleaned.isdigit()
            or cleaned.startswith("xhtml")
            or cleaned.startswith("class")
        ):
            continue
        tokens.append(cleaned)
    return tokens


def _retrieval_nuance_rank_terms(
    tf: Counter[str],
    df: Counter[str],
    *,
    phrase: bool,
    limit: int,
) -> list[str]:
    ranked: list[tuple[int, int, str]] = []
    for term, count in tf.items():
        document_count = df.get(term, 0)
        if phrase:
            if document_count < 2 and count < 3:
                continue
            score = document_count * 16 + min(count, 24)
        else:
            if document_count < 2 and count < 3:
                continue
            score = document_count * 8 + min(count, 16)
        ranked.append((score, count, term))
    ranked.sort(key=lambda row: (-row[0], -row[1], row[2]))
    return [term for _score, _count, term in ranked[:limit]]


def _build_retrieval_nuance_digest(
    *,
    tier: Any,
    sources: list[Any] | None,
    facts: list[Any] | None,
    decoration: list[Any] | None,
    diagnostics: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extract deterministic repeated-context signals from the final RAG packet.

    This is intentionally model-free. The final answer model gets a small digest
    of terms, recurring documents, graph arrows, and lane mix so high-frequency
    corpus context survives synthesis instead of being washed out by a generic
    definition.
    """

    token_tf: Counter[str] = Counter()
    token_df: Counter[str] = Counter()
    phrase_tf: Counter[str] = Counter()
    phrase_df: Counter[str] = Counter()
    document_counts: Counter[str] = Counter()
    lane_mix: Counter[str] = Counter()

    for source in sources or []:
        title, text = _retrieval_nuance_source_text(source)
        if title:
            document_counts[title] += 1
        data = _source_to_dict(source) or {}
        lane = str(data.get("source_tier") or data.get("chunk_kind") or "source")
        lane_mix[lane] += 1
        tokens = _retrieval_nuance_tokens(text)
        if not tokens:
            continue
        token_tf.update(tokens)
        token_df.update(set(tokens))
        source_phrases: Counter[str] = Counter()
        for left, right in zip(tokens, tokens[1:]):
            if left == right:
                continue
            phrase = f"{left} {right}"
            if phrase in _RETRIEVAL_NUANCE_STOPWORDS:
                continue
            source_phrases[phrase] += 1
        phrase_tf.update(source_phrases)
        phrase_df.update(set(source_phrases))

    graph_relationships: list[str] = []
    graph_text_parts: list[str] = []
    for fact in facts or []:
        subject = str(getattr(fact, "subject", "") or "").strip()
        relation = str(
            getattr(fact, "fact_type", None)
            or getattr(fact, "property_name", None)
            or "relates_to"
        ).strip()
        value = str(getattr(fact, "value", "") or "").strip()
        evidence = str(getattr(fact, "evidence_phrase", "") or "").strip()
        if subject and value:
            graph_relationships.append(f"{subject} --{relation}-> {value}")
            graph_text_parts.append(f"{subject} {relation} {value} {evidence}")
    for edge in decoration or []:
        seed = str(getattr(edge, "seed_entity", "") or "").strip()
        neighbor = str(getattr(edge, "neighbor_entity", "") or "").strip()
        predicate = str(getattr(edge, "predicate", "") or "").strip()
        family = str(getattr(edge, "relation_family", "") or "").strip()
        relation = predicate or family or "relates_to"
        evidence = str(getattr(edge, "edge_evidence", "") or "").strip()
        if seed and neighbor:
            suffix = f" ({family})" if family and family != relation else ""
            graph_relationships.append(f"{seed} --{relation}-> {neighbor}{suffix}")
            graph_text_parts.append(f"{seed} {relation} {neighbor} {family} {evidence}")

    if graph_text_parts:
        graph_tokens = _retrieval_nuance_tokens(" ".join(graph_text_parts))
        token_tf.update(graph_tokens)
        token_df.update(set(graph_tokens))
        graph_phrases = Counter(
            f"{left} {right}"
            for left, right in zip(graph_tokens, graph_tokens[1:])
            if left != right
        )
        phrase_tf.update(graph_phrases)
        phrase_df.update(set(graph_phrases))

    phrases = _retrieval_nuance_rank_terms(phrase_tf, phrase_df, phrase=True, limit=8)
    phrase_words = {word for phrase in phrases for word in phrase.split()}
    singles = [
        term
        for term in _retrieval_nuance_rank_terms(token_tf, token_df, phrase=False, limit=12)
        if term not in phrase_words
    ][:8]
    high_frequency_context = [*phrases[:6], *singles[:6]][:10]

    diag = diagnostics or {}
    counts = diag.get("counts") if isinstance(diag.get("counts"), dict) else {}
    final_source_tiers = (
        diag.get("final_source_tiers")
        if isinstance(diag.get("final_source_tiers"), dict)
        else dict(lane_mix)
    )

    return {
        "tier": _retrieval_tier_value(tier),
        "high_frequency_context": high_frequency_context,
        "recurring_documents": [
            {"name": name, "chunks": count}
            for name, count in document_counts.most_common(6)
            if count >= 2
        ],
        "source_lane_mix": final_source_tiers or dict(lane_mix),
        "retrieval_additions": {
            "lexical": counts.get("lexical", 0),
            "facts": counts.get("facts", len(facts or [])),
            "graph_expanded": counts.get("graph_expanded", 0),
            "summary": counts.get("funnel_a", 0) + counts.get("global_summaries", 0),
            "children": counts.get("funnel_b", 0),
        },
        "graph_relationships": graph_relationships[:8],
    }


def _dedupe_preserving_order(items: list[str], *, limit: int) -> list[str]:
    """Case-insensitive de-dup that keeps first-seen order, capped at `limit`."""
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = item.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item.strip())
        if len(out) >= limit:
            break
    return out


def _format_retrieval_nuance_contract(digest: dict[str, Any] | None) -> str | None:
    """Internal synthesis hint passed to the model.

    Deliberately minimal: only a few de-duped salient terms and (when present)
    a handful of graph relationships. The diagnostic counters
    (recurring_documents / source_lane_mix / retrieval_additions) are NOT sent
    to the model — they are leak-prone and belong only in the trace. The
    instructions hard-forbid rendering the terms as a list or as repeated
    cue-prefixed sentences so a weak model cannot turn them into 'Also X.
    Also Y.' spam.
    """
    if not digest:
        return None
    terms = _dedupe_preserving_order(
        [str(term) for term in digest.get("high_frequency_context") or [] if term],
        limit=6,
    )
    relationships = _dedupe_preserving_order(
        [
            str(item)
            for item in (digest.get("graph_relationships") or [])
            if str(item).strip()
        ],
        limit=6,
    )
    if not any((terms, relationships)):
        return None

    lines = [
        "<retrieval_nuance_digest>",
        (
            "Internal synthesis hint from the retrieved packet. NEVER render, "
            "quote, or describe this block; it is guidance only, not content."
        ),
    ]
    if terms:
        lines.append(f"salient_terms: {', '.join(terms)}")
    if relationships:
        lines.append("salient_relationships:")
        lines.extend(f"- {relationship}" for relationship in relationships)
    lines.extend(
        [
            "How to use this hint:",
            (
                "- Fold the on-topic salient terms naturally into your normal "
                "prose, only where they genuinely sharpen the answer."
            ),
            (
                "- NEVER output these terms as a list, as a 'salient terms' "
                "section, or as one term per sentence, and never write a run of "
                "'Also ...'/'Additionally ...' sentences to cram them in."
            ),
            (
                "- If a term is off-question, omit it. Prefer corpus-grounded "
                "wording over generic pretrained phrasing when the evidence "
                "supports it."
            ),
            "</retrieval_nuance_digest>",
        ]
    )
    return "\n".join(lines)


def _format_retrieval_nuance_trace(digest: dict[str, Any] | None) -> str:
    if not digest:
        return "No repeated corpus cues were available for this turn."
    terms = [str(term) for term in digest.get("high_frequency_context") or [] if term]
    docs = [
        f"{item.get('name')} x{item.get('chunks')}"
        for item in (digest.get("recurring_documents") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    relationships = [
        str(item)
        for item in (digest.get("graph_relationships") or [])
        if str(item).strip()
    ]
    lines = ["[Retrieval nuance]", f"tier: {digest.get('tier') or 'unknown'}"]
    lines.append(
        "high_frequency_context: "
        + (", ".join(terms[:8]) if terms else "no repeated terms detected")
    )
    if docs:
        lines.append(f"recurring_documents: {', '.join(docs[:5])}")
    additions = digest.get("retrieval_additions") or {}
    if additions:
        lines.append(
            "layer_additions: "
            f"summary={additions.get('summary', 0)} · "
            f"children={additions.get('children', 0)} · "
            f"lexical={additions.get('lexical', 0)} · "
            f"facts={additions.get('facts', 0)} · "
            f"graph_expanded={additions.get('graph_expanded', 0)}"
        )
    if relationships:
        lines.append("graph_cues: " + " | ".join(relationships[:3]))
    return "\n".join(lines)


def _format_retrieval_diagnostics_trace(
    diagnostics: dict[str, Any] | None,
    *,
    fallback_tier: Any,
    raw_chunks: int,
    context_chunks: int,
) -> str:
    """Compact live trace text that makes retrieval tiers visibly different."""

    diag = diagnostics or {}
    contract = diag.get("store_contract") or {}
    counts = diag.get("counts") or {}
    timings = diag.get("timings_s") or {}
    limits = diag.get("limits") or {}
    intent = diag.get("intent") or {}
    final_source_tiers = diag.get("final_source_tiers") or {}
    effective = (
        diag.get("effective_tier")
        or getattr(fallback_tier, "value", fallback_tier)
        or "unknown"
    )
    label = contract.get("label") or str(effective)

    def _count(key: str) -> int:
        try:
            return int(counts.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    stores: list[str] = []
    if contract.get("qdrant_vectors"):
        stores.append(
            "Qdrant vectors "
            f"(summaries={_count('funnel_a') + _count('global_summaries')}, "
            f"children={_count('funnel_b')})"
        )
    if contract.get("mongo_lexical"):
        stores.append(
            "Mongo lexical/hydration "
            f"(lexical={_count('lexical')}, anchors={_count('document_anchor')})"
        )
    if contract.get("neo4j_facts"):
        stores.append(
            "Neo4j graph "
            f"(facts={_count('facts')}, expanded={_count('graph_expanded')})"
        )
    if not stores:
        stores.append("training-data only or no corpus stores")
    total_s = float(diag.get("total_s") or 0.0)
    final_mix = (
        ", ".join(
            f"{tier}={count}"
            for tier, count in sorted(final_source_tiers.items())
        )
        if isinstance(final_source_tiers, dict) and final_source_tiers
        else "none"
    )

    lines = [
        "[Retrieval tier trace]",
        f"tier: {label} ({effective})",
        f"mode: {diag.get('search_mode') or 'local'}",
        f"intent: {intent.get('need') or 'balanced'}",
        f"contract: {contract.get('description') or 'No tier contract available.'}",
        f"stores: {'; '.join(stores)}",
        (
            "pool: "
            f"merged={_count('merged_initial')} "
            f"ranked={_count('ranked')} "
            f"grounded={_count('ranked_query_grounded')} "
            f"final={context_chunks}/{raw_chunks}"
        ),
        f"final_source_tiers: {final_mix}",
        (
            "limits: "
            f"child={limits.get('child_top_k', '?')} "
            f"summary={limits.get('summary_top_k', '?')} "
            f"final={limits.get('final_top_k', '?')} "
            f"rerank={'on' if limits.get('rerank_enabled') else 'off'}"
        ),
        (
            "timing: "
            f"total={total_s:.2f}s "
            f"embed={float(timings.get('embed') or 0):.2f}s "
            f"funnels={float(timings.get('funnels') or 0):.2f}s "
            f"graph={float(timings.get('graph') or 0):.2f}s "
            f"rerank={float(timings.get('rerank') or 0):.2f}s "
            f"hydrate={float(timings.get('hydrate') or 0):.2f}s"
        ),
    ]
    return "\n".join(lines)


# Baseline system prompt, applied to every chat turn regardless of reasoning
# mode. Exists to fix the pre-Phase-23 pattern where the only style guidance
# was the optional reasoning template — leaving reasoning=none produced raw
# RLHF-default listy output. Layer this prompt first, layer reasoning on top
# if requested. Tuned for Mistral 7B+ / Claude / GPT-4-class models; tiny
# local models (<3B) will partially ignore it.
POLYMATH_SYSTEM_PROMPT = (
    "You are a knowledgeable collaborator answering from retrieved context.\n"
    "\n"
    "Follow these rules:\n"
    "- Match response length to question complexity. A one-line question gets "
    "a one-line answer. Do not pad.\n"
    "- Write in prose. Use bullets or numbered lists ONLY when the user asks, "
    "or when the answer is genuinely a list (e.g. 'what are the five…').\n"
    "- Synthesize across the context. Do NOT narrate chunk-by-chunk "
    "('Source 1 says X, Source 2 says Y'). Integrate.\n"
    "- Never emit a run of short sentences that begin with the same word "
    "(e.g. repeated 'Also ...' or 'Additionally ...' lines). Weave related "
    "points into unified sentences and vary sentence structure.\n"
    "- Do not dump retrieved keywords or recurring terms as a list or as one "
    "term per sentence. Fold any salient terms naturally into the prose only "
    "where they genuinely sharpen the answer.\n"
    "- Cite only when quoting directly or when a claim is genuinely contested "
    "across sources. Do not cite in every sentence.\n"
    "- Skip preambles ('Based on the provided context…', 'Great question…'). "
    "Start with the answer.\n"
    "- If the context doesn't contain the answer to a corpus-specific or "
    "source-specific question, say so in one sentence. Don't invent, don't "
    "pad.\n"
    "- If a `<context>` or `<key_facts>` block is present, treat it as the "
    "primary answer substrate. When retrieved evidence directly defines, "
    "explains, compares, or exemplifies the thing asked about, synthesize from "
    "that evidence first instead of substituting your pretrained background "
    "knowledge.\n"
    "- Use general knowledge only when no retrieved evidence is present, or as "
    "a small bridge for a term the sources do not explain. If you use it for a "
    "material claim, add a brief inline caveat where the claim appears (e.g. "
    "'(beyond what the sources cover)'). Do NOT emit a standing status line "
    "such as '→ the retrieved corpus does not establish ...' or a separate "
    "'what the corpus does not establish' section, and do not attach corpus "
    "citations to unsupported background knowledge.\n"
    "- In RAG mode, do not introduce named libraries, frameworks, products, "
    "papers, metrics, datasets, or examples unless they appear in the retrieved "
    "evidence or the user explicitly asks you to use outside knowledge. If a "
    "useful example is not in the context, describe it generically and label it "
    "as outside the retrieved corpus.\n"
    "- Use markdown for scanability: short paragraphs, meaningful headings "
    "when the answer has sections, bold key terms sparingly, and compact "
    "bullets for grouped facts.\n"
    "- Avoid one large stream block. If an answer is longer than six "
    "sentences, break it into 2-4 short sections with bolded headers or "
    "small markdown headings.\n"
    "- Use tables only when comparing options, sources, statuses, or tradeoffs. "
    "Keep table cells short.\n"
    "- Put install/run commands in fenced shell blocks so the UI can render "
    "them as command cards.\n"
    "- No exclamation marks unless quoting. No 'Great question!' preambles. "
    "Use visual markers only as semantic navigation, never decoration. The "
    "safe palette is → for reasoning bridges, ✓/✗ for binary status, and a "
    "single warning marker for real risks or failure modes.\n"
    "- Default to the KVP list pattern (`**key:** value`) for any factual "
    "rundown of 2-6 attributes. Default heading hierarchy is h2 then h3.\n"
    "\n"
    "Agent-Zero-inspired chat render style for RAG answers:\n"
    "- Treat the answer like compact whiteboard synthesis built from RAG "
    "evidence, not a graph-query report. Keep it clean, scannable, and "
    "high-signal.\n"
    "- Open with the answer's strongest one-sentence synthesis. For complex "
    "answers this can be a short bold summary sentence; for simple answers, "
    "just answer plainly.\n"
    "- Use a descending hierarchy of detail: summary first, then structured "
    "evidence or comparison if useful, then the reasoning bridge, then "
    "supporting detail and caveats.\n"
    "- Use tables first only when the answer contains structured data, "
    "comparisons, configurations, multiple options, steps, or tradeoffs. Do "
    "not force tables into plain conceptual explanations.\n"
    "- For multi-component design answers, the first substantial payload "
    "should usually be a compact table. Put structured technical detail in "
    "the table, then explain the plain-English meaning below it.\n"
    "- Use bold anchors for scanability: bold signal nouns, component names, "
    "risk labels, or key decision terms. Do not bold whole paragraphs.\n"
    "- Reasoning bridges are welcome when they help: after a factual payload, "
    "add 1-2 sentences explaining why it matters for the user's question. "
    "Use the `→` marker sparingly for these bridges. Do not add reasoning "
    "bridges to simple answers.\n"
    "- Use blockquotes only as brief margin annotations, not as another "
    "generic section. Use horizontal rules only for real topic shifts.\n"
    "- Put warnings, missing evidence, or failure risks in a compact warning "
    "block only when the caveat materially changes the answer. Preferred "
    "shape: `**Failure mode:** ...`, then `- **Symptom:** ...` and "
    "`- **Fix:** ...`.\n"
    "- Never expose retrieval mechanics unless the user asks for diagnostics: "
    "do not mention facets, lanes, chunks, packets, graph tiers, coverage "
    "contracts, or 'the retrieved corpus grounds X lanes' in the final answer.\n"
    "- Start with a short orientation paragraph that familiarizes dense terms "
    "and answers the question's core idea. Do not start with a source audit, "
    "coverage audit, or ingredient checklist.\n"
    "- Do not use fixed Graph Query section labels such as `Orientation`, "
    "`Direction`, `Comparative Read`, `Recommended Start`, or `Next "
    "Questions` unless the user explicitly asks for that template. Choose "
    "plain content-driven headings that answer the question, such as "
    "`Core idea`, `How it would work`, `What the evidence supports`, or "
    "`Limits`.\n"
    "- For ideation or product/application questions, answer as natural RAG: "
    "explain the central synthesis first, then give the strongest concrete "
    "design or reasoning path. Use 2-3 alternatives only when the evidence "
    "really supports multiple paths.\n"
    "- For app, product, prototype, architecture, or research-design queries, "
    "prefer pressure-tested synthesis over a pure concept pitch. After the "
    "core answer, include what works, what is under-specified, feasibility "
    "risks, validation needs, and the smallest credible prototype path.\n"
    "- When a design touches several domains, a compact table is often the "
    "best first payload: columns like `Component`, `Strength`, `Weakness`, "
    "`What to validate`, or `Implementation note`. Follow it with prose that "
    "weaves the pieces together.\n"
    "- Avoid academic-abstract flow for design questions. The target visual "
    "shape is: bold thesis, table or decision matrix, reasoning bridge, "
    "implementation/validation notes, then compact risks.\n"
    "- Break ambitious concepts into sub-problems when that makes the answer "
    "more actionable: research validity, engineering feasibility, UX pattern, "
    "privacy/data boundary, and minimum viable prototype.\n"
    "- Use existing conversation context when it is clearly relevant to the "
    "user's current idea. Connect to prior named concepts or architecture only "
    "if they are present in the conversation or retrieved evidence; do not "
    "invent private project history.\n"
    "- For psychometrics, assessment, identity, or AI-personalization ideas, "
    "include a validation path when relevant: reliability, convergent validity, "
    "human review/coding, measurement drift, hallucination control, and what "
    "would count as a credible pilot.\n"
    "- Include minimal concrete examples when useful, but keep them grounded "
    "in retrieved evidence. Keep bullets compact and avoid template filler.\n"
    "- End with follow-on questions only when they naturally help the user "
    "continue the work.\n"
    "- Do not use emoji as decoration. Use bold sparingly as the thick-marker "
    "takeaway stroke: one sentence, one key term, or one decision label.\n"
    "- If the internal RAG guardrail says an idea is weakly supported or "
    "unsupported, do not turn that into a retrieval-status section. Instead, "
    "avoid overclaiming and add a concise caveat exactly where the unsupported "
    "claim would otherwise appear.\n"
    "\n"
    "Sound like a smart friend explaining, not a research assistant producing "
    "a report."
)


def _build_polymath_system_prompt(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    current_date = current.strftime("%Y-%m-%d")
    current_tz = current.tzname() or "local time"
    return (
        f"{POLYMATH_SYSTEM_PROMPT}\n"
        "\n"
        "Date and source freshness:\n"
        f"- Today's date is {current_date} ({current_tz}). Interpret relative "
        "dates like today, latest, recent, current, yesterday, and last year "
        "against this date.\n"
        "- When live Web is enabled and the question may have changed over "
        "time, prefer current or recently updated primary sources where "
        "available. Add years, versions, release names, domains, or update "
        "terms to web queries when they improve precision.\n"
        "- Do not reject older sources when they are primary, historical, or "
        "the user is asking about stable theory. For evidence claims, separate "
        "what was actually read from what only appeared in a snippet."
    )


class ChatOrchestrator:
    """Orchestrates the complete chat pipeline."""

    async def process_chat_request(
        self, request: ChatRequest, user_id: str | None = None
    ) -> AsyncGenerator[str, None]:
        """
        Main orchestrator for chat requests.

        Orchestrates the complete pipeline:
        1. Load or create conversation
        2. Create and save user message
        3. Trim history to fit context window
        4. Stream LLM response
        5. Save assistant message

        Args:
            request: ChatRequest with message and optional conversation_id
            user_id: Authenticated user id (Phase 19.3 — required to resolve
                     `profile:<id>` model strings into custom model profiles).

        Yields:
            SSE-formatted chunks
        """
        # Track timing and metadata
        start_time = datetime.utcnow()
        trimming_applied = False
        trimming_details = ""
        trace_events: list[dict[str, Any]] = []
        system_prompt = _build_polymath_system_prompt()

        def _record_trace_event(
            *,
            lane: str,
            title: str,
            status: str,
            content: str = "",
            metadata: dict[str, Any] | None = None,
        ) -> str:
            event = {
                "id": f"trace-{len(trace_events) + 1}",
                "lane": lane,
                "title": title,
                "status": status,
                "content": content,
                "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
                "metadata": metadata or {},
            }
            trace_events.append(event)
            return build_sse_chunk(
                ChatChunk(
                    type="trace_event",
                    trace_event=event,
                    conversation_id=str(conversation_id),
                )
            )

        # Step 1: Load or create conversation
        (
            conversation_id,
            model_config,
            existing_messages,
        ) = await self._load_or_create_conversation(request)
        object.__setattr__(request, "_user_id", user_id)
        object.__setattr__(
            request,
            "_recent_chat_messages",
            list(existing_messages[-6:] if existing_messages else []),
        )

        # Step 2: Get model to use
        model_used = self._get_model_to_use(request, model_config)

        # Step 3: Create user message
        user_message = self._create_user_message(request.message, model_used)

        # Phase 19.3 / Phase E — resolve `profile:<id>` (legacy Custom Models)
        # and `pool:<id>` (unified Model Pool) prefixes into concrete
        # base_url + api_key + model. Both fall through to the same LiteLLM
        # `openai/*` passthrough path.
        profile_creds: dict = {}
        agentic_on_request = (
            request.overrides.agentic_mode
            if (request.overrides and request.overrides.agentic_mode is not None)
            else settings.AGENTIC_MODE_ENABLED
        )
        web_search_enabled = _is_web_search_enabled_for_request(request)
        web_only_tool_route = bool(
            web_search_enabled
            and not request.selected_tools
            and not agentic_on_request
        )
        tool_route_active = bool(
            request.selected_tools or agentic_on_request
        )
        if user_id and (
            model_used.startswith("profile:") or model_used.startswith("pool:")
        ):
            prefix, _, _id = model_used.partition(":")

            # Use the unified resolver which already walks:
            #   1. settings.models.query_model_pool  (Sprint 3 unified)
            #   2. legacy model_pool collection
            #   3. legacy model_profiles collection
            # and returns a normalized dict with `model` already provider-
            # prefixed. Phase 24 perf — imported at module-level.
            _resolved = await resolve_by_entry_id(user_id, _id)

            if _resolved:
                profile_creds = {
                    "api_base": _resolved.get("api_base"),
                    "api_key": _resolved.get("api_key"),
                    "extra_params": _resolved.get("extra_params") or None,
                }
                model_used = _resolved["model"]
                logger.info(
                    "%s resolved: user=%s id=%s → %s",
                    prefix, user_id, _id, model_used,
                )
            else:
                logger.warning(
                    "%s not found: user=%s id=%s; "
                    "falling back to DEFAULT_COMPLETION_MODEL.",
                    prefix, user_id, _id,
                )
                model_used = settings.DEFAULT_COMPLETION_MODEL

            # Critical: sync request.overrides.model with the resolved/fallback
            # value so _build_request_body (llm.py:102) doesn't clobber the
            # body back to the unresolved `pool:...` / `profile:...` string.
            if request.overrides is not None:
                request.overrides.model = model_used

        # Phase F — role resolution. User-selected tools and explicit agentic
        # mode still route the answer stream through the tool-capable role.
        # Web-only turns keep the selected chat model and expose the native
        # web tools directly so the chat model owns query/refine/sufficiency.
        if tool_route_active:
            qres = (
                await resolve_query_model_kind(user_id, "agentic")
                if user_id
                else None
            )
            if qres:
                model_used = qres["model"]
                profile_creds = {
                    "api_base": qres["api_base"],
                    "api_key": qres["api_key"],
                    "extra_params": qres["extra_params"] or None,
                }
                logger.info(
                    "Phase F query prefs resolution: user=%s kind=%s → %s",
                    user_id, "agentic", model_used,
                )
            else:
                model_used = settings.AGENTIC_MODEL
                profile_creds = {}
                logger.info(
                    "Agentic env fallback resolution: user=%s kind=agentic → %s",
                    user_id or "-", model_used,
                )

        elif (
            user_id
            and not profile_creds
            and not (model_used.startswith("pool:") or model_used.startswith("profile:"))
            and not (request.overrides and request.overrides.model)
            and model_used in (settings.DEFAULT_COMPLETION_MODEL, settings.AGENTIC_MODEL)
        ):
            qres = await resolve_query_model_kind(user_id, "query")
            if qres:
                model_used = qres["model"]
                profile_creds = {
                    "api_base": qres["api_base"],
                    "api_key": qres["api_key"],
                    "extra_params": qres["extra_params"] or None,
                }
                logger.info(
                    "Phase F query prefs resolution: user=%s kind=%s → %s",
                    user_id, "query", model_used,
                )

        if request.overrides is not None:
            request.overrides.model = model_used

        yield _record_trace_event(
            lane="model_call",
            title="Chat model route",
            status="done",
            content=(
                "Resolved the final chat model before retrieval and tool "
                "execution."
            ),
            metadata={"model": model_used, "web_planner_split": web_only_tool_route},
        )

        # Phase 29 — vision-capability pre-flight. If the user attached
        # images but picked a non-vision model, the LLM call will 4xx
        # mid-stream and surface as a generic transport error. Catch
        # the mismatch HERE with a clear SSE error so the user knows
        # exactly what to fix. Runs after model resolution (so we
        # know the final model_used) but before retrieval (so we
        # don't waste pipeline work on a doomed request).
        if request.attachments:
            from services.vision_capabilities import (
                attachments_include_image,
                supports_vision,
                vision_capable_models_hint,
            )

            if attachments_include_image(request.attachments) and not supports_vision(model_used):
                logger.warning(
                    "vision mismatch: model=%r has no vision support but "
                    "request has image attachments — emitting SSE error",
                    model_used,
                )
                yield build_sse_chunk(
                    ChatChunk(
                        type="error",
                        content=(
                            f"The selected model ({model_used}) doesn't "
                            f"support images. {vision_capable_models_hint()}"
                        ),
                    )
                )
                return

        # Resolve reasoning mode (Phase 15) — per-request overrides win,
        # else falls back to server-side default (wired at settings layer).
        reasoning_mode, reasoning_blend = self._resolve_reasoning(request)

        # Resolve Query Profile (Phase 18 / 23) — preset bundles retrieval_k +
        # rerank + HyDE. Custom profile loads extra knobs from user settings.
        # Individual overrides on ModelOverrides still win.
        profile_cfg = await self._resolve_query_profile(request, user_id=user_id)
        profile_k = profile_cfg["retrieval_k"]
        profile_rerank = profile_cfg["rerank_enabled"]
        query_profile_used = profile_cfg["query_profile"]
        reasoning_mode_used = reasoning_mode or "none"

        # Phase 17 — HyDE: when enabled, generate a hypothetical answer and
        # use IT as the retrieval query. Answers tend to embed closer to
        # answer-shaped chunks than questions do. Graceful fallback on failure.
        hyde_trace_enabled = bool(request.overrides and request.overrides.hyde_enabled)
        hyde_route = None
        if hyde_trace_enabled:
            hyde_route = await self._resolve_hyde_route(
                request,
                user_id=user_id,
                fallback_model=model_used,
                fallback_api_base=profile_creds.get("api_base"),
                fallback_api_key=profile_creds.get("api_key"),
                fallback_extra=profile_creds.get("extra_params"),
            )
        if hyde_trace_enabled:
            hyde_model_trace = (
                (hyde_route or {}).get("model")
                or settings.HYDE_MODEL
                or model_used
            )
            yield _record_trace_event(
                lane="model_call",
                title="HyDE query helper",
                status="running",
                content=_format_model_api_trace(
                    name="HyDE query helper",
                    model=hyde_model_trace,
                    status="starting",
                    purpose=(
                        "Generate a hypothetical answer used only as the "
                        "local RAG retrieval query."
                    ),
                ),
                metadata={"model": hyde_model_trace},
            )
        hyde_start = perf_counter()
        retrieval_query, hyde_applied = await self._apply_hyde(
            request,
            user_id=user_id,
            hyde_explicit=bool(profile_cfg.get("hyde_explicit", False)),
            fallback_model=model_used,
            fallback_api_base=profile_creds.get("api_base"),
            fallback_api_key=profile_creds.get("api_key"),
            fallback_extra=profile_creds.get("extra_params"),
            resolved_route=hyde_route,
        )
        if hyde_trace_enabled:
            yield _record_trace_event(
                lane="model_call",
                title="HyDE query helper",
                status="done" if hyde_applied else "skipped",
                content=_format_model_api_trace(
                    name="HyDE query helper",
                    model=hyde_model_trace,
                    status="finished" if hyde_applied else "skipped_or_fallback",
                    purpose=(
                        "Generate a hypothetical answer used only as the "
                        "local RAG retrieval query."
                    ),
                    duration_s=perf_counter() - hyde_start,
                    detail=(
                        "HyDE applied"
                        if hyde_applied
                        else "Raw user query was used for retrieval."
                    ),
                ),
                metadata={
                    "model": hyde_model_trace,
                    "duration_s": perf_counter() - hyde_start,
                    "applied": hyde_applied,
                },
            )

        yield _record_trace_event(
            lane="retrieval",
            title="Local RAG retrieval",
            status="running",
            content=(
                "Starting corpus retrieval before any web-search merge. "
                f"requested_tier={getattr(request.retrieval_tier, 'value', request.retrieval_tier)} "
                f"query={_clip_trace_value(retrieval_query, 220)}"
            ),
            metadata={
                "requested_tier": getattr(
                    request.retrieval_tier,
                    "value",
                    request.retrieval_tier,
                ),
                "retrieval_k": profile_k,
                "rerank_enabled": profile_rerank,
                "query_profile": query_profile_used,
                "hyde_applied": hyde_applied,
            },
        )
        rag_start = perf_counter()

        # Step 3.5: Retrieval Pipeline
        #   atomic mode: decompose query → fan-out retrieval → merge
        #   all other modes: standard single-query retrieval
        from services.retriever.search_mode import resolve_search_mode

        requested_mode = (
            getattr(request.overrides, "search_mode", None)
            if request.overrides
            else None
        )
        resolved_mode = resolve_search_mode(requested_mode, request.message)
        # Phase 4 — LLM intent second-chance: when auto-mode's markers resolved to
        # local, ask the classifier whether this is really an overview question and
        # upgrade to global so the domain-stratified breadth path activates.
        if (
            (requested_mode or "auto").lower() == "auto"
            and resolved_mode == "local"
            and _should_run_overview_intent_classifier(request.message)
        ):
            if await _classify_overview_intent(request.message) == "global":
                resolved_mode = "global"
                logger.info("intent classifier upgraded auto/local -> global (overview)")
        if reasoning_mode == "atomic":
            from services.reasoning import atomic_retrieve

            retrieval = await atomic_retrieve(
                query=retrieval_query,
                corpus_ids=request.corpus_ids,
                retrieval_tier=request.retrieval_tier,
                collections=request.collections,
                model=model_used,
            )
        else:
            # Phase 27 — resolve search-mode dispatch. "auto" infers from
            # the user's actual message (NOT the HyDE-expanded retrieval
            # query, which would have lost the original phrasing signal).
            logger.info(
                "search_mode: requested=%s resolved=%s",
                requested_mode or "auto",
                resolved_mode,
            )
            retrieval = await retriever_orchestrator.retrieve(
                query=retrieval_query,
                corpus_ids=request.corpus_ids,
                retrieval_tier=request.retrieval_tier,
                collections=request.collections,
                retrieval_k=profile_k,
                rerank_enabled=profile_rerank,
                ranking_query=request.message,
                top_k_summary=profile_cfg["top_k_summary"],
                rerank_top_n=profile_cfg["rerank_top_n"],
                similarity_threshold=profile_cfg["similarity_threshold"],
                neo4j_expansion_cap=profile_cfg["neo4j_expansion_cap"],
                max_corpora_per_query=profile_cfg["max_corpora_per_query"],
                final_top_k=profile_cfg["final_top_k"],
                fact_seed_limit=profile_cfg["fact_seed_limit"],
                search_mode=resolved_mode,
            )
        coverage_start = perf_counter()
        coverage_sources, coverage_meta = await _enforce_chat_query_coverage(
            original_query=request.message,
            retrieval_query=retrieval_query,
            sources=list(retrieval.chunks or []),
            corpus_ids=request.corpus_ids,
            retrieval_tier=getattr(retrieval, "effective_tier", request.retrieval_tier),
            collections=request.collections,
            retrieval_k=profile_k,
            rerank_enabled=profile_rerank,
            top_k_summary=profile_cfg["top_k_summary"],
            rerank_top_n=profile_cfg["rerank_top_n"],
            similarity_threshold=profile_cfg["similarity_threshold"],
            neo4j_expansion_cap=profile_cfg["neo4j_expansion_cap"],
            max_corpora_per_query=profile_cfg["max_corpora_per_query"],
            fact_seed_limit=profile_cfg["fact_seed_limit"],
            final_top_k=profile_cfg["final_top_k"],
            source_cap=profile_cfg.get("source_cap"),
            search_mode=resolved_mode,
        )
        coverage_meta["duration_s"] = perf_counter() - coverage_start
        if coverage_meta.get("added"):
            logger.info(
                "chat_semantic_coverage: tier=%s selected=%s added=%s docs=%s uncovered=%s",
                coverage_meta.get("effective_tier"),
                coverage_meta.get("selected_facets"),
                coverage_meta.get("added"),
                coverage_meta.get("support_doc_ids"),
                coverage_meta.get("coverage_uncovered_lanes", []),
            )
        elif coverage_meta.get("selected_facets"):
            logger.info(
                "chat_semantic_coverage: tier=%s selected=%s added=0 uncovered=%s reports=%s",
                coverage_meta.get("effective_tier"),
                coverage_meta.get("selected_facets"),
                coverage_meta.get("coverage_uncovered_lanes", []),
                coverage_meta.get("lane_reports", []),
            )
        sources = _dedupe_sources_for_context(coverage_sources)
        effective_tier_for_trace = getattr(
            retrieval.effective_tier,
            "value",
            retrieval.effective_tier,
        )
        yield _record_trace_event(
            lane="retrieval",
            title="Local RAG retrieval",
            status="done",
            content=_format_retrieval_diagnostics_trace(
                getattr(retrieval, "diagnostics", None),
                fallback_tier=retrieval.effective_tier,
                raw_chunks=len(retrieval.chunks or []),
                context_chunks=len(sources or []),
            ),
            metadata={
                "duration_s": perf_counter() - rag_start,
                "effective_tier": str(effective_tier_for_trace),
                "chunks": len(sources or []),
                "retrieval_diagnostics": getattr(retrieval, "diagnostics", {}),
                "coverage_detected_facets": coverage_meta.get("detected_facets", []),
                "coverage_query_facet_breakdown": coverage_meta.get("query_facet_breakdown", []),
                "coverage_selected_facets": coverage_meta.get("selected_facets", []),
                "coverage_explicit_missing_facets": coverage_meta.get(
                    "explicit_missing_facets", []
                ),
                "coverage_dynamic_missing_facets": coverage_meta.get(
                    "dynamic_missing_facets", []
                ),
                "coverage_skipped_dynamic_facets": coverage_meta.get(
                    "skipped_dynamic_facets", []
                ),
                "coverage_added": coverage_meta.get("added", 0),
                "coverage_support_lanes": coverage_meta.get("support_lanes", []),
                "coverage_support_search_mode": coverage_meta.get("support_search_mode"),
                "coverage_support_doc_ids": coverage_meta.get("support_doc_ids", []),
                "coverage_duration_s": coverage_meta.get("duration_s", 0.0),
                "evidence_filtered_low_value": coverage_meta.get("filtered_low_value", 0),
                "evidence_cleaned_frontmatter": coverage_meta.get("cleaned_frontmatter", 0),
                "coverage_priority_lanes": coverage_meta.get("coverage_priority_lanes", []),
                "coverage_uncovered_priority_lanes": coverage_meta.get(
                    "coverage_uncovered_priority_lanes", []
                ),
                "coverage_lane_counts": coverage_meta.get("coverage_lane_counts", {}),
                "coverage_uncovered_lanes": coverage_meta.get("coverage_uncovered_lanes", []),
                "coverage_lane_reports": coverage_meta.get("lane_reports", []),
            },
        )
        retrieval_diagnostics = getattr(retrieval, "diagnostics", {}) or {}
        synthesis_lens_contract = _format_retrieval_tier_synthesis_contract(
            retrieval.effective_tier,
            retrieval_diagnostics,
        )
        yield _record_trace_event(
            lane="planning",
            title="Synthesis lens",
            status="done",
            content=_format_retrieval_tier_lens_trace(
                retrieval.effective_tier,
                retrieval_diagnostics,
            ),
            metadata={
                "effective_tier": str(effective_tier_for_trace),
                "lens": _retrieval_tier_lens_name(retrieval.effective_tier),
                "retrieval_diagnostics": retrieval_diagnostics,
            },
        )

        graph_context_enabled = (
            _is_graph_augmented_tier(retrieval.effective_tier)
            and settings.NEO4J_ENABLED
        )
        facts: list = list(getattr(retrieval, "facts", []) or [])
        if facts and not graph_context_enabled:
            logger.warning(
                "Dropping %d graph facts because effective tier is %s",
                len(facts),
                retrieval.effective_tier,
            )
            facts = []

        # Pt 10d (Cluster 2 — Graph Decoration) — graph-tier-only
        # post-retrieval enrichment. Vector Base and Hybrid never call Neo4j
        # here. When facts already answer the query, decoration is redundant,
        # so skip the extra traversal.
        decoration: list = []
        if not graph_context_enabled:
            decoration = []
        elif len(facts) >= 3:
            logger.info(
                "Graph decoration skipped — %d facts answered the query (Pt 10d.1 gate)",
                len(facts),
            )
        else:
            try:
                from services.retriever.graph_decoration import (
                    graph_decorator as _graph_decorator,
                )

                # Phase 5b — pass db through so decorate_winners can
                # annotate each row with cached structural metrics
                # (betweenness, pagerank, fragile_bridge membership).
                # db=None falls back to base decoration unchanged.
                _db_for_decoration = getattr(
                    __import__(
                        "services.ingestion_service",
                        fromlist=["ingestion_service"],
                    ).ingestion_service,
                    "db",
                    None,
                )
                decoration = await _graph_decorator.decorate_winners(
                    winning_chunks=sources,
                    corpus_ids=request.corpus_ids,
                    wanted_families=None,  # v1: no QueryFacets yet — accept all families
                    neighbor_limit=8,
                    chunks_per_neighbor=3,
                    db=_db_for_decoration,
                )
            except Exception as exc:
                logger.warning("Graph decoration skipped: %s", exc)
                decoration = []

        retrieval_nuance_digest = _build_retrieval_nuance_digest(
            tier=retrieval.effective_tier,
            sources=sources,
            facts=facts,
            decoration=decoration,
            diagnostics=retrieval_diagnostics,
        )
        retrieval_nuance_contract = _format_retrieval_nuance_contract(
            retrieval_nuance_digest,
        )
        if retrieval_nuance_contract:
            yield _record_trace_event(
                lane="planning",
                title="Retrieval nuance",
                status="done",
                content=_format_retrieval_nuance_trace(retrieval_nuance_digest),
                metadata=retrieval_nuance_digest,
            )

        # Trust-signal snapshot — captured here so it carries through to
        # both the `done` SSE frame and the persisted assistant message.
        # `agentic_on_request` was resolved earlier (line ~80) and reflects
        # the per-request override else server default.
        chunks_returned = len(sources)
        strategy_used = retrieval.effective_tier
        if hasattr(strategy_used, "value"):
            strategy_used = strategy_used.value  # enum → str
        downgrade_reason = retrieval.downgrade_reason
        # Phase 24 — trust signal renamed in spirit. The agentic toggle is
        # gone; "agentic-mode-used" now means tool-calling was active this
        # turn, not merely that a tool-capable model exists in settings.
        agentic_mode_used = bool(request.selected_tools or web_search_enabled)
        # Corpus IDs scoped for this turn — None on the request becomes []
        # on the message so the FE state-derivation can treat empty as
        # "NO_RAG" without an extra falsy check.
        collections_queried_for_msg: list[str] = list(request.corpus_ids or [])

        # Notify client when the requested retrieval tier was downgraded
        # (e.g. graph requested but not all corpora have use_neo4j=True).
        if retrieval.downgrade_reason:
            yield build_sse_chunk(
                ChatChunk(
                    type="tier_downgraded",
                    content=retrieval.downgrade_reason,
                    conversation_id=str(conversation_id),
                )
            )

        if web_search_enabled:
            object.__setattr__(request, "_skip_web_query_enrichment", True)
            object.__setattr__(request, "_web_query_builder", None)
            object.__setattr__(request, "_web_query_planner", None)
            yield _record_trace_event(
                lane="planning",
                title="Agentic web loop ready",
                status="done",
                content=(
                    "Web toggle is enabled. Local RAG has been loaded into the "
                    "prompt, and the chat model must call native web_search "
                    "before the final response. The model decides whether the "
                    "returned evidence is sufficient, whether to refine the "
                    "query, and whether to fetch a specific page. Obscura is "
                    "used deterministically inside fetch_page/web_search when "
                    "the runtime policy says a JS-render fallback is needed."
                ),
                metadata={
                    "web_search_required_before_final": True,
                    "max_web_search_calls": _MAX_WEB_SEARCH_CALLS_PER_TURN,
                    "max_tool_calls": _MAX_TOOL_CALLS_PER_TURN,
                    "raw_chain_of_thought": False,
                },
            )

        if sources:
            yield build_sse_chunk(
                ChatChunk(
                    type="sources",
                    sources=sources,
                    conversation_id=str(conversation_id),
                )
            )

        # Phase 24 — Skills (multi-select) + Tools, fetched in PARALLEL.
        # Both are independent Mongo reads; running serially wasted ~50-100ms
        # per turn. asyncio.gather collapses them to one round-trip's worth
        # of latency. Result of the tools fetch is cached in
        # `_tools_loaded_for_signal` so _load_tools below skips the duplicate
        # query (it was the same call run twice in the legacy code).
        skills_task = (
            skills_registry.get_skills_by_ids(request.active_skill_ids)
            if request.active_skill_ids
            else None
        )
        tools_task = (
            tool_registry.get_tools_by_ids(request.selected_tools)
            if request.selected_tools
            else None
        )
        skills_loaded: list = []
        tools_loaded: list = []
        if skills_task and tools_task:
            try:
                skills_loaded, tools_loaded = await asyncio.gather(
                    skills_task, tools_task
                )
            except Exception as exc:
                logger.warning("Failed parallel skills+tools fetch: %s", exc)
        elif skills_task:
            try:
                skills_loaded = await skills_task
            except Exception as exc:
                logger.warning("Failed to load active skills: %s", exc)
        elif tools_task:
            try:
                tools_loaded = await tools_task
            except Exception as exc:
                logger.warning("Failed to load tools: %s", exc)

        active_skills_dicts: list[dict] = [
            {
                "name": s.name,
                "slash_command": s.slash_command,
                "instructions": s.instructions,
            }
            for s in skills_loaded
        ]
        if web_search_enabled:
            active_skills_dicts.append(
                {
                    "name": "Live Web Search",
                    "slash_command": "/web",
                    "instructions": (
                        "The user enabled live web search for this turn. First "
                        "read the local RAG context already present in this "
                        "prompt, then call the native web_search tool at least "
                        "once before giving the final answer. Write concise "
                        "keyword queries, inspect the returned snippets, page "
                        "fetch evidence, domains, and telemetry, and decide "
                        "whether the evidence is sufficient. If not sufficient, "
                        "call web_search again with a refined query or call "
                        "fetch_page for a specific URL. Use response only when "
                        "you have enough information to answer. Cite URLs for "
                        "web-backed facts. Do not expose raw tool JSON or XML "
                        "as prose."
                    ),
                    "auto_selected": True,
                }
            )
        if active_skills_dicts:
            logger.info(
                "Skills active: %s",
                [s["name"] for s in active_skills_dicts],
            )
            for s in active_skills_dicts:
                inst = s["instructions"] or ""
                preview = inst[:400].replace("\n", " ⏎ ")
                logger.info(
                    "  ↳ skill='%s' slash=%s injected_chars=%d preview=%s%s",
                    s["name"],
                    s.get("slash_command") or "(none)",
                    len(inst),
                    preview,
                    "…" if len(inst) > 400 else "",
                )
        active_tool_names: list[str] = [t.name for t in tools_loaded]
        if web_search_enabled:
            active_tool_names.append("web_search")
            active_tool_names.append("fetch_page")
        # Cache the loaded tools so _load_tools below doesn't repeat the
        # Mongo round-trip. Stash on `request` (mutates the Pydantic model
        # via __dict__ since it's the simplest hand-off; the field isn't
        # serialized back to the client).
        if tools_loaded:
            object.__setattr__(request, "_tools_preloaded", tools_loaded)

        # Phase 24 — Reasoning cascade (opt-in). Run BEFORE building the
        # augmented prompt so analysis can be embedded as a context block.
        analysis_text: str | None = None
        if request.reasoning_cascade and sources:
            try:
                # Phase 24 perf — analyze imported at module-level as
                # reasoning_cascade_analyze.
                # Pass the chat model + creds as the final fallback. If user
                # hasn't picked a reasoning model AND no REASONING_MODEL env,
                # the cascade reuses whatever model is already running the
                # chat — never silently degrades to a hardcoded Ollama default.
                analysis_text = await reasoning_cascade_analyze(
                    user_message.content,
                    sources,
                    user_id=user_id,
                    chat_model=model_used,
                    chat_api_base=profile_creds.get("api_base") if profile_creds else None,
                    chat_api_key=profile_creds.get("api_key") if profile_creds else None,
                    chat_extra_params=profile_creds.get("extra_params") if profile_creds else None,
                )
            except Exception as exc:
                logger.warning("Reasoning cascade failed: %s", exc)

        coverage_prompt_note = _format_chat_coverage_prompt_note(coverage_meta)
        analysis_blocks = [
            block
            for block in (
                analysis_text,
                synthesis_lens_contract,
                retrieval_nuance_contract,
                coverage_prompt_note,
            )
            if block and block.strip()
        ]
        analysis_text = "\n\n".join(block.strip() for block in analysis_blocks) or None

        # Build augmented prompt — works whether or not we have sources, as
        # long as skills or analysis or sources is present.
        # Pt 10d — decide whether decoration reaches the chat prompt. The
        # decoration was already computed above; the gate here is whether
        # the active reasoning mode tells the LLM to infer the graph
        # itself. If yes, withhold inline decoration (and rely on the
        # reasoning cascade or the LLM's own graph-reasoning prompt). If
        # no, pass it through to build_augmented_prompt for inline
        # rendering inside the existing citation `(via ...)` parens.
        inline_decoration: list = []
        if decoration:
            try:
                from services.retriever.graph_decoration import (
                    should_skip_inline_decoration as _should_skip_inline_decoration_fn,
                )

                if not _should_skip_inline_decoration_fn(reasoning_mode, reasoning_blend):
                    inline_decoration = decoration
            except Exception:
                # If the helper somehow fails, prefer "render" over "drop"
                # since the underlying check is just a string-set lookup.
                inline_decoration = decoration

        # Code lane (Phase 2) — if retrieval surfaced code chunks, auto-detect
        # the dominant language and append a virtual skill carrying generic
        # code-synthesis rules plus the language-specific override (when one
        # is defined). Skipped silently when the user has already activated
        # a /code-* skill manually. The skill flows through the standard
        # active_skills_dicts envelope — no special rendering path.
        from services.code_lane_skills import maybe_inject_code_skill
        skill_count_before_code_lane = len(active_skills_dicts)
        active_skills_dicts = maybe_inject_code_skill(sources, active_skills_dicts)
        if active_skills_dicts and len(active_skills_dicts) > skill_count_before_code_lane:
            auto = active_skills_dicts[-1]
            if auto.get("auto_selected"):
                logger.info("Code lane: auto-injected skill %s", auto["name"])

        # Phase 29 — inline text-file attachments into the user message
        # BEFORE the RAG augmentation runs. Text files (.md/.txt/code
        # files) are part of the user's request context; the model
        # should see them alongside RAG sources. Image attachments are
        # handled separately at the multimodal-dict conversion step
        # below — they can't be flattened to text.
        attachments = list(request.attachments or [])
        text_attachments = [a for a in attachments if a.kind == "text"]
        image_attachments = [a for a in attachments if a.kind == "image"]

        # Phase 29 follow-up — budget attachment tokens against the
        # context window BEFORE history trimming. Without this, the
        # trimmer only sees `user_message.token_count` reflecting the
        # raw text, and the multimodal image_url blocks (which can run
        # ~1000-1600 tokens each per provider) silently overflow the
        # context. Text-attachment bodies are also counted here so
        # the trimmer accounts for them even though they get inlined
        # AFTER this point — count_tokens reads the raw `att.content`.
        if attachments:
            from utils.tokens import estimate_attachment_tokens

            attachment_tokens = estimate_attachment_tokens(
                attachments, model_used,
            )
            if attachment_tokens > 0:
                user_message.token_count = (
                    (user_message.token_count or 0) + attachment_tokens
                )
                logger.info(
                    "attachment token budget: +%d (images=%d, text=%d) → "
                    "user_message.token_count=%d",
                    attachment_tokens,
                    len(image_attachments),
                    len(text_attachments),
                    user_message.token_count,
                )
        if text_attachments:
            inlined_parts: list[str] = []
            for att in text_attachments:
                # Cap per-file text at ~32K chars (~8K tokens). Truncation
                # is honest — show the prefix and stamp a marker so the
                # model knows the file was cut off.
                body_text = att.content
                truncated = False
                if len(body_text) > 32_000:
                    body_text = body_text[:32_000]
                    truncated = True
                marker = (
                    f"\n[...content truncated — file was {att.size_bytes} bytes]"
                    if truncated
                    else ""
                )
                inlined_parts.append(
                    f"<attached_file name=\"{att.filename}\" "
                    f"mime=\"{att.mime_type}\">\n{body_text}{marker}\n</attached_file>"
                )
            attachments_block = "\n\n".join(inlined_parts)
            # Prepend the attachments block so the user's question reads
            # last (most-recent / highest-attention position). If the
            # user's text is empty (attachment-only turn), the joint
            # validator ensured at least one attachment exists.
            existing_text = (user_message.content or "").strip()
            user_message.content = (
                f"{attachments_block}\n\n{existing_text}"
                if existing_text
                else attachments_block
            )

        if sources or facts or active_skills_dicts or analysis_text:
            user_message.content = context_manager.build_augmented_prompt(
                query=user_message.content,
                sources=sources,
                facts=facts,
                corpus_ids=request.corpus_ids,
                reasoning_mode=reasoning_mode,
                reasoning_blend=reasoning_blend,
                active_skills=active_skills_dicts or None,
                analysis=analysis_text,
                decoration=inline_decoration,
            )

        # Step 4: Prepare messages for context
        messages_for_context = existing_messages + [user_message]

        # Step 5: Trim history to fit context window
        (
            trimmed_messages,
            trimming_applied,
            trimming_details,
            tokens_used_post_trim,
            tokens_max,
        ) = await self._trim_history(messages_for_context, model_used)

        # Always emit a budget frame so the UI can render "X / Y tokens"
        yield build_sse_chunk(
            ChatChunk(
                type="budget",
                conversation_id=str(conversation_id),
                tokens_used=tokens_used_post_trim,
                tokens_max=tokens_max,
                trimming_applied=trimming_applied,
            )
        )

        # Send trimming notification if history was trimmed
        if trimming_applied:
            yield build_sse_chunk(
                ChatChunk(
                    type="trimming",
                    content=trimming_details,
                    conversation_id=str(conversation_id),
                    trimming_applied=True,
                    trimming_details=trimming_details,
                )
            )

        # Step 6: Load tools if agentic mode is enabled
        tools, tool_schemas = await self._load_tools(request)

        # === START ReAct LOOP ===
        tool_call_count = 0
        web_search_call_count = 0
        tool_limit_reached = False
        react_messages: list[dict] = []
        tools_used_names: list[str] = []
        web_required_retry_count = 0
        last_generation_messages: list[dict] = []

        # Persist the RAW user message, not the RAG-augmented one. The object
        # `user_message.content` was overwritten above with the full augmented
        # prompt (context block + skills + analysis + question). Saving that
        # back poisoned history: every subsequent turn reloaded the prior
        # turn's retrieved chunks as "user input", compounding bloat. Rebuild
        # a clean ChatMessage from request.message so Mongo stores only what
        # the user typed.
        user_saved = await conversation_service.append_message(
            str(conversation_id),
            self._create_user_message(request.message, model_used),
        )
        if not user_saved:
            logger.error("Failed to persist user message for %s", conversation_id)
            yield build_sse_chunk(
                ChatChunk(
                    type="error",
                    content="Failed to save the user message. Please retry.",
                    conversation_id=str(conversation_id),
                )
            )
            return

        while tool_call_count < _MAX_TOOL_CALLS_PER_TURN:
            # Convert messages to dict format for LLM. Baseline system prompt
            # (Phase 23) is prepended every turn so style/length/anti-list
            # guidance survives regardless of whether reasoning mode is set.
            message_dicts: list[dict] = [
                {"role": "system", "content": system_prompt},
                *(
                    {"role": msg.role, "content": msg.content}
                    for msg in trimmed_messages
                ),
            ]

            # Phase 29 — multimodal injection for image attachments. The
            # text content (RAG sources + inlined text-file attachments
            # + user query) is already on the last user message. Convert
            # that message's `content` from a plain string to an OpenAI/
            # LiteLLM multimodal content array: one text block followed
            # by image_url blocks (one per image attachment). LiteLLM
            # passes the array through to the upstream provider, which
            # handles the multimodal completion natively.
            #
            # Only the FINAL user message gets multimodal content —
            # previous turns are history and stay text-only. Attachments
            # are per-turn (Phase 29 design choice — they don't persist).
            if image_attachments:
                for i in range(len(message_dicts) - 1, -1, -1):
                    if message_dicts[i].get("role") == "user":
                        text_content = message_dicts[i].get("content") or ""
                        content_blocks: list[dict] = [
                            {"type": "text", "text": text_content},
                        ]
                        for att in image_attachments:
                            # `data:image/png;base64,xxx` URI format —
                            # universally accepted by OpenAI/Anthropic/
                            # Gemini multimodal endpoints, and LiteLLM
                            # forwards the URL field unchanged.
                            data_url = (
                                f"data:{att.mime_type};base64,{att.content}"
                            )
                            content_blocks.append({
                                "type": "image_url",
                                "image_url": {"url": data_url},
                            })
                        message_dicts[i] = {
                            "role": "user",
                            "content": content_blocks,
                        }
                        break
            if react_messages:
                message_dicts.extend(react_messages)
            last_generation_messages = message_dicts
            force_initial_web_search = bool(
                web_search_enabled and web_search_call_count == 0
            )
            active_tool_schemas = _available_tool_schemas(
                tool_schemas,
                web_search_call_count=web_search_call_count,
                force_initial_web_search=force_initial_web_search,
            )
            active_tool_choice = (
                _force_tool_choice("web_search")
                if force_initial_web_search
                and _tool_schemas_contain(active_tool_schemas, "web_search")
                else None
            )

            assistant_content = ""
            assistant_thinking = ""
            tool_calls = []
            suppress_content_until_web = bool(
                web_search_enabled and web_search_call_count == 0
            )

            # Perf instrumentation — measure TTFT (time to first token),
            # stream duration, and post-stream tail so we can tell an LLM
            # that's slow to respond apart from a blocking post-stream hook.
            stream_start = perf_counter()
            first_token_at: float | None = None
            stream_end: float | None = None

            # Step 7: Stream LLM response
            yield _record_trace_event(
                lane="model_call",
                title="Chat model stream",
                status="running",
                content=_format_model_api_trace(
                    name="Chat model stream",
                    model=model_used,
                    status="starting",
                    purpose=(
                        "Generate the assistant response using the "
                        "retrieved RAG context and any completed tool results."
                    ),
                    detail=(
                        f"messages={len(message_dicts)} "
                        f"tools={','.join(_tool_schema_names(active_tool_schemas)) or 'no'}"
                    ),
                ),
                metadata={
                    "model": model_used,
                    "messages": len(message_dicts),
                    "tools_enabled": bool(active_tool_schemas),
                    "tool_choice": (
                        "web_search" if active_tool_choice is not None else None
                    ),
                    "forced_initial_web_search": bool(active_tool_choice),
                    "web_search_required_before_final": bool(
                        web_search_enabled and web_search_call_count == 0
                    ),
                },
            )
            try:
                async for chunk in llm_service.stream_chat(
                    messages=message_dicts,
                    model=model_used,
                    overrides=request.overrides,
                    tools=active_tool_schemas or None,
                    tool_choice=active_tool_choice,
                    **profile_creds,
                ):
                    if first_token_at is None and (
                        chunk.get("content") or chunk.get("thinking") or chunk.get("tool_calls")
                    ):
                        first_token_at = perf_counter()
                    if chunk.get("tool_calls"):
                        tool_calls.extend(chunk["tool_calls"])
                    elif chunk.get("thinking"):
                        assistant_thinking += chunk["thinking"]
                        yield build_sse_chunk(
                            ChatChunk(
                                type="thinking",
                                thinking=chunk["thinking"],
                                conversation_id=str(conversation_id),
                            )
                        )
                    elif chunk.get("content"):
                        assistant_content += chunk["content"]
                        if not suppress_content_until_web:
                            yield build_sse_chunk(
                                ChatChunk(
                                    type="token",
                                    content=chunk["content"],
                                    conversation_id=str(conversation_id),
                                )
                            )

            except Exception as e:
                logger.error(f"Error during LLM streaming: {e}")
                # In-process model fallback: primary failed before any content or
                # tool call (e.g. lapsed-model 500 at stream start) — retry once on
                # the known-good backup so the answer is not blank. Falls through to
                # the normal no-tool answer path on success; on still-empty, errors.
                if (
                    not assistant_content.strip()
                    and not tool_calls
                    and model_used != _CHAT_FALLBACK_MODEL
                ):
                    try:
                        logger.warning("stream fallback %s -> %s", model_used, _CHAT_FALLBACK_MODEL)
                        async for fb in llm_service.stream_chat(
                            messages=message_dicts,
                            model=_CHAT_FALLBACK_MODEL,
                            overrides=None,
                            tools=None,
                        ):
                            if fb.get("content"):
                                assistant_content += fb["content"]
                                if not suppress_content_until_web:
                                    yield build_sse_chunk(
                                        ChatChunk(
                                            type="token",
                                            content=fb["content"],
                                            conversation_id=str(conversation_id),
                                        )
                                    )
                    except Exception as e2:
                        logger.error(f"Fallback model also failed: {e2}")
                if not assistant_content.strip() and not tool_calls:
                    yield _record_trace_event(
                        lane="model_call",
                        title="Chat model stream",
                        status="error",
                        content=f"LLM streaming error: {e}",
                        metadata={"model": model_used},
                    )
                    yield build_sse_chunk(
                        ChatChunk(type="error", content=f"LLM streaming error: {e}")
                    )
                    return

            stream_end = perf_counter()
            yield _record_trace_event(
                lane="model_call",
                title="Chat model stream",
                status="done",
                content=_format_model_api_trace(
                    name="Chat model stream",
                    model=model_used,
                    status="finished",
                    purpose=(
                        "Generate the assistant response using the "
                        "retrieved RAG context and any completed tool results."
                    ),
                    duration_s=stream_end - stream_start,
                    detail=(
                        f"content_chars={len(assistant_content)} "
                        f"thinking_chars={len(assistant_thinking)} "
                        f"tool_calls={len(tool_calls)}"
                    ),
                ),
                metadata={
                    "model": model_used,
                    "duration_s": stream_end - stream_start,
                    "content_chars": len(assistant_content),
                    "thinking_chars": len(assistant_thinking),
                    "tool_calls": len(tool_calls),
                },
            )

            # Drop malformed / unknown tool calls before deciding whether this
            # turn is agentic. Some models (e.g. minimax-m2.7) emit an
            # empty-name tool call alongside a complete answer. An empty or
            # unrecognized name would otherwise "execute" as a not-found tool,
            # force a second generation pass, and stream a SECOND answer that
            # concatenates onto the first in the live UI (the persisted message
            # is fine because assistant_content is reset each iteration). The
            # "response" finish-tool is always considered valid.
            if tool_calls:
                tool_calls, dropped_tool_names = _partition_known_tool_calls(
                    tool_calls, active_tool_schemas
                )
                if dropped_tool_names:
                    logger.info(
                        "Dropped %d invalid/unknown tool call(s) from %s: %s",
                        len(dropped_tool_names),
                        model_used,
                        dropped_tool_names,
                    )
                    yield _record_trace_event(
                        lane="tool_call",
                        title="Ignored malformed tool call",
                        status="done",
                        content=(
                            f"Ignored {len(dropped_tool_names)} malformed or "
                            f"unknown tool call(s): "
                            f"{', '.join(dropped_tool_names)}. Treating the "
                            "streamed content as the final answer."
                        ),
                        metadata={"dropped": dropped_tool_names},
                    )

            # If no tool calls, this is the final response
            if not tool_calls:
                if web_search_enabled and web_search_call_count == 0:
                    web_required_retry_count += 1
                    logger.info(
                        "Web-enabled turn attempted final answer before web_search; retry=%d",
                        web_required_retry_count,
                    )
                    if web_required_retry_count > 1:
                        yield _record_trace_event(
                            lane="tool_call",
                            title="Required web_search missing",
                            status="error",
                            content=(
                                "The model attempted to answer without calling "
                                "web_search even though the Web toggle is enabled."
                            ),
                            metadata={"web_search_required_before_final": True},
                        )
                        yield build_sse_chunk(
                            ChatChunk(
                                type="error",
                                content=(
                                    "Web is enabled, but the model did not call "
                                    "web_search before answering. Please retry the "
                                    "turn or choose a tool-capable model."
                                ),
                                conversation_id=str(conversation_id),
                            )
                        )
                        return
                    if assistant_content.strip() or assistant_thinking.strip():
                        react_messages.append(
                            {
                                "role": "assistant",
                                "content": assistant_content.strip() or None,
                                **(
                                    {"reasoning_content": assistant_thinking}
                                    if assistant_thinking.strip()
                                    else {}
                                ),
                            }
                        )
                    react_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The Web toggle is enabled for this turn. Call "
                                "the native web_search tool now with a concise "
                                "keyword query before any final answer."
                            ),
                        }
                    )
                    assistant_content = ""
                    assistant_thinking = ""
                    continue
                if _looks_like_raw_tool_request_content(assistant_content):
                    logger.info(
                        "Suppressed raw tool-call syntax in assistant content; "
                        "forcing final no-tool answer for %s",
                        conversation_id,
                    )
                    assistant_content = ""
                    tool_limit_reached = True
                break

            remaining_tool_calls = _MAX_TOOL_CALLS_PER_TURN - tool_call_count
            (
                tool_calls,
                selected_web_search_calls,
                dropped_for_tool_limit,
                dropped_for_web_limit,
            ) = _limit_tool_calls_for_turn(
                tool_calls,
                remaining_tool_calls=remaining_tool_calls,
                web_search_call_count=web_search_call_count,
            )
            if dropped_for_tool_limit:
                tool_limit_reached = True
            if dropped_for_web_limit:
                logger.info(
                    "Dropped extra web_search tool call(s); limit is %d per turn",
                    _MAX_WEB_SEARCH_CALLS_PER_TURN,
                )
            if not tool_calls:
                break

            response_text, response_call, non_response_tool_calls = (
                _extract_response_tool_text(tool_calls)
            )
            if response_call is not None and not non_response_tool_calls:
                if web_search_enabled and web_search_call_count == 0:
                    web_required_retry_count += 1
                    if web_required_retry_count > 1:
                        yield _record_trace_event(
                            lane="tool_call",
                            title="Required web_search missing",
                            status="error",
                            content=(
                                "The model called response() before web_search "
                                "twice while Web was enabled."
                            ),
                            metadata={"web_search_required_before_final": True},
                        )
                        yield build_sse_chunk(
                            ChatChunk(
                                type="error",
                                content=(
                                    "Web is enabled, but the model called response "
                                    "before web_search. Please retry the turn or "
                                    "choose a tool-capable model."
                                ),
                                conversation_id=str(conversation_id),
                            )
                        )
                        return
                    react_messages.append(
                        {
                            "role": "assistant",
                            "content": assistant_content or None,
                            "tool_calls": [
                                {
                                    "id": response_call.get("id") or "response_before_web",
                                    "type": response_call.get("type") or "function",
                                    "function": response_call.get("function") or {},
                                }
                            ],
                        }
                    )
                    react_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": response_call.get("id") or "response_before_web",
                            "name": "response",
                            "content": (
                                "Rejected: Web is enabled, so call web_search "
                                "before response."
                            ),
                        }
                    )
                    continue
                if response_text:
                    streamed_narrative = assistant_content.strip()
                    assistant_content = response_text
                    yield _record_trace_event(
                        lane="final",
                        title="response tool",
                        status="done",
                        content="Model called response() to terminate the agentic loop.",
                        metadata={"tool_name": "response"},
                    )
                    # If the model already streamed narrative this iteration,
                    # reset the UI draft so response_text replaces it instead of
                    # appending (avoids a duplicated answer in the live stream).
                    if streamed_narrative and not suppress_content_until_web:
                        yield build_sse_chunk(
                            ChatChunk(
                                type="draft_reset",
                                conversation_id=str(conversation_id),
                            )
                        )
                    yield build_sse_chunk(
                        ChatChunk(
                            type="token",
                            content=response_text,
                            conversation_id=str(conversation_id),
                        )
                    )
                    break
            if response_call is not None:
                tool_calls = non_response_tool_calls

            # The model streamed narrative content this iteration but is about
            # to call real tools and regenerate. Tell the UI to discard the
            # in-progress draft so this pre-tool prose does not concatenate with
            # the final answer the next iteration streams (live-stream
            # de-duplication; the persisted message is already clean).
            if assistant_content.strip() and not suppress_content_until_web:
                yield build_sse_chunk(
                    ChatChunk(
                        type="draft_reset",
                        conversation_id=str(conversation_id),
                    )
                )

            # Announce tool execution before running — lets the UI show "⚙ Running: <tool>"
            tool_call_summaries = [
                {
                    "name": c.get("function", {}).get("name", ""),
                    "args": c.get("function", {}).get("arguments", "{}"),
                }
                for c in tool_calls
            ]
            yield _record_trace_event(
                lane="tool_call",
                title="Native tool call",
                status="running",
                content=json.dumps(tool_call_summaries),
                metadata={
                    "tool_count": len(tool_call_summaries),
                    "stored_before_execution": True,
                },
            )
            yield build_sse_chunk(
                ChatChunk(
                    type="tool_call_start",
                    content=json.dumps(tool_call_summaries),
                    conversation_id=str(conversation_id),
                )
            )

            # If we have tool calls, execute them
            tool_call_count += len(tool_calls)
            web_search_call_count += selected_web_search_calls
            tool_limit_reached = tool_limit_reached or (
                tool_call_count >= _MAX_TOOL_CALLS_PER_TURN
            )
            tool_results = await self._execute_tools(tool_calls, tools, request)
            for call in tool_calls:
                name = _tool_call_name(call)
                if name and name not in tools_used_names:
                    tools_used_names.append(name)

            # Emit tool results — paired 1:1 with the start event
            tool_result_summaries = [
                {
                    "name": c.get("function", {}).get("name", ""),
                    "result": r,
                }
                for c, r in zip(tool_calls, tool_results)
            ]
            yield build_sse_chunk(
                ChatChunk(
                    type="tool_result",
                    content=json.dumps(tool_result_summaries),
                    conversation_id=str(conversation_id),
                )
            )
            for call, result in zip(tool_calls, tool_results):
                if _tool_call_name(call) == "web_search":
                    decision_trace = _format_web_retrieval_decision_trace(result)
                    if decision_trace is not None:
                        decision_content, decision_metadata = decision_trace
                        yield _record_trace_event(
                            lane="reasoning",
                            title="Web retrieval decision trace",
                            status="done",
                            content=decision_content,
                            metadata=decision_metadata,
                        )
            yield _record_trace_event(
                lane="tool_result",
                title="Native tool result",
                status="done",
                content=json.dumps(
                    [
                        {
                            "name": item["name"],
                            "result_preview": _clip_trace_value(item["result"], 500),
                        }
                        for item in tool_result_summaries
                    ]
                ),
                metadata={"tool_count": len(tool_result_summaries)},
            )

            # Append tool results to message history and continue loop
            assistant_tool_calls: list[dict] = []
            for i, call in enumerate(tool_calls):
                call_id = call.get("id") or f"call_{tool_call_count}_{i}"
                fn = call.get("function") or {}
                assistant_tool_calls.append(
                    {
                        "id": call_id,
                        "type": call.get("type") or "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "{}",
                        },
                    }
                )
            assistant_tool_message = {
                "role": "assistant",
                "content": assistant_content or None,
                "tool_calls": assistant_tool_calls,
            }
            if assistant_thinking.strip():
                # DeepSeek-style thinking models require prior assistant
                # reasoning_content to be echoed when continuing after a
                # tool call. Without this, LiteLLM/provider rejects the
                # follow-up and the streamed answer never gets persisted.
                assistant_tool_message["reasoning_content"] = assistant_thinking
            react_messages.append(assistant_tool_message)
            for i, (call, result) in enumerate(zip(tool_calls, tool_results)):
                fn = call.get("function") or {}
                react_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": (
                            call.get("id")
                            or assistant_tool_calls[min(i, len(assistant_tool_calls) - 1)][
                                "id"
                            ]
                        ),
                        "name": fn.get("name") or "",
                        "content": result,
                    }
                )

            pending_tool_sources = getattr(request, "_pending_tool_sources", [])
            if pending_tool_sources:
                sources = _dedupe_sources_for_context(
                    _cap_web_sources_for_turn(
                        _append_deduped_web_sources(
                            list(sources or []),
                            list(pending_tool_sources),
                        )
                    )
                )
                chunks_returned = len(sources)
                object.__setattr__(request, "_pending_tool_sources", [])
                yield build_sse_chunk(
                    ChatChunk(
                        type="sources",
                        sources=sources,
                        conversation_id=str(conversation_id),
                    )
                )
                evidence_block = _format_evidence_packet_block(
                    sources=sources,
                    request=request,
                )
                if evidence_block:
                    evidence_signature = str(hash(evidence_block))
                    if (
                        getattr(request, "_last_evidence_packet_signature", None)
                        != evidence_signature
                    ):
                        object.__setattr__(
                            request,
                            "_last_evidence_packet_signature",
                            evidence_signature,
                        )
                        react_messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"{evidence_block}\n\n"
                                    "Use this evidence packet for the final "
                                    "synthesis. Do not repeat the packet; "
                                    "answer the user's original question."
                                ),
                            }
                        )
                        yield _record_trace_event(
                            lane="reasoning",
                            title="Evidence packet",
                            status="done",
                            content=_clip_trace_value(evidence_block, 900),
                            metadata={
                                "web_sources": sum(
                                    1
                                    for source in sources
                                    if (
                                        (data := _source_to_dict(source))
                                        and _is_web_source_data(data)
                                    )
                                ),
                                "corpus_sources": sum(
                                    1
                                    for source in sources
                                    if (
                                        (data := _source_to_dict(source))
                                        and not _is_web_source_data(data)
                                    )
                                ),
                                "raw_chain_of_thought": False,
                            },
                        )
            elif _collect_web_run_summaries(request):
                evidence_block = _format_evidence_packet_block(
                    sources=sources,
                    request=request,
                )
                if evidence_block:
                    evidence_signature = str(hash(evidence_block))
                    if (
                        getattr(request, "_last_evidence_packet_signature", None)
                        != evidence_signature
                    ):
                        object.__setattr__(
                            request,
                            "_last_evidence_packet_signature",
                            evidence_signature,
                        )
                        react_messages.append(
                            {
                                "role": "user",
                                "content": (
                                    f"{evidence_block}\n\n"
                                    "Use this evidence packet for the final "
                                    "synthesis. Do not repeat the packet; "
                                    "answer the user's original question."
                                ),
                            }
                        )

        # === END ReAct LOOP ===

        if react_messages and (tool_limit_reached or not assistant_content.strip()):
            if tool_limit_reached:
                assistant_content = ""
            final_messages: list[dict] = [
                {"role": "system", "content": system_prompt},
                *(
                    {"role": msg.role, "content": msg.content}
                    for msg in trimmed_messages
                ),
                *react_messages,
                {
                    "role": "user",
                    "content": (
                        "Use the gathered corpus and tool results above to answer "
                        "the original question now. Do not call any more tools, "
                        "and do not write tool-call syntax, XML, JSON, or DSML. "
                        "Write only the user-facing answer."
                    ),
                },
            ]
            last_generation_messages = final_messages
            try:
                final_stream_start = perf_counter()
                yield _record_trace_event(
                    lane="model_call",
                    title="Chat model final stream",
                    status="running",
                    content=_format_model_api_trace(
                        name="Chat model final no-tool stream",
                        model=model_used,
                        status="starting",
                        purpose=(
                            "Force a user-facing answer after tool "
                            "activity without allowing more tool calls."
                        ),
                        detail=f"messages={len(final_messages)} tools=no",
                    ),
                    metadata={
                        "model": model_used,
                        "messages": len(final_messages),
                        "tools_enabled": False,
                    },
                )
                async for chunk in llm_service.stream_chat(
                    messages=final_messages,
                    model=model_used,
                    overrides=request.overrides,
                    tools=None,
                    **profile_creds,
                ):
                    if chunk.get("thinking"):
                        assistant_thinking += chunk["thinking"]
                        yield build_sse_chunk(
                            ChatChunk(
                                type="thinking",
                                thinking=chunk["thinking"],
                                conversation_id=str(conversation_id),
                            )
                        )
                    elif chunk.get("content"):
                        assistant_content += chunk["content"]
                        yield build_sse_chunk(
                            ChatChunk(
                                type="token",
                                content=chunk["content"],
                                conversation_id=str(conversation_id),
                            )
                        )
                final_duration_s = perf_counter() - final_stream_start
                yield _record_trace_event(
                    lane="model_call",
                    title="Chat model final stream",
                    status="done",
                    content=_format_model_api_trace(
                        name="Chat model final no-tool stream",
                        model=model_used,
                        status="finished",
                        purpose=(
                            "Force a user-facing answer after tool "
                            "activity without allowing more tool calls."
                        ),
                        duration_s=final_duration_s,
                        detail=(
                            f"content_chars={len(assistant_content)} "
                            f"thinking_chars={len(assistant_thinking)}"
                        ),
                    ),
                    metadata={
                        "model": model_used,
                        "duration_s": final_duration_s,
                        "content_chars": len(assistant_content),
                        "thinking_chars": len(assistant_thinking),
                    },
                )
            except Exception as e:
                logger.error(f"Error during final no-tool LLM streaming: {e}")
                # In-process model fallback (see _CHAT_FALLBACK_MODEL): retry once on
                # the backup when the primary produced nothing, so the forced answer
                # is not blank. On still-empty, fall through to the error emit.
                if not assistant_content.strip() and model_used != _CHAT_FALLBACK_MODEL:
                    try:
                        logger.warning("final-stream fallback %s -> %s", model_used, _CHAT_FALLBACK_MODEL)
                        async for fb in llm_service.stream_chat(
                            messages=final_messages,
                            model=_CHAT_FALLBACK_MODEL,
                            overrides=None,
                            tools=None,
                        ):
                            if fb.get("content"):
                                assistant_content += fb["content"]
                                yield build_sse_chunk(
                                    ChatChunk(
                                        type="token",
                                        content=fb["content"],
                                        conversation_id=str(conversation_id),
                                    )
                                )
                    except Exception as e2:
                        logger.error(f"Fallback model also failed: {e2}")
                if not assistant_content.strip():
                    yield _record_trace_event(
                        lane="model_call",
                        title="Chat model final stream",
                        status="error",
                        content=f"LLM streaming error: {e}",
                        metadata={"model": model_used},
                    )
                    yield build_sse_chunk(
                        ChatChunk(type="error", content=f"LLM streaming error: {e}")
                    )
                    return

        if not assistant_content.strip():
            if model_used != _CHAT_FALLBACK_MODEL and last_generation_messages:
                yield _record_trace_event(
                    lane="model_call",
                    title="Chat model fallback",
                    status="running",
                    content=(
                        "The selected model completed without user-facing "
                        "tokens; retrying once on the fallback chat model."
                    ),
                    metadata={"from_model": model_used, "to_model": _CHAT_FALLBACK_MODEL},
                )
                fallback_start = perf_counter()
                try:
                    async for fb in llm_service.stream_chat(
                        messages=last_generation_messages,
                        model=_CHAT_FALLBACK_MODEL,
                        overrides=None,
                        tools=None,
                    ):
                        if fb.get("content"):
                            assistant_content += fb["content"]
                            yield build_sse_chunk(
                                ChatChunk(
                                    type="token",
                                    content=fb["content"],
                                    conversation_id=str(conversation_id),
                                )
                            )
                    yield _record_trace_event(
                        lane="model_call",
                        title="Chat model fallback",
                        status="done" if assistant_content.strip() else "error",
                        content=(
                            "Fallback model generated an answer."
                            if assistant_content.strip()
                            else "Fallback model also returned no user-facing tokens."
                        ),
                        metadata={
                            "from_model": model_used,
                            "to_model": _CHAT_FALLBACK_MODEL,
                            "duration_s": perf_counter() - fallback_start,
                            "content_chars": len(assistant_content),
                        },
                    )
                except Exception as fallback_exc:
                    logger.error("Empty-stream fallback failed: %s", fallback_exc)
                    yield _record_trace_event(
                        lane="model_call",
                        title="Chat model fallback",
                        status="error",
                        content=f"Fallback model failed: {fallback_exc}",
                        metadata={"from_model": model_used, "to_model": _CHAT_FALLBACK_MODEL},
                    )

        if not assistant_content.strip():
            logger.error("LLM returned an empty assistant response for %s", conversation_id)
            yield _record_trace_event(
                lane="final",
                title="Assistant final answer",
                status="error",
                content="The model returned no user-facing answer after retrieval.",
                metadata={"model": model_used},
            )
            yield build_sse_chunk(
                ChatChunk(
                    type="error",
                    content=(
                        "The model did not return an answer after retrieval. "
                        "Please retry the question."
                    ),
                    conversation_id=str(conversation_id),
                )
            )
            return

        # Phase 15 — self_correct review pass:
        # draft has streamed; now ask the LLM to review. If errors found,
        # emit the critique as a `thinking` chunk, then stream the revision
        # as additional tokens. Transparent — user sees the correction.
        if reasoning_mode == "self_correct" and assistant_content.strip() and sources:
            try:
                from services.reasoning import self_correct_review

                revised, was_revised, issues = await self_correct_review(
                    query=request.message,
                    chunks=sources,
                    initial_answer=assistant_content,
                    model=model_used,
                )
                if was_revised:
                    critique = "; ".join(issues[:3])  # cap at first 3 issues for display
                    yield build_sse_chunk(
                        ChatChunk(
                            type="thinking",
                            thinking=f"⟳ Revising: {critique}",
                            conversation_id=str(conversation_id),
                        )
                    )
                    # Stream the revised answer as a second block of tokens.
                    # Simple chunk-by-chunk emission (no re-call to LLM — just
                    # send the revised text as tokens so the UI appends it).
                    yield build_sse_chunk(
                        ChatChunk(
                            type="token",
                            content=f"\n\n---\n**Revised answer:**\n\n{revised}",
                            conversation_id=str(conversation_id),
                        )
                    )
                    assistant_content = f"{assistant_content}\n\n---\n**Revised answer:**\n\n{revised}"
            except Exception as exc:
                logger.warning("self_correct post-pass failed (%s) — keeping draft", exc)

        # Phase 24 — collect skill/tool/reasoning trust signals for this turn
        skills_used_names = [s["name"] for s in active_skills_dicts]
        final_tools_used = list(tools_used_names)
        reasoning_cascade_applied = bool(analysis_text)
        yield _record_trace_event(
            lane="final",
            title="Assistant final answer",
            status="done",
            content=(
                "Final answer assembled and ready to persist. "
                f"content_chars={len(assistant_content)}"
            ),
            metadata={
                "model": model_used,
                "content_chars": len(assistant_content),
                "trace_events": len(trace_events) + 1,
            },
        )

        try:
            thinking_to_save = assistant_thinking.strip() or None
            await self._save_assistant_message(
                conversation_id,
                assistant_content,
                thinking_to_save,
                model_used,
                trimming_applied,
                chunks_returned=chunks_returned,
                strategy_used=strategy_used,
                query_profile_used=query_profile_used,
                reasoning_mode_used=reasoning_mode_used,
                hyde_applied=hyde_applied,
                agentic_mode_used=agentic_mode_used,
                downgrade_reason=downgrade_reason,
                collections_queried=collections_queried_for_msg,
                skills_used=skills_used_names,
                tools_used=final_tools_used,
                reasoning_cascade_applied=reasoning_cascade_applied,
                sources=sources,
                trace_events=trace_events,
            )
        except Exception as exc:
            logger.error("Failed to persist assistant message for %s: %s", conversation_id, exc)
            yield build_sse_chunk(
                ChatChunk(
                    type="error",
                    content=(
                        "The answer was generated, but the backend could not "
                        "save it. Please retry."
                    ),
                    conversation_id=str(conversation_id),
                )
            )
            return

        # Step 9: Send completion chunk — carries trust-signal fields so the
        # live UI renders the RetrievalBadge without waiting for a reload.
        yield build_sse_chunk(
            ChatChunk(
                type="done",
                conversation_id=str(conversation_id),
                model_used=model_used,
                chunks_returned=chunks_returned,
                strategy_used=strategy_used,
                query_profile_used=query_profile_used,
                reasoning_mode_used=reasoning_mode_used,
                hyde_applied=hyde_applied,
                agentic_mode_used=agentic_mode_used,
                downgrade_reason=downgrade_reason,
                collections_queried=collections_queried_for_msg,
                skills_used=skills_used_names,
                tools_used=final_tools_used,
                reasoning_cascade_applied=reasoning_cascade_applied,
            )
        )

        # Log completion — break the total into ttft / stream / tail so we can
        # tell a slow LLM apart from a blocking post-stream hook.
        done_emitted = perf_counter()
        elapsed = (datetime.utcnow() - start_time).total_seconds()
        ttft_s = (first_token_at - stream_start) if first_token_at else None
        stream_s = (stream_end - first_token_at) if (first_token_at and stream_end) else None
        tail_s = (done_emitted - stream_end) if stream_end else None
        ttft_str = f"{ttft_s:.2f}s" if ttft_s is not None else "n/a"
        stream_str = f"{stream_s:.2f}s" if stream_s is not None else "n/a"
        tail_str = f"{tail_s:.3f}s" if tail_s is not None else "n/a"
        logger.info(
            f"Chat completed conv={conversation_id} model={model_used} "
            f"total={elapsed:.2f}s ttft={ttft_str} stream={stream_str} tail={tail_str}"
        )

    # ── Phase 18 Query Profile ──────────────────────────────────────────────

    # Preset defaults. Profile is a speed preset that bundles retrieval_k +
    # rerank + HyDE. Individual overrides on ModelOverrides take precedence.
    _QUERY_PROFILE_PRESETS: dict[str, dict] = {
        "fast": {"retrieval_k": 10, "rerank_enabled": False, "hyde_enabled": False},
        # Pt10c — balanced now enables HyDE by default. Cross-domain
        # queries on heterogeneous libraries (e.g. "how does generative
        # AI apply to urban planning?") were producing wrong-domain
        # retrieval because the raw query embedding cosine-matched on
        # surface tokens like "design" instead of conceptual content.
        # HyDE generates a hypothetical answer first, embeds THAT, then
        # retrieves — which routes the search to the actually-relevant
        # documents. The ~1-2s latency cost is acceptable for a
        # knowledge-graph application where quality > speed.
        "balanced": {
            "retrieval_k": 40,
            "rerank_enabled": True,
            "hyde_enabled": True,
            "rerank_top_n": 24,
        },
        "thorough": {
            "retrieval_k": 60,
            "rerank_enabled": True,
            "hyde_enabled": True,
            "rerank_top_n": 32,
        },
    }

    async def _resolve_query_profile(
        self, request: ChatRequest, user_id: str | None = None
    ) -> dict:
        """
        Resolve Query Profile into concrete knobs. Returns a dict:
          retrieval_k, rerank_enabled, hyde_enabled,
          top_k_summary, rerank_top_n, similarity_threshold,
          neo4j_expansion_cap, max_corpora_per_query, fact_seed_limit

        Priority per knob:
          1. explicit per-request override on ModelOverrides
          2. profile preset (fast / balanced / thorough / custom)
          3. None where the preset doesn't specify (retriever falls back to
             its own defaults / hardcoded constants)

        "custom" profile loads the full RetrievalSettings object from the
        user's saved settings. `final_top_k` is intentionally global: Speed
        controls how wide the search/rerank pool is, while Final K controls
        how many chunks reach the LLM after that pool is ranked.
        """
        overrides = request.overrides
        hyde_explicit = bool(overrides and overrides.hyde_enabled is not None)
        profile_key = (
            overrides.query_profile if overrides and overrides.query_profile else "balanced"
        )

        # Defaults for the extra knobs — None means "let retriever decide"
        extras = {
            "top_k_summary": None,
            "rerank_top_n": None,
            "similarity_threshold": None,
            "neo4j_expansion_cap": None,
            "max_corpora_per_query": None,
            "final_top_k": None,
            "fact_seed_limit": None,
            "source_cap": None,
        }

        saved_retrieval_settings = None
        if user_id:
            try:
                gs = await settings_service.get_settings(user_id)
                saved_retrieval_settings = gs.retrieval
                extras["final_top_k"] = saved_retrieval_settings.final_top_k
                extras["source_cap"] = getattr(
                    saved_retrieval_settings, "source_cap", _CHAT_COVERAGE_SOURCE_CAP
                )
            except Exception as exc:
                logger.warning(
                    "Retrieval settings load failed for %s (%s) — "
                    "using profile defaults",
                    user_id,
                    exc,
                )

        if profile_key == "custom":
            preset = dict(self._QUERY_PROFILE_PRESETS["balanced"])  # safe fallback
            if saved_retrieval_settings is not None:
                rs = saved_retrieval_settings
                preset = {
                    "retrieval_k": rs.top_k_child,
                    "rerank_enabled": rs.rerank_enabled,
                    # HyDE stays a user-toggled concern regardless of custom
                    "hyde_enabled": False,
                }
                extras.update(
                    {
                        "top_k_summary": rs.top_k_summary,
                        "rerank_top_n": rs.rerank_top_n,
                        "similarity_threshold": rs.similarity_threshold,
                        "neo4j_expansion_cap": rs.neo4j_expansion_cap,
                        "max_corpora_per_query": rs.max_corpora_per_query,
                        "final_top_k": rs.final_top_k,
                        "fact_seed_limit": getattr(rs, "fact_seed_limit", 12),
                        "source_cap": getattr(
                            rs, "source_cap", _CHAT_COVERAGE_SOURCE_CAP
                        ),
                    }
                )
                logger.info(
                    "Custom profile resolved for user %s: k=%s rerank=%s thresh=%s final_k=%s",
                    user_id,
                    preset["retrieval_k"],
                    preset["rerank_enabled"],
                    extras["similarity_threshold"],
                    extras["final_top_k"],
                )
        else:
            preset = self._QUERY_PROFILE_PRESETS.get(
                profile_key, self._QUERY_PROFILE_PRESETS["balanced"]
            )

        for key in extras:
            if extras[key] is None and key in preset:
                extras[key] = preset[key]

        # Per-request overrides win on the three classic knobs
        retrieval_k = (
            overrides.retrieval_k
            if (overrides and overrides.retrieval_k is not None)
            else preset["retrieval_k"]
        )
        rerank_enabled = (
            overrides.rerank_enabled
            if (overrides and overrides.rerank_enabled is not None)
            else preset["rerank_enabled"]
        )
        hyde_enabled = (
            overrides.hyde_enabled
            if (overrides and overrides.hyde_enabled is not None)
            else preset["hyde_enabled"]
        )

        if overrides is not None:
            for key in (
                "top_k_summary",
                "rerank_top_n",
                "similarity_threshold",
                "neo4j_expansion_cap",
                "max_corpora_per_query",
                "final_top_k",
                "fact_seed_limit",
            ):
                value = getattr(overrides, key, None)
                if value is not None:
                    extras[key] = value

        # Mirror the HyDE decision onto request.overrides so _apply_hyde
        # sees the resolved value (preserves existing call contract).
        if request.overrides is None:
            request.overrides = ModelOverrides()
        if request.overrides.hyde_enabled is None:
            request.overrides.hyde_enabled = hyde_enabled

        return {
            "retrieval_k": retrieval_k,
            "rerank_enabled": bool(rerank_enabled),
            "hyde_enabled": bool(hyde_enabled),
            "hyde_explicit": hyde_explicit,
            "query_profile": profile_key,
            **extras,
        }

    async def _resolve_hyde_route(
        self,
        request: ChatRequest,
        user_id: str | None = None,
        *,
        fallback_model: str | None = None,
        fallback_api_base: str | None = None,
        fallback_api_key: str | None = None,
        fallback_extra: dict | None = None,
    ) -> dict[str, Any]:
        """Resolve the model used by the optional HyDE helper call.

        Dedicated HyDE pool config wins. If it is absent, HyDE inherits the
        already-resolved chat model, including pool/profile credentials.
        """
        overrides = request.overrides
        explicit_model = (overrides.hyde_model if overrides else None) or None
        if explicit_model:
            return {
                "model": explicit_model,
                "api_base": None,
                "api_key": None,
                "extra_params": None,
                "source": "request_override",
            }

        # Phase F — user-configured Settings -> Models -> HyDE card.
        if user_id:
            qres = await resolve_query_model_kind(user_id, "hyde")
            if qres:
                logger.info(
                    "HyDE — Phase F prefs resolution: user=%s → %s",
                    user_id,
                    qres["model"],
                )
                return {
                    "model": qres["model"],
                    "api_base": qres["api_base"],
                    "api_key": qres["api_key"],
                    "extra_params": qres["extra_params"] or None,
                    "source": "hyde_pool",
                }

        if fallback_model:
            return {
                "model": fallback_model,
                "api_base": fallback_api_base,
                "api_key": fallback_api_key,
                "extra_params": fallback_extra,
                "source": "active_chat_model",
            }

        return {
            "model": settings.HYDE_MODEL,
            "api_base": None,
            "api_key": None,
            "extra_params": None,
            "source": "env",
        }

    async def _apply_hyde(
        self,
        request: ChatRequest,
        user_id: str | None = None,
        *,
        hyde_explicit: bool = False,
        fallback_model: str | None = None,
        fallback_api_base: str | None = None,
        fallback_api_key: str | None = None,
        fallback_extra: dict | None = None,
        resolved_route: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """
        Phase 17 — Hypothetical Document Embeddings.

        When `overrides.hyde_enabled` is True, call a small/fast LLM to write
        a 2-3 sentence hypothetical answer to the user's question. Return
        that text (which will be embedded for Qdrant search) instead of the
        raw query.

        Returns:
            (retrieval_query, applied) — `applied` is True ONLY when the
            HyDE call succeeded and produced a non-empty hypothesis. Mere
            `hyde_enabled=True` is not sufficient — used for the trust-signal
            badge that distinguishes "asked for HyDE" from "actually ran HyDE".

        Model resolution order:
          1. request.overrides.hyde_model (per-request)
          2. Phase F — user query prefs `hyde_pool_id` → pool entry creds
          3. active chat model for this turn
          4. settings.HYDE_MODEL (server emergency default)

        On any failure (LLM down, malformed response), log a warning and
        fall back to the original query (applied=False).
        """
        overrides = request.overrides
        if not (overrides and overrides.hyde_enabled):
            return request.message, False
        source_constrained = _should_skip_hyde_for_query(request.message)
        if source_constrained and not hyde_explicit:
            logger.info(
                "HyDE skipped for source-constrained query: '%s'",
                request.message[:80],
            )
            return request.message, False
        if source_constrained:
            logger.info(
                "HyDE source-constrained guard bypassed by explicit toggle: '%s'",
                request.message[:80],
            )

        route = resolved_route or await self._resolve_hyde_route(
            request,
            user_id=user_id,
            fallback_model=fallback_model,
            fallback_api_base=fallback_api_base,
            fallback_api_key=fallback_api_key,
            fallback_extra=fallback_extra,
        )
        hyde_model = route.get("model") or settings.HYDE_MODEL
        hyde_api_base = route.get("api_base")
        hyde_api_key = route.get("api_key")
        hyde_extra = route.get("extra_params") or None

        failure_key = _hyde_failure_key(hyde_model, hyde_api_base)
        failed_at = _HYDE_FAILURE_CACHE.get(failure_key)
        if failed_at is not None:
            age = perf_counter() - failed_at
            if age < HYDE_FAILURE_TTL_SECONDS:
                logger.warning(
                    "HyDE skipped for %.0fs after endpoint failure "
                    "(model=%s api_base=%s). Falling back to raw query.",
                    HYDE_FAILURE_TTL_SECONDS - age,
                    hyde_model,
                    hyde_api_base or "(litellm default)",
                )
                return request.message, False
            _HYDE_FAILURE_CACHE.pop(failure_key, None)

        prompt = (
            "Write a concise, plausible 2-3 sentence answer to this question "
            "as if you already knew the answer. Focus on style and structure "
            "over accuracy — we'll search for the real sources after. Do not "
            "preface with 'The answer is' or similar; just write the answer.\n\n"
            f"Question: {request.message}"
        )
        start = perf_counter()
        try:
            # HyDE is a pre-retrieval helper, not the answer. Keep it on a
            # short leash so a slow/broken helper endpoint cannot dominate
            # the whole chat turn.
            hypothetical = await llm_service.complete_sync(
                messages=[{"role": "user", "content": prompt}],
                model=hyde_model,
                temperature=0.3,
                max_tokens=settings.HYDE_MAX_TOKENS,
                api_base=hyde_api_base,
                api_key=hyde_api_key,
                extra_params=hyde_extra,
                timeout=settings.HYDE_TIMEOUT_SECONDS,
            )
            hypothetical = (hypothetical or "").strip()
            if not hypothetical:
                logger.warning("HyDE returned empty output — using raw query")
                return request.message, False

            _HYDE_FAILURE_CACHE.pop(failure_key, None)
            logger.info(
                "HyDE active [model=%s duration=%.2fs]: query='%s' → hypothesis='%s'",
                hyde_model,
                perf_counter() - start,
                request.message[:80],
                hypothetical[:120],
            )
            return hypothetical, True
        except Exception as exc:
            _HYDE_FAILURE_CACHE[failure_key] = perf_counter()
            logger.warning(
                "HyDE call failed after %.2fs/%ss (model=%s api_base=%s) — "
                "%s: %s. "
                "Fix: set Settings → Models → HyDE to a working entry, or "
                "override HYDE_MODEL env to a pulled Ollama model / cloud "
                "model. Falling back to raw query.",
                perf_counter() - start,
                settings.HYDE_TIMEOUT_SECONDS,
                hyde_model,
                hyde_api_base or "(litellm default)",
                type(exc).__name__,
                exc,
            )
            return request.message, False

    def _resolve_reasoning(
        self, request: ChatRequest
    ) -> tuple[str | None, list[str] | None]:
        """
        Phase 15 resolution: per-request overrides > server default.
        Returns (mode, blend). Either can be None, which callers treat as 'none'.
        """
        mode: str | None = None
        blend: list[str] | None = None
        if request.overrides:
            mode = request.overrides.reasoning_mode or None
            blend = request.overrides.reasoning_blend or None
        # Server-side default (if no per-request value). Settings service seeds
        # AGENTIC_MODE_ENABLED etc. the same way; reasoning has no env var —
        # it's purely persisted per-user in ChatLLMSettings.default_reasoning_mode,
        # which is read on the frontend via settingsStore.loadFromAPI and sent
        # with every request. So if `mode` is None here, treat as "none".
        return mode, blend

    async def _load_or_create_conversation(
        self, request: ChatRequest
    ) -> tuple[ObjectId, ModelConfig, list[ChatMessage]]:
        """
        Load existing conversation or create new one.

        Args:
            request: ChatRequest with optional conversation_id

        Returns:
            Tuple of (conversation_id, model_config, existing_messages)
        """
        if request.conversation_id and ObjectId.is_valid(request.conversation_id):
            conv_id = ObjectId(request.conversation_id)
            conversation = await conversation_service.get_conversation(str(conv_id))
            if conversation and conversation.id:
                return (
                    ObjectId(conversation.id),
                    conversation.model_config_conversation,
                    conversation.messages,
                )
        # Create a new conversation if no valid ID provided
        model_config = ModelConfig()
        if request.overrides and request.overrides.model:
            model_config.model = request.overrides.model

        new_conv_id_str = await conversation_service.create_conversation(
            title=request.message[:50], model_config=model_config
        )
        return ObjectId(new_conv_id_str), model_config, []

    def _get_model_to_use(self, request: ChatRequest, model_config: ModelConfig) -> str:
        """
        Determine which model to use based on request overrides, agentic mode,
        or conversation config. Priority:
          1. explicit overrides.model (user-specified for this turn)
          2. agentic mode (per-request override or server-side default) → agentic_model
          3. conversation's configured model
        """
        if request.overrides and request.overrides.model:
            return request.overrides.model

        per_request_agentic = (
            request.overrides.agentic_mode if request.overrides else None
        )
        agentic_on = (
            per_request_agentic
            if per_request_agentic is not None
            else settings.AGENTIC_MODE_ENABLED
        )
        if agentic_on:
            if request.overrides and request.overrides.agentic_model:
                return request.overrides.agentic_model
            return settings.AGENTIC_MODEL

        # Phase 24 — defaults are empty everywhere now. Resolution chain:
        #   1. conversation's stored model (real value the user picked)
        #   2. settings.DEFAULT_COMPLETION_MODEL env (deployer-set)
        #   3. raise — user must configure a model
        # The legacy ollama/llama3.2:3b literal is treated as "unset" so
        # pre-Phase-24 conversations don't keep firing dead requests.
        LEGACY = {"ollama/llama3.2:3b", "ollama/qwen3:1.7b"}
        stored = (model_config.model or "").strip()
        if stored and stored not in LEGACY:
            return stored
        env_default = (settings.DEFAULT_COMPLETION_MODEL or "").strip()
        if env_default and env_default not in LEGACY:
            return env_default
        # Nothing configured — surface a clean error rather than silently
        # binding to a dead Ollama model.
        raise ValueError(
            "No chat model configured. Pick one in the chat header's model "
            "selector, or set DEFAULT_COMPLETION_MODEL in your .env."
        )

    def _create_user_message(self, message: str, model: str) -> ChatMessage:
        """Create a user message object without saving it."""
        return ChatMessage(
            role="user",
            content=message,
            token_count=count_tokens(message, model),
            created_at=datetime.utcnow(),
        )

    async def _trim_history(
        self, messages: list[ChatMessage], model: str
    ) -> tuple[list[ChatMessage], bool, str, int, int]:
        """
        Trim conversation history to fit context window.

        Returns:
            Tuple of (trimmed_messages, was_trimmed, details, tokens_used, tokens_max)
        """
        from utils.tokens import get_model_context_limit

        trim_result = context_manager.trim_history(
            messages=messages,
            model=model,
        )
        tokens_max = get_model_context_limit(model)

        return (
            trim_result.messages,
            trim_result.was_trimmed,
            trim_result.details,
            trim_result.tokens_after,
            tokens_max,
        )

    async def _save_assistant_message(
        self,
        conversation_id: ObjectId,
        content: str,
        thinking: str | None,
        model: str,
        trimming_applied: bool,
        *,
        chunks_returned: int | None = None,
        strategy_used: str | None = None,
        query_profile_used: str | None = None,
        reasoning_mode_used: str | None = None,
        hyde_applied: bool = False,
        agentic_mode_used: bool = False,
        downgrade_reason: str | None = None,
        collections_queried: list[str] | None = None,
        skills_used: list[str] | None = None,
        tools_used: list[str] | None = None,
        reasoning_cascade_applied: bool = False,
        sources: list[Any] | None = None,
        trace_events: list[dict[str, Any]] | None = None,
    ) -> ChatMessage:
        """Saves the assistant's final message to the database."""
        assistant_message = ChatMessage(
            role="assistant",
            content=content,
            thinking=thinking,
            trace_events=trace_events or [],
            model_used=model,
            token_count=count_tokens(content, model),
            created_at=datetime.utcnow(),
            trimming_applied=trimming_applied,
            collections_queried=collections_queried or [],
            chunks_returned=chunks_returned,
            sources=_compact_source_previews(sources),
            strategy_used=strategy_used,
            query_profile_used=query_profile_used,
            reasoning_mode_used=reasoning_mode_used,
            hyde_applied=hyde_applied,
            agentic_mode_used=agentic_mode_used,
            downgrade_reason=downgrade_reason,
            skills_used=skills_used or [],
            tools_used=tools_used or [],
            reasoning_cascade_applied=reasoning_cascade_applied,
        )

        saved = await conversation_service.append_message(
            str(conversation_id), assistant_message
        )
        if not saved:
            raise RuntimeError("conversation_service.append_message returned False")
        return assistant_message

    async def _load_tools(self, request: ChatRequest) -> tuple[list, list[dict]]:
        """Load tools and their schemas if any are selected.

        Phase 24 perf — when process_chat_request already fetched the tools
        in parallel with skills (for the trust-signal name list), it stashes
        the result on `request._tools_preloaded`. We reuse it here instead
        of issuing a duplicate Mongo round-trip.
        """
        web_search_enabled = _is_web_search_enabled_for_request(request)
        if not request.selected_tools and not web_search_enabled:
            return [], []

        preloaded = getattr(request, "_tools_preloaded", None)
        tools = (
            preloaded
            if preloaded is not None
            else await tool_registry.get_tools_by_ids(request.selected_tools or [])
        )
        tool_schemas = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]
        if web_search_enabled:
            tool_schemas.append(_web_search_tool_schema())
            tool_schemas.append(_fetch_page_tool_schema())
        if tool_schemas:
            tool_schemas.append(_response_tool_schema())
        return tools, tool_schemas

    async def _execute_tools(
        self,
        tool_calls: list,
        tools: list,
        request: ChatRequest | None = None,
    ) -> list[str]:
        """Execute tool calls and return results."""
        results = []
        for call in tool_calls:
            tool_name = call.get("function", {}).get("name")
            try:
                args = json.loads(call.get("function", {}).get("arguments", "{}"))
            except Exception as e:
                results.append(f"Error parsing tool arguments for {tool_name}: {e}")
                continue

            if tool_name == "web_search":
                results.append(await self._execute_web_search_tool(args, request))
                continue
            if tool_name == "fetch_page":
                results.append(await self._execute_fetch_page_tool(args, request))
                continue
            if tool_name == "response":
                results.append(json.dumps({"ok": True, "terminal": True}))
                continue

            tool = next((t for t in tools if t.name == tool_name), None)
            if not tool:
                results.append(f"Error: Tool '{tool_name}' not found.")
                continue

            try:
                result = tool_registry.execute_tool(tool.code, tool.name, args)
                results.append(str(result))
            except Exception as e:
                results.append(f"Error executing tool {tool_name}: {e}")
        return results

    async def _execute_web_search_tool(
        self,
        args: dict,
        request: ChatRequest | None = None,
    ) -> str:
        if not settings.LIVE_WEB_SEARCH_ENABLED:
            return json.dumps({"error": "web_search is disabled by the server"})

        raw_query_arg = args.get("query")
        query = " ".join(str(raw_query_arg or "").split()).strip()
        if (
            request is not None
            and request.message
            and (not query or query.lower() in {"true", "false", "null", "none"})
        ):
            query = " ".join(request.message.split()).strip()
        if not query:
            return json.dumps({"error": "query is required"})
        web_options = _resolve_web_evidence_options(request)
        try:
            max_results = int(
                args.get("max_results") or web_options["max_sources"]
            )
        except (TypeError, ValueError):
            max_results = int(web_options["max_sources"])
        max_results = max(1, min(max_results, int(web_options["max_sources"])))

        try:
            from services.web_freshness import (
                live_web_search,
                infer_web_search_time_range,
                refine_tool_search_query,
                rerank_web_source_chunks,
                web_hits_to_source_chunks,
            )
            from services.web_query_enrichment import (
                WebQueryEnrichmentResult,
                enrich_web_search_query,
            )

            skip_enrichment = bool(
                request is not None
                and getattr(request, "_skip_web_query_enrichment", False)
            )
            if skip_enrichment:
                base_query = refine_tool_search_query(
                    query,
                    request.message if request is not None else None,
                )
                builder_meta = (
                    getattr(request, "_web_query_builder", None)
                    if request is not None
                    else None
                )
                planner_meta = (
                    getattr(request, "_web_query_planner", None)
                    if request is not None
                    else None
                )
                enrichment = WebQueryEnrichmentResult(
                    query=base_query,
                    base_query=base_query,
                    applied=False,
                    attempted=False,
                    model=(
                        (builder_meta or {}).get("model")
                        if builder_meta is not None
                        else (planner_meta or {}).get("model")
                    ),
                    fallback_reason=(
                        "deterministic_web_query_builder_used"
                        if builder_meta is not None
                        else "native_web_planner_query_used"
                    ),
                )
            else:
                enrichment = await enrich_web_search_query(
                    tool_query=query,
                    original_query=request.message if request is not None else None,
                    user_id=getattr(request, "_user_id", None),
                    recent_messages=getattr(request, "_recent_chat_messages", None),
                )
            query = enrichment.query
            search_query = query[:300]
            candidate_limit = max(max_results, int(web_options["candidate_limit"]))
            prior_web_urls: set[str] = set()
            if request is not None and request.conversation_id:
                prior_web_urls = await conversation_service.get_recent_web_source_urls(
                    request.conversation_id
                )

            async def run_search_pass(pass_query: str) -> dict[str, Any]:
                pass_time_range = infer_web_search_time_range(pass_query)
                pass_hits = await live_web_search._search_live_web_pool(
                    pass_query,
                    max_results=candidate_limit,
                    time_range=pass_time_range,
                )
                pass_fetched, pass_fetch_stats, pass_hits_to_fetch, pass_web_pipeline = (
                    await live_web_search._fetch_pages_for_search(
                        search_query=pass_query,
                        hits=pass_hits,
                        max_results=max_results,
                        prior_web_urls=prior_web_urls,
                        fetch_depth=str(web_options["fetch_depth"]),
                        youtube_transcripts_enabled=bool(
                            web_options["youtube_transcripts"]
                        ),
                        max_fetch_pages=int(web_options["max_fetch_pages"]),
                    )
                )
                pass_fetch_stats_by_url = {
                    str(item.get("url")): item for item in pass_fetch_stats
                }
                pass_candidate_chunks = web_hits_to_source_chunks(
                    pass_hits,
                    fetched_markdown=pass_fetched,
                    fetch_stats_by_url=pass_fetch_stats_by_url,
                    search_query=pass_query,
                    max_chars=int(settings.OBSCURA_MAX_CHARS or 4000),
                )
                pass_chunks = await rerank_web_source_chunks(
                    pass_query,
                    pass_candidate_chunks,
                    limit=max_results,
                )
                pass_scores = _annotate_web_evidence_scores(pass_query, pass_chunks)
                pass_engine_errors = list(
                    dict.fromkeys(
                        str(error)
                        for hit in pass_hits
                        for error in (hit.engine_errors or ())
                        if str(error).strip()
                    )
                )
                pass_pipeline_for_grade = {
                    **pass_web_pipeline,
                    "full_page_fetch_successes": len(pass_fetched),
                    "full_page_fetch_attempts": len(pass_hits_to_fetch),
                }
                pass_sufficiency = _classify_web_evidence_sufficiency(
                    chunks=pass_chunks,
                    scores=pass_scores,
                    engine_errors=pass_engine_errors,
                    pipeline=pass_pipeline_for_grade,
                )
                return {
                    "search_query": pass_query,
                    "time_range": pass_time_range,
                    "hits": pass_hits,
                    "fetched": pass_fetched,
                    "fetch_stats": pass_fetch_stats,
                    "hits_to_fetch": pass_hits_to_fetch,
                    "web_pipeline": pass_web_pipeline,
                    "candidate_chunks": pass_candidate_chunks,
                    "chunks": pass_chunks,
                    "scores": pass_scores,
                    "engine_errors": pass_engine_errors,
                    "sufficiency": pass_sufficiency,
                }

            attempts: list[dict[str, Any]] = []
            selected_pass = await run_search_pass(search_query)
            attempts.append(selected_pass)

            first_grade = selected_pass["sufficiency"]["grade"]
            first_best = float(selected_pass["sufficiency"].get("best_score") or 0.0)
            first_count = int(selected_pass["sufficiency"].get("result_count") or 0)
            should_retry = (
                first_grade == "insufficient"
                or (first_grade == "partial" and first_best < 0.55)
                or (bool(selected_pass["engine_errors"]) and first_count < 3)
            )
            retry_query = None
            if should_retry:
                retry_query = _build_backend_retry_query(
                    search_query=search_query,
                    original_query=request.message if request is not None else None,
                )
            if retry_query and retry_query.lower() != search_query.lower():
                retry_pass = await run_search_pass(retry_query)
                attempts.append(retry_pass)

                grade_rank = {"insufficient": 0, "partial": 1, "confident": 2}

                def attempt_rank(item: dict[str, Any]) -> tuple[int, float, int]:
                    sufficiency = item["sufficiency"]
                    return (
                        grade_rank.get(str(sufficiency.get("grade")), 0),
                        float(sufficiency.get("best_score") or 0.0),
                        int(sufficiency.get("result_count") or 0),
                    )

                if attempt_rank(retry_pass) > attempt_rank(selected_pass):
                    selected_pass = retry_pass

            search_query = selected_pass["search_query"]
            time_range = selected_pass["time_range"]
            hits = selected_pass["hits"]
            fetched = selected_pass["fetched"]
            fetch_stats = selected_pass["fetch_stats"]
            hits_to_fetch = selected_pass["hits_to_fetch"]
            web_pipeline = selected_pass["web_pipeline"]
            candidate_chunks = selected_pass["candidate_chunks"]
            chunks = selected_pass["chunks"]
            evidence_scores = selected_pass["scores"]
            evidence_sufficiency = selected_pass["sufficiency"]
            engine_errors = selected_pass["engine_errors"]
            fetch_limit = len(hits_to_fetch)
            hits_by_url = {hit.url: hit for hit in hits}
            result_items = []
            for chunk in chunks:
                url = str((chunk.metadata or {}).get("url") or chunk.doc_id)
                hit = hits_by_url.get(url)
                metadata = chunk.metadata or {}
                result_items.append(
                    {
                        "title": (hit.title if hit else chunk.doc_name),
                        "url": url,
                        "content": _web_chunk_content_preview(chunk),
                        "snippet": (hit.snippet[:700] if hit else ""),
                        "published_date": (
                            hit.published_date if hit else (chunk.metadata or {}).get("published_date")
                        ),
                        "search_query": metadata.get("search_query"),
                        "time_range": metadata.get("time_range"),
                        "full_page_fetched": bool(
                            metadata.get("full_page_fetched")
                        ),
                        "evidence_mode": metadata.get("evidence_mode"),
                        "fetch_status": metadata.get("fetch_status"),
                        "fetch_method": metadata.get("fetch_method"),
                        "source_type": metadata.get("source_type"),
                        "transcript_status": metadata.get("transcript_status"),
                        "evidence_score": metadata.get("evidence_score"),
                        "engine_errors": metadata.get("engine_errors") or [],
                        "obscura_skipped_reason": metadata.get(
                            "obscura_skipped_reason"
                        ),
                        "cache_hit": bool(metadata.get("cache_hit")),
                        "content_chars": metadata.get("content_chars"),
                        "source_text_chars": metadata.get("source_text_chars"),
                        "source_text_max_chars": metadata.get("source_text_max_chars"),
                        "content_truncated": bool(
                            metadata.get("content_truncated")
                        ),
                        "rerank_text_max_chars": metadata.get("rerank_text_max_chars"),
                        "web_content_untrusted": True,
                    }
                )
            if request is not None and chunks:
                pending = list(getattr(request, "_pending_tool_sources", []) or [])
                pending = _append_deduped_web_sources(pending, chunks)
                pending = _cap_web_sources_for_turn(pending)
                object.__setattr__(request, "_pending_tool_sources", pending)

            search_queries = sorted(
                {
                    str(hit.search_query or search_query)
                    for hit in hits
                    if str(hit.search_query or search_query).strip()
                }
            )
            obscura_domains = [
                domain.strip()
                for domain in str(
                    getattr(settings, "LIVE_WEB_OBSCURA_DOMAINS", "") or ""
                ).split(",")
                if domain.strip()
            ]
            pipeline = {
                "web_search_calls_this_turn": 1,
                "web_search_call_limit": _MAX_WEB_SEARCH_CALLS_PER_TURN,
                "candidate_limit_requested": candidate_limit,
                "candidate_results": len(hits),
                "search_queries": search_queries[:12],
                "freshness_time_range": time_range,
                "fetch_depth": web_options["fetch_depth"],
                "research_mode": bool(web_options["research_mode"]),
                "youtube_transcripts_enabled": bool(
                    web_options["youtube_transcripts"]
                ),
                "requested_max_sources": web_options["requested_max_sources"],
                "snippet_rerank_applied": bool(
                    settings.LIVE_WEB_SEARCH_FETCH_FULL_PAGES
                    and len(hits) > len(hits_to_fetch)
                    and bool(hits_to_fetch)
                ),
                "snippet_rerank_fetch_limit": fetch_limit,
                "full_page_fetch_enabled": bool(
                    settings.LIVE_WEB_SEARCH_FETCH_FULL_PAGES
                ),
                "full_page_fetch_attempts": len(hits_to_fetch),
                "full_page_fetch_successes": len(fetched),
                "fetcher": getattr(settings, "LIVE_WEB_PAGE_FETCHER", "auto"),
                "fetches": fetch_stats[:10],
                "js_render": {
                    "configured": bool(settings.OBSCURA_COMMAND),
                    "policy": "allowlisted fallback after static extraction fails",
                    "allowlisted_domains": obscura_domains,
                    "attempted": any(
                        bool(item.get("obscura_attempted")) for item in fetch_stats
                    ),
                    "rendered": any(
                        bool(item.get("js_rendered")) for item in fetch_stats
                    ),
                },
                "final_chunk_candidates": len(candidate_chunks),
                "final_reranked_results": len(chunks),
                "final_result_limit": max_results,
                "ranked_by": settings.RERANKER_MODEL,
                "evidence_sufficiency": evidence_sufficiency,
                "source_scoring": {
                    "formula": (
                        "relevance*0.50 + completeness*0.25 + "
                        "intent_fit*0.15 + diversity_bonus - penalty"
                    ),
                    "scores": evidence_scores,
                },
                "backend_retry": {
                    "attempted": len(attempts) > 1,
                    "reason": (
                        "low_or_degraded_evidence"
                        if len(attempts) > 1
                        else "not_needed"
                    ),
                    "selected_query": search_query,
                    "attempts": [
                        {
                            "query": item["search_query"],
                            "grade": item["sufficiency"]["grade"],
                            "reason": item["sufficiency"]["reason"],
                            "best_score": item["sufficiency"]["best_score"],
                            "result_count": item["sufficiency"]["result_count"],
                            "candidate_results": len(item["hits"]),
                            "engine_error_count": len(item["engine_errors"]),
                        }
                        for item in attempts
                    ],
                },
                "engine_errors": engine_errors,
                "provider_counts": {
                    provider: sum(1 for hit in hits if hit.provider == provider)
                    for provider in sorted({hit.provider for hit in hits})
                },
                "wikipedia_result_count": sum(
                    1 for hit in hits if hit.provider == "wikipedia"
                ),
                "utility_query_enrichment": {
                    "attempted": enrichment.attempted,
                    "applied": enrichment.applied,
                    "model": enrichment.model,
                    "base_query": enrichment.base_query,
                    "prompt_version": enrichment.prompt_version,
                    "duration_ms": enrichment.duration_ms,
                    "history_user_messages_used": enrichment.history_user_messages_used,
                    "fallback_reason": enrichment.fallback_reason,
                },
                "web_query_planner": (
                    getattr(request, "_web_query_planner", None)
                    if request is not None
                    else None
                ),
                "web_query_builder": (
                    getattr(request, "_web_query_builder", None)
                    if request is not None
                    else None
                ),
            }
            pipeline.update(web_pipeline)
            if evidence_sufficiency.get("grade") == "insufficient":
                pipeline["search_health"] = "insufficient_evidence"
            elif engine_errors:
                pipeline["search_health"] = "degraded_search"
            elif not hits:
                pipeline["search_health"] = "failed_search"
            else:
                pipeline["search_health"] = "ok"
            logger.info(
                (
                    "web_search pipeline query=%r candidates=%d "
                    "fetch_attempts=%d fetch_successes=%d final=%d "
                    "time_range=%r js_rendered=%s snippet_only=%s "
                    "redis_search_cache_hit=%s redis_page_cache_hit=%s "
                    "utility_attempted=%s utility_applied=%s "
                    "utility_history_user_messages=%s planner_native_tool=%s "
                    "deterministic_web_builder=%s"
                ),
                search_query,
                len(hits),
                len(hits_to_fetch),
                len(fetched),
                len(chunks),
                time_range,
                pipeline["js_render"]["rendered"],
                pipeline.get("snippet_only"),
                pipeline.get("redis_search_cache_hit"),
                pipeline.get("redis_page_cache_hit"),
                enrichment.attempted,
                enrichment.applied,
                enrichment.history_user_messages_used,
                bool((pipeline.get("web_query_planner") or {}).get("native_tool_call")),
                bool((pipeline.get("web_query_builder") or {}).get("attempted")),
            )
            _record_web_evidence_run(
                request,
                {
                    "kind": "web_search",
                    "query": search_query,
                    "result_count": len(chunks),
                    "candidate_results": len(hits),
                    "engine_errors": engine_errors,
                    "degraded": bool(engine_errors)
                    or evidence_sufficiency.get("grade") != "confident",
                    "sufficiency": evidence_sufficiency,
                    "pipeline": pipeline,
                },
            )

            return json.dumps(
                {
                    "query": search_query,
                    "candidate_results": len(hits),
                    "reranked_results": len(chunks),
                    "ranked_by": settings.RERANKER_MODEL,
                    "freshness_time_range": time_range,
                    "full_page_fetch_enabled": bool(
                        settings.LIVE_WEB_SEARCH_FETCH_FULL_PAGES
                    ),
                    "pipeline": pipeline,
                    "evidence_sufficiency": evidence_sufficiency,
                    "results": result_items,
                    "note": (
                        "Use these only when relevant. Cite the URL for any "
                        "claim that depends on web results."
                    ),
                },
                ensure_ascii=False,
            )
        except Exception as e:
            logger.warning("web_search tool failed: %s", e)
            _record_web_evidence_run(
                request,
                {
                    "kind": "web_search",
                    "query": query,
                    "result_count": 0,
                    "candidate_results": 0,
                    "engine_errors": [str(e)],
                    "degraded": True,
                },
            )
            return json.dumps({"error": f"web_search failed: {e}"})

    async def _execute_fetch_page_tool(
        self,
        args: dict,
        request: ChatRequest | None = None,
    ) -> str:
        raw_url = " ".join(str(args.get("url") or "").split()).strip()
        reason = " ".join(str(args.get("reason") or "").split()).strip()
        if not raw_url:
            return json.dumps({"error": "url is required"})

        try:
            from services.web_freshness import _valid_web_url, live_web_search
        except Exception as exc:
            return json.dumps({"error": f"fetch_page unavailable: {exc}"})

        if not _valid_web_url(raw_url):
            return json.dumps({"error": "url must be http(s)", "url": raw_url})

        try:
            web_options = _resolve_web_evidence_options(request)
            result = await live_web_search._fetch_one_page_with_stats(
                raw_url,
                allow_obscura=str(web_options["fetch_depth"]) == "deep",
                youtube_transcripts_enabled=bool(
                    web_options["youtube_transcripts"]
                ),
            )
            content = (result.text or "").strip()
            payload = {
                "url": raw_url,
                "reason": reason,
                "status": result.status,
                "method": result.method,
                "fetch_depth": web_options["fetch_depth"],
                "chars": result.chars,
                "content_truncated": bool(
                    result.text and result.chars >= int(settings.OBSCURA_MAX_CHARS or 4000)
                ),
                "source_text_max_chars": int(settings.OBSCURA_MAX_CHARS or 4000),
                "cache_hit": bool(result.from_cache),
                "cache_layer": result.cache_layer,
                "source_type": result.source_type,
                "transcript_status": result.transcript_status,
                "obscura_attempted": bool(result.obscura_attempted),
                "obscura_rendered": bool(result.js_rendered),
                "obscura_skipped_reason": result.obscura_skipped_reason,
                "web_content_untrusted": True,
                "content": content,
                "note": (
                    "Use this fetched page only when relevant. Cite the URL "
                    "for claims that depend on it."
                ),
            }
            if request is not None and content:
                pending = list(getattr(request, "_pending_tool_sources", []) or [])
                pending = _append_deduped_web_sources(
                    pending,
                    [
                        SourceChunk(
                            chunk_id=f"web-fetch:{raw_url}",
                            parent_id=f"web-fetch:{raw_url}",
                            doc_id=raw_url,
                            corpus_id="live-web",
                            text=content,
                            score=1.0,
                            source_tier="web_search",
                            doc_name=raw_url,
                            metadata={
                                "url": raw_url,
                                "fetch_method": result.method,
                                "fetch_status": result.status,
                                "full_page_fetched": True,
                                "source_type": result.source_type,
                                "transcript_status": result.transcript_status,
                                "content_chars": result.chars,
                                "source_text_chars": result.chars,
                                "source_text_max_chars": int(settings.OBSCURA_MAX_CHARS or 4000),
                                "content_truncated": bool(
                                    result.text
                                    and result.chars >= int(settings.OBSCURA_MAX_CHARS or 4000)
                                ),
                                "obscura_attempted": bool(result.obscura_attempted),
                                "obscura_skipped_reason": result.obscura_skipped_reason,
                                "js_rendered": bool(result.js_rendered),
                                "retriever": "fetch_page",
                            },
                        )
                    ],
                )
                pending = _cap_web_sources_for_turn(pending)
                object.__setattr__(request, "_pending_tool_sources", pending)
            _record_web_evidence_run(
                request,
                {
                    "kind": "fetch_page",
                    "query": raw_url,
                    "result_count": 1 if content else 0,
                    "candidate_results": 1,
                    "engine_errors": [] if content else [result.status],
                    "degraded": not bool(content),
                    "pipeline": {
                        "fetch_depth": web_options["fetch_depth"],
                        "youtube_transcripts_enabled": bool(
                            web_options["youtube_transcripts"]
                        ),
                        "full_page_fetch_attempts": 1,
                        "full_page_fetch_successes": 1 if content else 0,
                        "js_render": {
                            "attempted": bool(result.obscura_attempted),
                            "rendered": bool(result.js_rendered),
                            "skipped_reason": result.obscura_skipped_reason,
                        },
                    },
                },
            )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.warning("fetch_page tool failed: %s", exc)
            _record_web_evidence_run(
                request,
                {
                    "kind": "fetch_page",
                    "query": raw_url,
                    "result_count": 0,
                    "candidate_results": 1,
                    "engine_errors": [str(exc)],
                    "degraded": True,
                },
            )
            return json.dumps({"error": f"fetch_page failed: {exc}", "url": raw_url})


# Global instance
chat_orchestrator = ChatOrchestrator()
