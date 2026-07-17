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
    ShelfReserveContext,
    matching_ingest_facets,
    matching_vector_facets,
    metadata_facet_terms,
    normalize_facet_id,
    select_facet_final,
)
from services.llm import llm_service
from services.retriever import retriever_orchestrator
from services.ingestion.section_classifier import is_noisy
from services.tool_registry import tool_registry

# Phase 24 perf — hoist hot-path imports to module level so each chat turn
# doesn't pay the import-resolution cost (was previously inside `try:` blocks
# in process_chat_request).
from services.skills_registry import skills_registry
from services.reasoning_cascade import analyze as reasoning_cascade_analyze
from services.query_model_resolver import (
    resolve as resolve_query_model_kind,
    resolve_by_entry_id,
    resolve_fallback_candidates,
)
from services.retriever.intent_policy import infer_retrieval_intent
from services.retriever.evidence_plan import (
    EvidenceLane,
    EvidencePlan,
    MULTI_CONCEPT_MIN_SOURCES,
    build_evidence_plan,
    build_evidence_plan_from_sides,
    evidence_lane_matches_text,
    evidence_plan_to_dict,
    parse_llm_sides,
)
from services.retriever.evidence_allocation import (
    STRONG_LANE_SCORE as _EVIDENCE_LANE_STRONG_SCORE,
    cap_chunks_per_doc as _ea_cap_chunks_per_doc,
    per_doc_cap_for_plan as _evidence_per_doc_cap_for_plan,
    relationship_allocation_eligible as _relationship_allocation_eligible,
    select_lane_support as _ea_select_lane_support,
)
from services.retriever.query_plan import FALLBACK_PROBE_ID as _FALLBACK_PROBE_ID
from services.answerability_tuning import (
    CROSS_DOCUMENT_RELATIONSHIP_ATOM as _CROSS_DOC_ATOM,
    answerability_policy_version as _answerability_policy_version,
    corpus_scope_v2_enabled as _answerability_corpus_scope_v2_enabled,
    corpus_scope_v2_support as _answerability_corpus_scope_v2_support,
    coverage_threshold as _answerability_coverage_threshold,
    cross_doc_atom_is_critical as _cross_doc_atom_is_critical,
    inject_cross_doc_atom as _inject_cross_doc_atom,
    lane_strong_score as _lane_strong_score,
    missing_is_relationship_only as _missing_is_relationship_only,
    relationship_lane_min_sources as _relationship_lane_min_sources,
    neutralize_relationship_critical as _neutralize_relationship_critical,
    partial_floor as _answerability_partial_floor,
    relationship_min_distinct_docs as _relationship_min_distinct_docs,
    rerank_evidence_support as _rerank_evidence_support,
    text_help_threshold as _answerability_text_help_threshold,
)
from services.retriever.excerpt import query_guided_excerpt as _query_guided_excerpt
from services.retriever.query_semantics import (
    GENERIC_CONCEPT_TOKENS,
    concept_groups,
    is_curated_concept,
    lexical_terms,
    required_atoms_for_query,
    required_operator_atoms,
    split_query_sides,
)
from services.retriever.query_plan import (
    build_query_plan_v2,
    contextualize_followup_query,
    query_plan_evidence_sides,
    query_plan_to_dict,
)
from services.retriever.librarian_planner import librarian_planner
from services.retriever.planned_fusion import reserved_required_lane_ids
from services.retriever.search_mode import resolve_search_mode
from services.retriever.temporal import (
    detect_temporal_intent,
    temporal_routing_enabled,
)
from services.settings import settings_service
from utils.streaming import build_sse_chunk
from utils.tokens import count_tokens, get_model_context_limit

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
# Pre-retrieval LLM decomposition (optional, flag-gated) emits ~300 tokens of
# JSON — longer than HyDE's ~2 sentences. The 8s HyDE budget silently truncated
# it to an empty string; even 25s clipped slower models. This is an opt-in,
# latency-tolerant quality step, so give it generous headroom. For lower
# latency, point the HyDE route at a fast JSON-reliable instruct model.
_EVIDENCE_LLM_DECOMPOSE_TIMEOUT = 15.0
# Support-chunk score cap (v4): supplementary facet/lane gap-fills sit below
# CE-confirmed evidence. Reserved side-seats are protected by curation.
_SUPPORT_SCORE_CAP = 0.50
# Thinking models occasionally return empty content; retry a bounded number of
# times before falling back to the deterministic plan.
_EVIDENCE_LLM_DECOMPOSE_ATTEMPTS = 2
# Total wall budget for the decomposition step. The racers run CONCURRENTLY
# and the step overlaps the main retrieval (launched as a background task,
# awaited only before the evidence-plan pass), so this deadline is nearly
# free — it exists to cap the tail (observed 26s turns when the old
# sequential 3x40s retry budget met a slow thinking model).
_EVIDENCE_LLM_DECOMPOSE_DEADLINE = 10.0
_CHAT_COVERAGE_MAX_DYNAMIC_SUPPLEMENTS = 4
_CHAT_COVERAGE_THRESHOLD = 4
_CHAT_COVERAGE_WEAK_THRESHOLD = 2
_CHAT_COVERAGE_SOURCE_CAP = 8
_PROMPT_COMPACTION_SOURCE_CHAR_STEPS = (1400, 950, 650, 450, 320)
# Max chunks any single document may contribute to the final context.
# 0 = DISABLED (uncapped). Reverted 2026-06-19: this hard cap was a band-aid for
# duplicate-driven over-concentration (the same book ingested twice as PDF + MD).
# The document-dedup pipeline (detect / prevent / correct, services/ingestion/
# dedup.py) removes that root cause, so an authoritative single source may again
# contribute as deeply as it ranks for. Set >0 to re-enable the per-doc ceiling
# (honored by both _cap_chunks_per_doc and select_facet_final).
_CHAT_PER_DOC_CAP = 0
# Per-facet coverage retrievals are independent and run concurrently; this caps
# the fan-out so a many-facet query can't swamp Qdrant/Mongo/the reranker at once.
_CHAT_COVERAGE_MAX_CONCURRENCY = 4
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
# Last-resort fallback when a user has no other enabled query-pool entry. Normal
# chat fallback is resolved dynamically from the user's encrypted model pool so
# an exhausted provider/account cannot strand a correct retrieval packet.
_CHAT_FALLBACK_MODEL = "deepseek/deepseek-chat"


async def _resolve_chat_fallback(
    user_id: str | None,
    *,
    primary_model: str,
    primary_entry_id: str | None,
) -> dict[str, Any] | None:
    """Return one safe configured fallback without exposing credentials."""
    try:
        candidates = await resolve_fallback_candidates(
            user_id,
            primary_model=primary_model,
            primary_entry_id=primary_entry_id,
            limit=1,
        )
    except Exception as exc:  # fallback resolution must not mask primary error
        logger.warning("Configured chat fallback resolution failed: %s", exc)
        candidates = []
    if candidates:
        return candidates[0]
    if primary_model != _CHAT_FALLBACK_MODEL:
        return {
            "entry_id": None,
            "provider": None,
            "model": _CHAT_FALLBACK_MODEL,
            "api_base": None,
            "api_key": None,
            "extra_params": None,
        }
    return None


# (Removed) The Phase-4 LLM "overview intent" second-chance classifier that
# silently upgraded local→global. Routing is now tier-authoritative: GLOBAL is
# an explicit user choice only (see resolve_search_mode), so the classifier and
# its helpers (_INTENT_CACHE / _INTENT_SYSTEM / _OVERVIEW_CLASSIFIER_HINTS /
# _SIMPLE_DEFINITION_RE) were removed.
_CHAT_EVIDENCE_MIN_KEEP_AFTER_FILTER = 4
_RAW_TOOL_REQUEST_MARKERS = (
    "<｜｜dsml｜｜tool_calls",
    "<tool_calls",
    "tool_calls>",
    "invoke name=",
    '"tool_calls"',
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
_BROAD_SPECTRUM_QUERY_RE = re.compile(
    r"\b("
    r"full\s+spectrum|whole\s+picture|big\s+picture|broad\s+(?:view|overview|survey|map)|"
    r"overview|survey|map\s+(?:my|the|this)\s+(?:corpus|library|books|documents)|"
    r"across\s+(?:my|the|this|all)\s+(?:corpus|library|books|documents|sources)|"
    r"entire\s+(?:corpus|library|set|collection)|all\s+(?:books|documents|sources)|"
    r"themes?\s+across|landscape|taxonomy|synthesize\s+everything"
    r")\b",
    re.IGNORECASE,
)

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
    if ("what is" in text or "define " in text or "explain " in text) and any(
        marker in text for marker in _HYDE_SPECIFIC_RELATION_MARKERS
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


def _source_allowed_by_corpus_scope(source: Any, corpus_ids: list[str] | None) -> bool:
    """Final evidence guard: selected corpora own corpus evidence authority.

    Retrieval layers already apply corpus filters, but this last-mile check keeps
    merged support/tool paths from accidentally passing deselected corpus chunks
    into the final prompt, SSE source bundle, or persisted assistant message.
    Web/tool evidence remains allowed because it is not corpus evidence.
    """

    if not corpus_ids:
        return True
    data = _source_to_dict(source)
    if not data:
        return False
    if _is_web_source_data(data):
        return True
    return str(data.get("corpus_id") or "").strip() in set(corpus_ids)


def _filter_sources_to_selected_corpora(
    sources: list[Any] | None,
    corpus_ids: list[str] | None,
) -> list[Any]:
    if not sources:
        return []
    return [
        source
        for source in sources
        if _source_allowed_by_corpus_scope(source, corpus_ids)
    ]


def _filter_facts_to_selected_corpora(
    facts: list[Any] | None,
    corpus_ids: list[str] | None,
) -> list[Any]:
    if not facts:
        return []
    if not corpus_ids:
        return list(facts)
    allowed = {str(cid) for cid in corpus_ids}
    filtered: list[Any] = []
    for fact in facts:
        if hasattr(fact, "model_dump"):
            data = fact.model_dump(mode="json")
        elif isinstance(fact, dict):
            data = fact
        else:
            data = vars(fact) if hasattr(fact, "__dict__") else {}
        if str(data.get("corpus_id") or "").strip() in allowed:
            filtered.append(fact)
    return filtered


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

    seen = {key for source in existing if (key := _web_source_key(source))}
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


def _planned_store_names(tier: Any) -> list[str]:
    value = str(getattr(tier, "value", tier) or "")
    if value == RetrievalTier.qdrant_only.value:
        return ["qdrant_vectors"]
    if value == RetrievalTier.qdrant_mongo_graph.value:
        return ["qdrant_vectors", "mongo_lexical_hydration", "neo4j_graph"]
    if value == RetrievalTier.qdrant_mongo.value:
        return ["qdrant_vectors", "mongo_lexical_hydration"]
    return ["selected_retrieval_tier"]


def _operator_labels_for_query(query: str | None) -> list[str]:
    atoms = required_operator_atoms(query)
    labels: list[str] = []
    if "definition" in atoms:
        labels.append("definition")
    if "relationship" in atoms:
        labels.append("relationship")
    if "procedure" in atoms:
        labels.append("procedure")
    if "methods_tasks" in atoms:
        labels.append("methods/tasks")
    return labels


def _build_chat_query_plan(
    *,
    query: str,
    retrieval_query: str,
    requested_tier: Any,
    corpus_ids: list[str] | None,
    collections: list[str] | None,
    profile_cfg: dict[str, Any],
    search_mode: str,
    hyde_applied: bool,
    librarian_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic query plan used for trace and downstream guardrails."""

    intent = infer_retrieval_intent(query)
    semantic_query = retrieval_query or query
    concepts = [
        {"key": group.key, "aliases": list(group.aliases[:8])}
        for group in concept_groups(semantic_query, max_groups=8)
    ]
    tier_value = str(getattr(requested_tier, "value", requested_tier) or "")
    corpus_count = len(corpus_ids or [])
    query_plan_v2 = build_query_plan_v2(
        query,
        corpus_ids=corpus_ids,
        standalone_query=semantic_query,
    )
    evidence_plan = build_evidence_plan(semantic_query)
    if settings.QUERY_PLAN_V2:
        v2_sides = query_plan_evidence_sides(query_plan_v2)
        if v2_sides:
            evidence_plan = build_evidence_plan_from_sides(
                semantic_query,
                v2_sides,
                allow_single=True,
            )
    plan = {
        "query": query,
        "retrieval_query": retrieval_query,
        "query_rewritten": bool(retrieval_query != query),
        "hyde_applied": bool(hyde_applied),
        "requested_tier": tier_value,
        "search_mode": str(search_mode or "auto"),
        "stores": _planned_store_names(requested_tier),
        "corpus_scope": "selected_corpora" if corpus_count else "no_corpus_selected",
        "corpus_count": corpus_count,
        "collection_count": len(collections or []),
        "concepts": concepts,
        "lexical_terms": lexical_terms(query)[:12],
        "operators": _operator_labels_for_query(query),
        "required_atoms": sorted(required_atoms_for_query(query, max_concepts=4)),
        "evidence_plan": evidence_plan_to_dict(evidence_plan),
        "query_plan_v2": query_plan_to_dict(query_plan_v2),
        "query_plan_v2_mode": (
            "active"
            if settings.QUERY_PLAN_V2
            else "shadow"
            if settings.QUERY_PLAN_V2_SHADOW
            else "disabled"
        ),
        "intent": {
            "need": intent.need.value,
            "broad_score": intent.broad_score,
            "specific_score": intent.specific_score,
            "child_ratio": intent.child_ratio,
            "summary_ratio": intent.summary_ratio,
        },
        "budget": {
            "profile": profile_cfg.get("query_profile"),
            "retrieval_k": profile_cfg.get("retrieval_k"),
            "top_k_summary": profile_cfg.get("top_k_summary"),
            "rerank_enabled": bool(profile_cfg.get("rerank_enabled")),
            "rerank_top_n": profile_cfg.get("rerank_top_n"),
            "final_top_k": profile_cfg.get("final_top_k"),
            "source_cap": profile_cfg.get("source_cap"),
        },
        "answerability_policy": (
            "enforce_retrieved_evidence"
            if corpus_count
            else "general_chat_no_corpus_gate"
        ),
    }
    if librarian_plan is not None:
        plan["librarian_query_plan"] = librarian_plan
    return plan


async def _build_librarian_plan_trace(
    *,
    query: str,
    corpus_ids: list[str] | None,
    requested_tier: Any,
    enabled: bool,
    shadow: bool,
    planner_service: Any = None,
    db: Any = None,
    embedding_config_loader: Any = None,
) -> dict[str, Any] | None:
    """Build L1/L2 trace metadata without feeding it into retrieval."""

    if not enabled and not shadow:
        return None
    planner_service = planner_service or librarian_planner
    db = conversation_service._db if db is None else db
    embedding_config_loader = (
        embedding_config_loader or retriever_orchestrator._embedding_config_for_query
    )
    try:
        embedding_config = await embedding_config_loader(corpus_ids)
        result = await asyncio.wait_for(
            planner_service.build(
                query,
                corpus_ids=corpus_ids,
                requested_tier=requested_tier,
                db=db,
                embedding_config=embedding_config,
            ),
            timeout=2.0,
        )
        return {
            "mode": "enabled_pending_l3" if enabled else "shadow",
            "behavior_applied": False,
            "plan": result.plan.model_dump(mode="json"),
            "diagnostics": dict(result.diagnostics),
        }
    except Exception as exc:  # noqa: BLE001 - shadow failures are traced, not hidden
        return {
            "mode": "enabled_pending_l3" if enabled else "shadow",
            "behavior_applied": False,
            "plan": None,
            "diagnostics": {
                "status": "degraded",
                "reason": f"{type(exc).__name__}: {exc}"[:300],
                "provider_calls": 0,
            },
        }


def _format_chat_query_plan_trace(plan: dict[str, Any]) -> str:
    concepts = [
        str(item.get("key") or "").strip()
        for item in plan.get("concepts") or []
        if isinstance(item, dict) and str(item.get("key") or "").strip()
    ]
    budget = plan.get("budget") if isinstance(plan.get("budget"), dict) else {}
    intent = plan.get("intent") if isinstance(plan.get("intent"), dict) else {}
    evidence_plan = (
        plan.get("evidence_plan") if isinstance(plan.get("evidence_plan"), dict) else {}
    )
    evidence_lanes = [
        str(lane.get("name") or "").strip()
        for lane in (evidence_plan.get("lanes") or [])
        if isinstance(lane, dict) and str(lane.get("name") or "").strip()
    ]
    lines = [
        "[Query plan]",
        (
            "search: "
            f"scope={plan.get('corpus_scope')} "
            f"corpora={plan.get('corpus_count')} "
            f"tier={plan.get('requested_tier')} "
            f"mode={plan.get('search_mode')}"
        ),
        f"stores: {', '.join(plan.get('stores') or []) or 'none'}",
        f"intent: {intent.get('need') or 'balanced'}",
        f"concepts: {', '.join(concepts) if concepts else 'none detected'}",
        (
            "evidence_lanes: "
            f"{', '.join(evidence_lanes) if evidence_lanes else 'none'}"
        ),
        ("operators: " f"{', '.join(plan.get('operators') or []) or 'none'}"),
        (
            "required_evidence: "
            f"{', '.join(plan.get('required_atoms') or []) or 'none'}"
        ),
        (
            "budget: "
            f"profile={budget.get('profile')} "
            f"k={budget.get('retrieval_k')} "
            f"summaries={budget.get('top_k_summary')} "
            f"rerank={'on' if budget.get('rerank_enabled') else 'off'} "
            f"rerank_top_n={budget.get('rerank_top_n')} "
            f"final={budget.get('final_top_k')}"
        ),
        f"answerability: {plan.get('answerability_policy')}",
    ]
    librarian = (
        plan.get("librarian_query_plan")
        if isinstance(plan.get("librarian_query_plan"), dict)
        else None
    )
    if librarian is not None:
        librarian_artifact = (
            librarian.get("plan") if isinstance(librarian.get("plan"), dict) else {}
        )
        lines.append(
            "librarian: "
            f"mode={librarian.get('mode')} "
            f"shape={librarian_artifact.get('shape') or 'unavailable'} "
            f"hash={librarian_artifact.get('plan_hash') or 'unavailable'} "
            "behavior=shadow_only"
        )
    if plan.get("query_rewritten"):
        lines.append("query_rewrite: retrieval query differs from user query")
    return "\n".join(lines)


def _as_answerability_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _selection_sufficiency_from_diagnostics(
    diagnostics: dict[str, Any] | None,
    sources: list[SourceChunk] | None = None,
) -> dict[str, Any] | None:
    diag = diagnostics if isinstance(diagnostics, dict) else {}
    selection = diag.get("selection") if isinstance(diag.get("selection"), dict) else {}
    sufficiency = (
        selection.get("sufficiency")
        if isinstance(selection.get("sufficiency"), dict)
        else None
    )
    if sufficiency:
        return dict(sufficiency)

    for source in sources or []:
        metadata = getattr(source, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        value = metadata.get("answer_sufficiency")
        if isinstance(value, dict):
            return dict(value)
    return None


def _atoms_covered_by_source_text(
    atoms: list[str], sources: list[SourceChunk] | None
) -> set[str]:
    """Required CONCEPT atoms whose term literally appears in the retrieved text.

    The facet/concept coverage signal is metadata-based: it misses chunks that
    answer the question in their TEXT but were never tagged with that concept
    (unfamiliar / fictional entities the extractor never saw). This lexical pass
    recovers them. Operator atoms (definition/relationship/…) and the
    cross-document atom are not lexical terms, so they are skipped.
    """

    raw = " ".join(str(getattr(s, "text", "") or "") for s in (sources or []))
    # Normalise punctuation to spaces so "eggs." matches the term "eggs".
    haystack = re.sub(r"[^a-z0-9]+", " ", raw.lower())
    if not haystack.strip():
        return set()
    padded = f" {haystack} "
    covered: set[str] = set()
    for atom in atoms or []:
        a = str(atom)
        if not a.startswith("concept:"):
            continue
        term = re.sub(r"[^a-z0-9]+", " ", a.split(":", 1)[1].strip().lower()).strip()
        if len(term) >= 3 and f" {term} " in padded:
            covered.add(a)
    return covered


def _build_retrieval_answerability_gate(
    *,
    query: str,
    diagnostics: dict[str, Any] | None,
    sources: list[SourceChunk] | None,
    facts: list[Any] | None,
    corpus_ids: list[str] | None,
    web_search_enabled: bool,
    evidence_plan_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert retriever sufficiency diagnostics into a chat-time contract."""

    expected_atoms = sorted(required_atoms_for_query(query, max_concepts=4))
    sufficiency = _selection_sufficiency_from_diagnostics(diagnostics, sources) or {}
    required_atoms = sorted(
        str(atom)
        for atom in (sufficiency.get("required_atoms") or expected_atoms)
        if str(atom).strip()
    )
    covered_atoms = sorted(
        str(atom)
        for atom in (sufficiency.get("covered_required_atoms") or [])
        if str(atom).strip()
    )
    missing_atoms = sorted(
        str(atom)
        for atom in (
            sufficiency.get("missing_atoms")
            if sufficiency.get("missing_atoms") is not None
            else [atom for atom in required_atoms if atom not in set(covered_atoms)]
        )
        if str(atom).strip()
    )
    missing_critical = sorted(
        str(atom)
        for atom in (sufficiency.get("missing_critical_atoms") or [])
        if str(atom).strip()
    )
    required_coverage = _as_answerability_float(
        sufficiency.get("required_coverage"),
        1.0 if not required_atoms else 0.0,
    )
    corpus_scoped = bool(corpus_ids)
    evidence_count = len(sources or []) + len(facts or [])
    raw_answerable = bool(sufficiency.get("answerable"))

    plan_meta = evidence_plan_meta if isinstance(evidence_plan_meta, dict) else {}
    if plan_meta.get("active"):
        required_set = set(required_atoms)
        covered_set = set(covered_atoms)
        missing_set = set(missing_atoms)
        critical_set = set(missing_critical)
        required_lanes = {
            str(name)
            for name in (plan_meta.get("required_lanes") or [])
            if str(name).strip()
        }
        covered_lanes = {
            str(name)
            for name in (plan_meta.get("covered_lanes") or [])
            if str(name).strip()
        }
        missing_lanes = {
            str(name)
            for name in (plan_meta.get("missing_lanes") or [])
            if str(name).strip()
        }
        final_plan = (
            plan_meta.get("final") if isinstance(plan_meta.get("final"), dict) else {}
        )
        plan_payload = (
            plan_meta.get("plan") if isinstance(plan_meta.get("plan"), dict) else {}
        )
        plan_operators = {
            str(operator)
            for operator in (plan_payload.get("operators") or [])
            if str(operator).strip()
        }
        relationship_plan = (
            "relationship" in plan_operators
            or "relationship" in required_set
            or str(plan_payload.get("mode") or plan_meta.get("mode") or "")
            == "multi_concept_relationship"
        )
        distinct_doc_count = int(final_plan.get("distinct_doc_count") or 0)
        for lane_name in required_lanes:
            required_set.add(f"concept:{lane_name}")
        for lane_name in covered_lanes:
            atom = f"concept:{lane_name}"
            required_set.add(atom)
            covered_set.add(atom)
            missing_set.discard(atom)
            critical_set.discard(atom)
        for lane_name in missing_lanes:
            atom = f"concept:{lane_name}"
            required_set.add(atom)
            missing_set.add(atom)
            critical_set.add(atom)
        if relationship_plan and len(required_lanes) >= 2 and _inject_cross_doc_atom():
            cross_doc_atom = _CROSS_DOC_ATOM
            required_set.add(cross_doc_atom)
            bridge_thin = bool(missing_lanes) or distinct_doc_count < min(
                _relationship_min_distinct_docs(), len(required_lanes)
            )
            if bridge_thin:
                missing_set.add(cross_doc_atom)
                # The corpus lacks an explicit cross-document link. Only strict
                # mode treats that as a refusal; lenient/off trust the LLM to
                # bridge the grounded sides with its own reasoning.
                if _cross_doc_atom_is_critical():
                    critical_set.add(cross_doc_atom)
                else:
                    critical_set.discard(cross_doc_atom)
            else:
                covered_set.add(cross_doc_atom)
                missing_set.discard(cross_doc_atom)
                critical_set.discard(cross_doc_atom)
        missing_set.update(required_set - covered_set)
        missing_set -= covered_set
        critical_set &= missing_set
        # Soften relationship-family criticality (cross-doc bridge + the bare
        # "relationship" operator) unless RELATIONSHIP_GATE=strict. Concept
        # lanes (a side with zero evidence) and definition/procedure stay
        # critical — that is the honesty floor the LLM must not paper over.
        critical_set = _neutralize_relationship_critical(critical_set)
        required_atoms = sorted(required_set)
        covered_atoms = sorted(covered_set & required_set)
        missing_atoms = sorted(missing_set)
        missing_critical = sorted(critical_set)
        required_coverage = (
            len(covered_set & required_set) / len(required_set) if required_set else 1.0
        )

    # Text-answerability fallback: count a required concept as covered when its
    # term actually appears in the retrieved source text, even if no facet was
    # tagged for it. Only ADDS coverage, so it can never cause a refusal — it
    # just stops the gate from discarding a chunk that visibly answers the
    # question (the metadata-vs-text gap).
    text_help = False
    if required_atoms and sources:
        text_covered = _atoms_covered_by_source_text(missing_atoms, sources)
        if text_covered:
            text_help = True
            required_set = set(required_atoms)
            covered_set = (set(covered_atoms) | text_covered) & required_set
            covered_atoms = sorted(covered_set)
            missing_atoms = sorted(required_set - covered_set)
            missing_critical = [a for a in missing_critical if a not in covered_set]
            required_coverage = (
                len(covered_set) / len(required_set) if required_set else 1.0
            )

    answer_shape = (
        str((diagnostics or {}).get("answer_shape") or "")
        if isinstance(diagnostics, dict)
        else ""
    )
    _cov_floor = _answerability_coverage_threshold(answer_shape or None)
    _text_floor = _answerability_text_help_threshold()
    _partial_floor = _answerability_partial_floor()
    effective_answerable = (
        raw_answerable
        or (required_coverage >= _cov_floor and not missing_critical)
        # When the retrieved TEXT covers a majority of the query concepts and
        # nothing critical is missing, answer: the remaining uncovered atoms are
        # generic question words (how/many/happens/reads) the corpus need not
        # "establish" — the substantive terms are present in the evidence.
        or (required_coverage >= _text_floor and text_help and not missing_critical)
    )

    if not corpus_scoped:
        status = "not_enforced"
    elif evidence_count <= 0:
        status = "unanswerable"
    elif effective_answerable:
        status = "answerable"
    elif (
        _missing_is_relationship_only(missing_critical)
        and required_coverage >= _partial_floor
    ):
        # The ONLY thing missing is the cross-document relationship bridge — the
        # grounded sides are present and the LLM can synthesize the link. Answer
        # with a "synthesized across sources" caveat instead of refusing. This
        # carve-out also softens strict mode when coverage is healthy.
        status = "partial"
    elif missing_critical:
        status = "unanswerable"
    elif required_coverage >= _partial_floor:
        status = "partial"
    else:
        status = "weak"

    scope_support = _answerability_corpus_scope_v2_support(
        query,
        [_answerability_chunk_text(source) for source in (sources or [])],
    )
    scope_guard_applied = bool(
        _answerability_corpus_scope_v2_enabled()
        and corpus_scoped
        and evidence_count > 0
        and not raw_answerable
        and status in {"answerable", "partial"}
        and scope_support.get("eligible")
        and not scope_support.get("supported")
    )
    if scope_guard_applied:
        status = "unanswerable"

    return {
        "status": status,
        "answerable": status == "answerable",
        "raw_answerable": raw_answerable,
        "corpus_scoped": corpus_scoped,
        "web_search_enabled": bool(web_search_enabled),
        "evidence_count": evidence_count,
        "source_count": len(sources or []),
        "fact_count": len(facts or []),
        "required_atoms": required_atoms,
        "covered_required_atoms": covered_atoms,
        "missing_atoms": missing_atoms,
        "missing_critical_atoms": missing_critical,
        "required_coverage": round(required_coverage, 4),
        "answerability_policy_version": _answerability_policy_version(),
        "corpus_scope_guard": {
            **scope_support,
            "applied": scope_guard_applied,
            "reason": (
                "retriever_insufficient_distinctive_scope_undercovered"
                if scope_guard_applied
                else "not_applied"
            ),
        },
        # P0.4 — lane coverage is telemetry, answerability is the decision;
        # surface them separately so UI/MCP can render both without conflation.
        "lane_coverage": (
            (diagnostics or {}).get("selection", {}).get("lane_coverage")
            if isinstance(diagnostics, dict)
            and isinstance((diagnostics or {}).get("selection"), dict)
            else None
        ),
        "answer_shape": answer_shape or None,
        "coverage_threshold": round(_cov_floor, 4),
        "diagnostic_source": (
            "retriever_sufficiency+evidence_plan"
            if sufficiency and plan_meta.get("active")
            else "evidence_plan"
            if plan_meta.get("active")
            else "retriever_sufficiency"
            if sufficiency
            else "fallback"
        ),
        "evidence_plan": plan_meta,
    }


def _format_retrieval_answerability_trace(gate: dict[str, Any]) -> str:
    status = str(gate.get("status") or "unknown")
    missing = [str(atom) for atom in gate.get("missing_atoms") or []]
    covered = [str(atom) for atom in gate.get("covered_required_atoms") or []]
    rule = (
        "normal synthesis"
        if status == "answerable"
        else "answer only supported parts and caveat missing evidence"
    )
    if status == "not_enforced":
        rule = "no selected corpus; normal chat path"
    if gate.get("web_search_enabled") and status != "answerable":
        rule += "; web/tool evidence may repair gaps"
    return "\n".join(
        [
            "[Answerability gate]",
            f"status: {status}",
            f"coverage: {gate.get('required_coverage')}",
            ("required: " f"{', '.join(gate.get('required_atoms') or []) or 'none'}"),
            f"covered: {', '.join(covered) if covered else 'none'}",
            f"missing: {', '.join(missing) if missing else 'none'}",
            f"rule: {rule}",
        ]
    )


def _format_retrieval_answerability_prompt_note(
    gate: dict[str, Any] | None,
) -> str | None:
    if not gate or not gate.get("corpus_scoped"):
        return None
    status = str(gate.get("status") or "unknown")
    required = ", ".join(gate.get("required_atoms") or []) or "none"
    covered = ", ".join(gate.get("covered_required_atoms") or []) or "none"
    missing = ", ".join(gate.get("missing_atoms") or []) or "none"
    lines = [
        "Internal retrieval answerability contract (do not mention this block):",
        f"- Required evidence atoms for the user's query: {required}.",
        f"- Retrieved evidence coverage: {status}; covered={covered}; missing={missing}.",
    ]
    if status == "answerable":
        lines.append(
            "- The selected corpus evidence covers the query-level requirements. "
            "Answer directly from the retrieved sources and synthesize across them."
        )
    elif status == "partial":
        lines.append(
            "- The selected corpus evidence is partial. Answer only the parts "
            "the retrieved sources support, and mark the missing part as not "
            "established by the retrieved sources."
        )
    else:
        lines.append(
            "- HARD LIMIT: the selected corpus retrieval is not sufficient to "
            "answer the user's full question as a source-backed claim."
        )
        lines.append(
            "- Say briefly what the selected sources did not establish, name "
            "the missing concept or relationship in ordinary language, and "
            "suggest broadening corpus/search scope only if that would help."
        )
    if gate.get("web_search_enabled") and status != "answerable":
        lines.append(
            "- If web/tool evidence is gathered later and covers the missing "
            "atoms, you may use that new evidence. Otherwise keep the caveat."
        )
    lines.append(
        "- Do not fill missing corpus evidence with generic knowledge while "
        "presenting it as retrieved or source-backed."
    )
    return "\n".join(lines)


def _should_short_circuit_answerability(
    gate: dict[str, Any] | None,
    *,
    web_search_enabled: bool,
    selected_tools: list[str] | None,
) -> bool:
    if not gate or not gate.get("corpus_scoped"):
        return False
    if web_search_enabled or selected_tools:
        return False
    return str(gate.get("status") or "") in {"unanswerable", "weak"}


def _friendly_missing_atom(atom: str) -> str:
    text = str(atom or "").strip()
    if text == f"concept:{_FALLBACK_PROBE_ID}":
        # Internal synthetic catch-all lane; never leak plumbing ids into
        # user-facing refusal text (P0.4).
        return "the main subject of the question"
    if text.startswith("concept:"):
        return text.split(":", 1)[1].replace("_", " ")
    if text == "methods_tasks":
        return "methods or examples"
    if text == "cross_document_relationship_evidence":
        return "evidence from both sides of the relationship"
    return text.replace("_", " ")


def _format_answerability_short_circuit_response(
    gate: dict[str, Any],
    *,
    query: str,
    sources: list[SourceChunk] | None = None,
) -> str:
    missing = [
        _friendly_missing_atom(atom)
        for atom in (gate.get("missing_atoms") or [])
        if str(atom).strip()
    ]
    missing_text = ", ".join(dict.fromkeys(missing)) or "the required evidence"
    # P0.4 — a precise refusal names what the retrieved material DOES cover,
    # so a nearby-but-different-concept miss is visible to the user.
    nearby_names: list[str] = []
    for chunk in sources or []:
        name = str(
            getattr(chunk, "doc_name", "") or getattr(chunk, "filename", "") or ""
        ).strip()
        if name and name not in nearby_names:
            nearby_names.append(name)
        if len(nearby_names) >= 3:
            break
    nearby_text = (
        f" The nearest retrieved material comes from: {'; '.join(nearby_names)}."
        if nearby_names
        else ""
    )
    if int(gate.get("source_count") or 0) > 0:
        return (
            "I cannot answer that as a source-backed result from the selected "
            f"corpus. The retrieval found some related material, but it did not "
            f"establish {missing_text} strongly enough to support the question."
            f"{nearby_text} "
            "Try a broader corpus/search scope or ask for the narrower part the "
            "retrieved sources do cover."
        )
    return (
        "I cannot answer that from the selected corpus because retrieval did "
        f"not find source evidence for {missing_text}. Try selecting the repo "
        "or corpus that contains that material, or broaden the search scope."
    )


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


# Domain values that are real taxonomy labels (Ghost A) vs. the cluster/outlier
# placeholders some parents carry — only real labels are shown to the model,
# so "Domain: Cluster 3" never leaks into evidence. Deterministic, list-free
# test: a taxonomy label is lowercase snake/alpha and not a cluster marker.
def _is_taxonomy_domain(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text or text in {"other", "outliers", "unknown", "null", "none"}:
        return False
    if text.startswith("cluster"):
        return False
    return True


def _clean_source_label(raw: str) -> str:
    """Human-friendly source label from a filename/title — never an ID.

    Strips the .md/.pdf/.epub extension and the common Anna's-Archive /
    libgen provenance tails so the model sees "The Art of Seduction" not
    "The Art of Seduction -- Robert Greene -- 2005 -- Anna's Archive.md".
    """
    text = str(raw or "").strip()
    for ext in (".md", ".pdf", ".epub", ".txt", ".html"):
        if text.lower().endswith(ext):
            text = text[: -len(ext)]
            break
    # Provenance tail: "… -- Author -- Year -- Anna's Archive" / "… libgen.li"
    for sep in (" -- ", "{", "(z-lib", "libgen", "Anna’s Archive", "Anna's Archive"):
        idx = text.find(sep)
        if idx > 12:  # keep at least a plausible title before cutting
            text = text[:idx]
    return text.strip(" -_.") or "Untitled source"


def _source_section_label(data: dict[str, Any]) -> str:
    """Human-readable section from heading_path — the last 2 non-empty
    segments (e.g. 'Chapter 3 › The Charmer'). Document structure the model
    otherwise never sees. Empty string when no heading is available."""
    heading = data.get("heading_path")
    if not heading:
        meta = _source_metadata(data)
        heading = meta.get("heading_path")
    if not isinstance(heading, (list, tuple)):
        return ""
    segments = [str(h).strip() for h in heading if str(h or "").strip()]
    if not segments:
        return ""
    return " › ".join(segments[-2:])


def _source_title(data: dict[str, Any]) -> str:
    """Human-facing source label. NEVER falls back to an internal doc_id /
    chunk_id (that leaked DB identifiers into model input and citations);
    an unnamed source is labeled generically instead."""
    metadata = _source_metadata(data)
    for value in (
        data.get("doc_name"),
        metadata.get("title"),
        metadata.get("filename"),
        metadata.get("url"),
    ):
        text = str(value or "").strip()
        if text:
            return _clean_source_label(text)
    return "Untitled source"


def _source_excerpt(
    data: dict[str, Any], *, max_chars: int, query: str | None = None
) -> str:
    text = str(data.get("text") or data.get("summary") or "").strip()
    marker = "\nContent: "
    if marker in text:
        text = text.split(marker, 1)[1]
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    if query:
        # B2: fill the budget with the query's best sentence window instead
        # of the chunk head — the final model should argue from the passage
        # that matched, not from whatever the chunk opens with.
        windowed = _query_guided_excerpt(text, query, max_chars=max_chars - 1)
        if windowed:
            return windowed.rstrip() + "..."
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


def _annotate_web_evidence_scores(
    query: str, chunks: list[Any]
) -> list[dict[str, Any]]:
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
    best_score = max(
        (float(score.get("final") or 0.0) for score in scores), default=0.0
    )
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
    elif (
        best_score >= 0.72 and avg_score >= 0.55 and result_count >= 3 and not degraded
    ):
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
    anchors = re.findall(
        r"\b[A-Z][A-Za-z0-9_]*(?:Service|API|SDK|DB|RAG|LLM)?\b",
        original_query or search_query,
    )
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
    if any(
        marker in lower_original
        for marker in ("official", "docs", "documentation", "api", "reference")
    ):
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
    selected_corpus_ids = list(getattr(request, "corpus_ids", None) or [])
    source_dicts = [
        data
        for source in (sources or [])
        if (data := _source_to_dict(source)) is not None
        and _source_allowed_by_corpus_scope(source, selected_corpus_ids)
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
            excerpt = _source_excerpt(
                data, max_chars=700, query=getattr(request, "message", None)
            )
            # Metadata prefix (M1): surface the provenance the model needs to
            # reason about source discipline and structure — Title, Section,
            # Domain, semantic/structural Kind. All fields are already on the
            # hydrated SourceChunk (domain + heading_path via parent lookup);
            # internal IDs, hashes, paths, and token counts are deliberately
            # NOT included. Domain shown only when it is a real taxonomy label.
            prefix_bits: list[str] = [f"{idx}. {label}"]
            section = _source_section_label(data)
            if section:
                prefix_bits.append(f"§ {section}")
            domain = data.get("domain") or metadata.get("domain")
            if _is_taxonomy_domain(domain):
                prefix_bits.append(f"domain={str(domain).strip()}")
            kind = (
                metadata.get("semantic_chunk_type")
                or data.get("chunk_kind")
                or metadata.get("chunk_kind")
                or "body"
            )
            prefix_bits.append(f"kind={kind}")
            prefix_bits.append(f"score={score}")
            lines.append(" | ".join(prefix_bits) + f"\n   {excerpt or '(no excerpt)'}")

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
            final_score = score.get("final") if isinstance(score, dict) else None
            excerpt = _source_excerpt(
                data, max_chars=1100, query=getattr(request, "message", None)
            )
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


def _source_neardup_key(source: Any) -> str | None:
    """Cross-document near-duplicate key: a normalized text signature that is NOT
    scoped by doc_id, so the SAME passage surfaced under two documents (e.g. a
    book ingested as both a PDF and a .md) collapses to one card. Normalization
    (lowercase, strip non-alphanumeric) absorbs whitespace/markup differences
    between the two conversions."""
    data = _source_to_dict(source)
    if not data or _is_web_source_data(data):
        return None
    norm = re.sub(r"[^a-z0-9]+", " ", str(data.get("text") or "").lower()).strip()
    if len(norm) < 120:
        return None
    return f"nd:{norm[:400]}"


def _dedupe_sources_for_context(sources: list[Any] | None) -> list[Any]:
    """Preserve order while removing exact + near-duplicate source cards."""
    if not sources:
        return []
    deduped: list[Any] = []
    seen: set[str] = set()
    seen_exact_text: set[str] = set()
    seen_neardup: set[str] = set()
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
        neardup_key = _source_neardup_key(source)
        if neardup_key and neardup_key in seen_neardup:
            duplicates += 1
            continue
        if key:
            seen.add(key)
        if text_key:
            seen_exact_text.add(text_key)
        if neardup_key:
            seen_neardup.add(neardup_key)
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
    # Match low-value markers on the HEADING + summary only — NOT arbitrary body
    # text. A substantive chunk that merely contains the word "references" (e.g.
    # "BERT references the original transformer paper") is not low-value;
    # matching it in body text was silently dropping good evidence and thinning
    # the final set. True reference / boilerplate chunks are now caught at
    # ingestion (chunk_kind → NOISY_KINDS retrieval filter); this stays as the
    # heading-level backstop for legacy/unclassified chunks.
    if _LOW_VALUE_EVIDENCE_RE.search(f"{heading_text}\n{summary_head}"):
        return True
    leading_body = text_head.lstrip()[:500]
    if re.search(
        r"(?im)^\s{0,3}#{1,6}\s*(?:table of contents|references?|"
        r"bibliography|works cited|acknowledg(?:e)?ments?|"
        r"join our (?:book'?s |community'?s )?discord)\b",
        leading_body,
    ):
        return True
    if re.search(r"\brelated work\b", heading_text, re.IGNORECASE):
        haystack = f"{heading_text}\n{summary_head}\n{text_head}"
        citation_hits = len(
            re.findall(r"\b[A-Z][A-Za-z-]+ et al\.\s*\(\d{4}", haystack)
        )
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


_RESERVED_SUPPORT_ROLES = frozenset(
    {"evidence_plan_lane", "chat_semantic_facet_coverage"}
)


def _is_reserved_support_chunk(source: Any) -> bool:
    """True for chunks a coverage stage deliberately reserved.

    Per-side evidence-plan support and semantic-facet coverage chunks are the
    evidence we added on purpose to balance a multi-document answer. They are
    protected from the per-document cap so the cap trims the dominant book, not
    the reserved sides.
    """

    metadata = getattr(source, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("support_role") or "") in _RESERVED_SUPPORT_ROLES


def _answerability_chunk_text(source: SourceChunk) -> str:
    """Body-focused text used by the final-context chunk gate.

    This intentionally excludes doc_id/doc_name. A chunk from an on-topic book
    still needs answer-bearing passage text, summary text, heading text, or
    semantic facet text to survive the gate.
    """

    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    values: list[Any] = [
        source.text,
        source.summary,
        " ".join(str(v) for v in (source.heading_path or [])),
        metadata.get("title"),
        metadata.get("section"),
        " ".join(metadata_facet_terms(metadata)),
    ]
    return _chat_coverage_norm(" ".join(str(v) for v in values if v))


def _answerability_contains(text: str, term: str) -> bool:
    norm = _chat_coverage_norm(term)
    if not norm:
        return False
    return f" {norm} " in f" {text} "


def _answerability_terms_for_gate(query: str) -> list[str]:
    terms = [
        term
        for term in lexical_terms(query or "")
        if term not in GENERIC_CONCEPT_TOKENS
    ]
    normalized = f" {_chat_coverage_norm(query)} "
    # In "the art of seduction", art is part of the title phrase, not a useful
    # answer-bearing anchor. Keeping it makes unrelated "art" passages survive.
    if " art of " in normalized:
        terms = [term for term in terms if term != "art"]
    return terms


def _query_requests_broad_spectrum(query: str | None) -> bool:
    return bool(_BROAD_SPECTRUM_QUERY_RE.search(str(query or "")))


def _score_answerability_chunk(
    source: SourceChunk,
    *,
    query: str,
    evidence_plan: EvidencePlan | None = None,
    required_planned_lane_ids: list[str] | None = None,
) -> tuple[float, dict[str, Any]]:
    if _is_reserved_support_chunk(source):
        return 1.0, {
            "reason": "reserved_support",
            "protected": True,
            "matched_concepts": [],
            "matched_lanes": [],
            "matched_terms": [],
        }

    metadata = source.metadata if isinstance(source.metadata, dict) else {}
    corpus_reservations = [
        str(value)
        for value in (metadata.get("planned_corpus_reservations") or [])
        if str(value)
    ]
    if corpus_reservations:
        return 1.0, {
            "reason": "selected_corpus_reservation",
            "protected": True,
            "matched_concepts": [],
            "matched_lanes": [],
            "matched_terms": [],
            "matched_corpora": corpus_reservations,
        }

    supported_required_lanes = reserved_required_lane_ids(
        source,
        required_planned_lane_ids,
    )
    if supported_required_lanes:
        return 1.0, {
            "reason": "query_plan_required_lane",
            "protected": True,
            "matched_concepts": [],
            "matched_lanes": supported_required_lanes,
            "matched_terms": [],
        }

    text = _answerability_chunk_text(source)
    plan = evidence_plan if isinstance(evidence_plan, EvidencePlan) else None
    lanes = list(plan.required_lanes) if plan and plan.active else []
    # When an evidence plan exists, trust its collapsed semantic lanes. Raw
    # concept aliases include intentionally broad surfaces ("type", "profile")
    # that are useful for recall but too loose for final answer-bearing proof.
    groups = [] if lanes else concept_groups(query or "", max_groups=4)
    terms = _answerability_terms_for_gate(query or "")

    if not groups and not terms and not lanes:
        return 1.0, {
            "reason": "no_query_anchors",
            "protected": False,
            "matched_concepts": [],
            "matched_lanes": [],
            "matched_terms": [],
        }

    grounding = (
        metadata.get("query_grounding")
        if isinstance(metadata.get("query_grounding"), dict)
        else {}
    )
    grounded = {
        _chat_coverage_norm(item)
        for item in (grounding.get("matched") or [])
        if str(item).strip()
    }

    matched_concepts: list[str] = []
    for group in groups:
        aliases = [group.key.replace("_", " "), *group.aliases]
        if _chat_coverage_norm(group.key) in grounded or any(
            _answerability_contains(text, alias) for alias in aliases
        ):
            matched_concepts.append(group.key)

    matched_lanes: list[str] = []
    for lane in lanes:
        grounded_lane_match = (
            _chat_coverage_norm(lane.name) in grounded
            or _chat_coverage_norm(lane.concept_key) in grounded
        )
        # For personality lanes, grounding metadata may only say that the broad
        # word "personality" appeared in the query. That is useful telemetry,
        # not answer-bearing proof that this chunk comes from a personality
        # framework/book.
        if lane.concept_key in {"personality", "personality_framework"}:
            grounded_lane_match = False
        if (
            _evidence_lane_match_score(source, lane) >= _lane_strong_score()
            or grounded_lane_match
        ):
            matched_lanes.append(lane.name)

    matched_terms = [term for term in terms if _answerability_contains(text, term)]
    lexical_score = len(matched_terms) / len(terms) if terms else 0.0
    side_score = 1.0 if matched_concepts or matched_lanes else 0.0
    # Restore lexical coverage as a floor even for multi-lane queries. The lane
    # strictness in _evidence_lane_match_score plus generic-token stripping in
    # _answerability_terms_for_gate already block false lane satisfaction, so
    # zeroing lexical for >=2 lanes was redundant and only cost legitimate
    # term-bearing chunks (the cross-domain bridge that uses neither lane's
    # exact aliases but does carry the query terms).
    score = max(side_score, lexical_score)
    return score, {
        "reason": "answer_bearing" if score > 0 else "no_answer_bearing_overlap",
        "protected": False,
        "matched_concepts": matched_concepts,
        "matched_lanes": matched_lanes,
        "matched_terms": matched_terms,
        "lexical_coverage": round(lexical_score, 4),
    }


def _annotate_answerability_chunk_gate(
    source: SourceChunk,
    *,
    score: float,
    status: str,
    detail: dict[str, Any],
    mode: str,
) -> SourceChunk:
    data = source.model_dump()
    metadata = dict(data.get("metadata") or {})
    metadata["answerability_chunk_gate"] = {
        "mode": mode,
        "status": status,
        "score": round(float(score or 0.0), 4),
        "reason": detail.get("reason"),
        "protected": bool(detail.get("protected")),
        "matched_concepts": list(detail.get("matched_concepts") or [])[:6],
        "matched_lanes": list(detail.get("matched_lanes") or [])[:6],
        "matched_terms": list(detail.get("matched_terms") or [])[:10],
        "lexical_coverage": detail.get("lexical_coverage", 0.0),
    }
    data["metadata"] = metadata
    return SourceChunk(**data)


def _apply_final_context_answerability_gate(
    sources: list[SourceChunk],
    *,
    query: str,
    evidence_plan: EvidencePlan | None = None,
    required_planned_lane_ids: list[str] | None = None,
    search_mode: str | None = None,
    mode: str | None = None,
    min_keep: int | None = None,
    strict_floor: float | None = None,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Flag-gated per-chunk filter for the final prompt context.

    This is softer than the answer/refuse gate: it only shapes which selected
    chunks the LLM sees. Multi-side support chunks are protected, so the broad
    decomposition layer cannot be undone by a late lexical pass.
    """

    active_mode = str(
        mode
        if mode is not None
        else getattr(settings, "ANSWERABILITY_CHUNK_GATE", "off") or "off"
    ).lower()
    if active_mode not in {"soft", "strict"}:
        return list(sources or []), {
            "enabled": False,
            "mode": active_mode,
            "input": len(sources or []),
            "kept": len(sources or []),
            "dropped": 0,
            "demoted": 0,
        }

    if str(search_mode or "").lower() == "global":
        return list(sources or []), {
            "enabled": True,
            "mode": active_mode,
            "skipped": True,
            "skip_reason": "global_search_mode",
            "input": len(sources or []),
            "kept": len(sources or []),
            "dropped": 0,
            "demoted": 0,
        }

    floor = (
        0.0
        if active_mode == "soft"
        else float(
            strict_floor
            if strict_floor is not None
            else getattr(settings, "ANSWERABILITY_CHUNK_GATE_STRICT_FLOOR", 0.20)
        )
    )
    keep_floor = max(
        1,
        int(
            min_keep
            if min_keep is not None
            else getattr(
                settings,
                "ANSWERABILITY_CHUNK_GATE_MIN_KEEP",
                _CHAT_EVIDENCE_MIN_KEEP_AFTER_FILTER,
            )
        ),
    )

    plan_for_gate = evidence_plan if isinstance(evidence_plan, EvidencePlan) else None
    gate_lanes = (
        list(plan_for_gate.required_lanes)
        if plan_for_gate and plan_for_gate.active
        else []
    )
    multi_side_plan = len(gate_lanes) >= 2
    broad_spectrum = _query_requests_broad_spectrum(query)

    strong: list[SourceChunk] = []
    weak_rows: list[tuple[SourceChunk, SourceChunk, float, dict[str, Any]]] = []
    score_rows: list[dict[str, Any]] = []
    for source in sources or []:
        score, detail = _score_answerability_chunk(
            source,
            query=query,
            evidence_plan=evidence_plan,
            required_planned_lane_ids=required_planned_lane_ids,
        )
        protected = bool(detail.get("protected"))
        passes = protected or (
            score > floor if active_mode == "soft" else score >= floor
        )
        status = "kept" if passes else "drop_candidate"
        annotated = _annotate_answerability_chunk_gate(
            source,
            score=score,
            status=status,
            detail=detail,
            mode=active_mode,
        )
        score_rows.append(
            {
                "chunk_id": str(getattr(source, "chunk_id", "") or ""),
                "doc_id": str(getattr(source, "doc_id", "") or ""),
                "score": round(float(score or 0.0), 4),
                "status": status,
                "reason": detail.get("reason"),
                "matched_concepts": list(detail.get("matched_concepts") or [])[:4],
                "matched_lanes": list(detail.get("matched_lanes") or [])[:4],
                "matched_terms": list(detail.get("matched_terms") or [])[:6],
            }
        )
        if passes:
            strong.append(annotated)
        else:
            weak_rows.append((source, annotated, float(score or 0.0), detail))

    needed = max(0, keep_floor - len(strong))
    recoverable_rows = weak_rows
    if multi_side_plan and active_mode == "soft":
        # For a decomposed multi-side question, do not refill the prompt with
        # zero-evidence chunks just to satisfy min_keep. This preserves answer
        # breadth without letting a broad word like "personality" pull software
        # passages back into the final context.
        recoverable_rows = [row for row in weak_rows if row[2] > 0]
    recovered_rows = recoverable_rows[:needed]
    recovered_raw = [row[0] for row in recovered_rows]
    recovered: list[SourceChunk] = []
    for source in recovered_raw:
        score, detail = _score_answerability_chunk(
            source,
            query=query,
            evidence_plan=evidence_plan,
            required_planned_lane_ids=required_planned_lane_ids,
        )
        recovered.append(
            _annotate_answerability_chunk_gate(
                source,
                score=score,
                status="demoted_min_keep",
                detail=detail,
                mode=active_mode,
            )
        )
    kept = [*strong, *recovered]
    recovered_keys = {
        str(getattr(source, "chunk_id", "") or f"object:{id(source)}")
        for source in recovered_raw
    }
    dropped = [
        annotated
        for source, annotated, _score, _detail in weak_rows
        if str(getattr(source, "chunk_id", "") or f"object:{id(source)}")
        not in recovered_keys
    ]
    return kept, {
        "enabled": True,
        "mode": active_mode,
        "input": len(sources or []),
        "kept": len(kept),
        "dropped": len(dropped),
        "demoted": len(recovered),
        "broad_spectrum": broad_spectrum,
        "multi_side_plan": multi_side_plan,
        "min_keep": keep_floor,
        "floor": round(floor, 4),
        "dropped_chunk_ids": [
            str(getattr(source, "chunk_id", "") or "") for source in dropped
        ],
        "demoted_chunk_ids": [
            str(getattr(source, "chunk_id", "") or "") for source in recovered
        ],
        "scores": score_rows[:12],
    }


def _cap_chunks_per_doc(
    sources: list[SourceChunk], cap: int = _CHAT_PER_DOC_CAP
) -> list[SourceChunk]:
    """Keep at most `cap` chunks per document, preserving order (sources arrive
    in selection/score order). A universal final guard so one book — or a
    duplicate copy of it — can't monopolize the context with near-redundant
    passages, regardless of which coverage path produced the set."""
    if not cap or cap <= 0:
        return sources
    kept: list[SourceChunk] = []
    counts: dict[str, int] = {}
    for source in sources:
        doc_id = str(getattr(source, "doc_id", "") or "")
        if doc_id:
            if counts.get(doc_id, 0) >= cap:
                continue
            counts[doc_id] = counts.get(doc_id, 0) + 1
        kept.append(source)
    return kept


def _query_terms_for_prompt_clip(query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,}", query or ""):
        term = token.lower().strip()
        if not term:
            continue
        if len(term) <= 2 and term not in {"ai", "ml"}:
            continue
        if term not in terms:
            terms.append(term)
    return terms[:12]


def _clip_source_text_for_prompt(text: str, query: str, max_chars: int) -> str:
    text = str(text or "")
    max_chars = max(120, int(max_chars or 0))
    if len(text) <= max_chars:
        return text

    text_low = text.lower()
    positions = [
        pos
        for term in _query_terms_for_prompt_clip(query)
        if (pos := text_low.find(term)) >= 0
    ]
    if positions:
        center = min(positions)
        start = max(0, center - (max_chars // 3))
        end = min(len(text), start + max_chars)
        start = max(0, end - max_chars)
        prefix = (
            "[...source excerpt clipped before this point...]\n" if start > 0 else ""
        )
        suffix = (
            "\n[...source excerpt clipped after this point...]"
            if end < len(text)
            else ""
        )
        return f"{prefix}{text[start:end].strip()}{suffix}"

    return (
        text[:max_chars].rstrip() + "\n[...source excerpt clipped after this point...]"
    )


def _copy_source_for_prompt(source: SourceChunk, *, text: str) -> SourceChunk:
    if hasattr(source, "model_copy"):
        return source.model_copy(update={"text": text})
    data = source.model_dump() if hasattr(source, "model_dump") else dict(source)
    data["text"] = text
    return SourceChunk(**data)


def _compact_sources_for_prompt(
    sources: list[SourceChunk],
    *,
    query: str,
    source_max_chars: int | None,
) -> list[SourceChunk]:
    if source_max_chars is None:
        return list(sources or [])
    return [
        _copy_source_for_prompt(
            source,
            text=_clip_source_text_for_prompt(
                source.text or "", query, source_max_chars
            ),
        )
        for source in (sources or [])
    ]


def _build_budgeted_augmented_prompt(
    *,
    query: str,
    sources: list[SourceChunk],
    facts: list[Any],
    corpus_ids: list[str] | None,
    reasoning_mode: str | None,
    reasoning_blend: list[str] | None,
    active_skills: list[dict] | None,
    analysis: str | None,
    decoration: list[Any],
    model: str,
    packet: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build the current RAG turn and compact it before history trimming.

    History trimming can remove older turns, but it must keep the current user
    message. A single RAG-heavy turn therefore needs its own deterministic
    compaction path instead of merely logging that it is over budget.
    """
    raw_budget = get_model_context_limit(model) - int(
        getattr(settings, "RESERVE_TOKENS", 500) or 0
    )
    budget_tokens = max(512, raw_budget)

    def _atomic_claim_anchor_render_count(prompt: str) -> int:
        """Count only anchor rows that survived final prompt construction."""

        start = prompt.find("<atomic_claim_anchors>")
        end = prompt.find("</atomic_claim_anchors>", start + 1)
        if start < 0 or end < 0:
            return 0
        return sum(
            line.startswith('- From "') for line in prompt[start:end].splitlines()
        )

    def _build(
        *,
        source_chars: int | None,
        fact_limit: int | None,
        decoration_limit: int | None,
        use_analysis: bool,
    ) -> tuple[str, int, dict[str, Any]]:
        prompt_sources = _compact_sources_for_prompt(
            sources,
            query=query,
            source_max_chars=source_chars,
        )
        prompt_facts = list(facts or [])
        if fact_limit is not None:
            prompt_facts = prompt_facts[: max(0, fact_limit)]
        prompt_decoration = list(decoration or [])
        if decoration_limit is not None:
            prompt_decoration = prompt_decoration[: max(0, decoration_limit)]

        prompt = context_manager.build_augmented_prompt(
            query=query,
            sources=prompt_sources,
            facts=prompt_facts,
            corpus_ids=corpus_ids,
            reasoning_mode=reasoning_mode,
            reasoning_blend=reasoning_blend,
            active_skills=active_skills or None,
            analysis=analysis if use_analysis else None,
            decoration=prompt_decoration,
            packet=packet,
        )
        return (
            prompt,
            count_tokens(prompt, model),
            {
                "source_chars": source_chars,
                "facts": len(prompt_facts),
                "decorations": len(prompt_decoration),
                "analysis": bool(analysis and use_analysis),
            },
        )

    full_prompt, full_tokens, full_shape = _build(
        source_chars=None,
        fact_limit=None,
        decoration_limit=None,
        use_analysis=True,
    )
    if full_tokens <= budget_tokens:
        return full_prompt, {
            "compacted": False,
            "budget_tokens": budget_tokens,
            "before_tokens": full_tokens,
            "after_tokens": full_tokens,
            "shape": full_shape,
            "atomic_claim_anchor_render_count": (
                _atomic_claim_anchor_render_count(full_prompt)
            ),
        }

    best_prompt = full_prompt
    best_tokens = full_tokens
    best_shape = full_shape
    variants: list[tuple[int | None, int | None, int | None, bool]] = []
    for index, source_chars in enumerate(_PROMPT_COMPACTION_SOURCE_CHAR_STEPS):
        variants.append(
            (
                source_chars,
                max(4, min(len(facts or []), 12 - index * 2)),
                max(0, min(len(decoration or []), 16 - index * 3)),
                index < 3,
            )
        )
    variants.append((220, min(len(facts or []), 4), 0, False))

    for source_chars, fact_limit, decoration_limit, use_analysis in variants:
        candidate_prompt, candidate_tokens, candidate_shape = _build(
            source_chars=source_chars,
            fact_limit=fact_limit,
            decoration_limit=decoration_limit,
            use_analysis=use_analysis,
        )
        best_prompt = candidate_prompt
        best_tokens = candidate_tokens
        best_shape = candidate_shape
        if candidate_tokens <= budget_tokens:
            break

    hard_clipped = False
    while best_tokens > budget_tokens and len(best_prompt) > 1000:
        hard_clipped = True
        ratio = max(0.2, min(0.9, budget_tokens / max(best_tokens, 1)))
        max_chars = max(800, int(len(best_prompt) * ratio * 0.9))
        best_prompt = (
            best_prompt[:max_chars].rstrip()
            + "\n\n[...current-turn RAG prompt clipped to fit model context budget...]"
        )
        best_tokens = count_tokens(best_prompt, model)

    return best_prompt, {
        "compacted": True,
        "budget_tokens": budget_tokens,
        "before_tokens": full_tokens,
        "after_tokens": best_tokens,
        "shape": best_shape,
        "source_chunks": len(sources or []),
        "facts_before": len(facts or []),
        "decorations_before": len(decoration or []),
        "hard_clipped": hard_clipped,
        "over_budget_after_compaction": best_tokens > budget_tokens,
        "atomic_claim_anchor_render_count": (
            _atomic_claim_anchor_render_count(best_prompt)
        ),
    }


async def _drop_noisy_retrieval_chunks(chunks: list[Any]) -> list[Any]:
    """Exclude noisy chunks (bibliography / links / boilerplate) from the final
    pool. The graph-expansion lane bypasses funnel_a's Qdrant NOISY_KINDS filter,
    and hydrate can leave chunk_kind=body when a chunk is hydrated as its parent
    — so after the cheap in-memory pass we confirm against the authoritative
    child `chunks` collection in Mongo."""
    if not chunks:
        return chunks
    survivors = [c for c in chunks if not is_noisy(getattr(c, "chunk_kind", None))]
    refs = [
        (
            str(getattr(c, "corpus_id", "") or ""),
            str(getattr(c, "chunk_id", "") or ""),
        )
        for c in survivors
        if getattr(c, "corpus_id", None) and getattr(c, "chunk_id", None)
    ]
    db = getattr(conversation_service, "_db", None)
    if db is None or not refs:
        return survivors
    try:
        rows = (
            await db["chunks"]
            .find(
                {
                    "$or": [
                        {"corpus_id": corpus_id, "chunk_id": chunk_id}
                        for corpus_id, chunk_id in refs
                    ]
                },
                {"corpus_id": 1, "chunk_id": 1, "chunk_kind": 1},
            )
            .to_list(length=None)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("noisy-chunk Mongo confirm failed: %s", exc)
        return survivors
    noisy_refs = {
        (str(r.get("corpus_id") or ""), str(r["chunk_id"]))
        for r in rows
        if is_noisy(r.get("chunk_kind"))
    }
    if not noisy_refs:
        return survivors
    return [
        c
        for c in survivors
        if (
            str(getattr(c, "corpus_id", "") or ""),
            str(getattr(c, "chunk_id", "") or ""),
        )
        not in noisy_refs
    ]


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
                    [
                        haystack.find(term)
                        for term in matched
                        if haystack.find(term) >= 0
                    ]
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
    name_tokens = [token for token in str(facet.get("name") or "").split("_") if token]
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
            dict.fromkeys(
                [*(existing.get("matched") or []), *(row.get("matched") or [])]
            )
        )[:8]
        existing["support_terms"] = list(
            dict.fromkeys(
                [
                    *(existing.get("support_terms") or []),
                    *(row.get("support_terms") or []),
                ]
            )
        )[:12]
        existing["triggers"] = list(
            dict.fromkeys(
                [*(existing.get("triggers") or []), *(row.get("triggers") or [])]
            )
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
                    [
                        *(existing.get("facet_doc_ids") or []),
                        *(row.get("facet_doc_ids") or []),
                    ]
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
    support_facet = (
        metadata.get("support_facet")
        if isinstance(metadata.get("support_facet"), dict)
        else {}
    )
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


def _chat_query_fit_score(
    source: SourceChunk, query: str, facet: dict[str, Any]
) -> int:
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
        for term in re.findall(
            r"[a-z0-9][a-z0-9\-]{4,}", str(original_query or "").lower()
        )
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
        final_score = (
            (facet_score * 10.0) + (query_fit * 2.0) + bounded_score + new_doc_bonus
        )
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
                corpus_id=str(cleaned.corpus_id or ""),
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


# P1.5 shelf_reserve — bounded librarian-card projection for the seat pass:
# exactly the fields shelf_engine.assign_shelf_roles reads (role fields with
# their entry-level source_ids) plus the aggregated evidence_spans.
_SHELF_RESERVE_CARD_PROJECTION = {
    "_id": 0,
    "corpus_id": 1,
    "doc_id": 1,
    "schema_version": 1,
    "central_subjects": 1,
    "candidate_latent_subjects": 1,
    "capabilities_developed": 1,
    "mechanisms_taught": 1,
    "transferable_principles": 1,
    "evidence_spans": 1,
}
_SHELF_RESERVE_MAX_CONCEPTS = 12


def _shelf_reserve_query_concepts(
    query: str,
    retrieval_diagnostics: dict[str, Any] | None,
) -> list[str]:
    """Resolved query concept ids for the P1.5 shelf_reserve seat pass.

    Deterministic source choice (documented per the P1.5 spec):

    1. PRIMARY: ``vocabulary_resolution.matches[].canonical_key`` from the
       main retrieval's diagnostics — corpus-resolved lexicon ids that share
       the ``normalize_identity`` keyspace with librarian-card ``value_key``s
       (librarian cards are seeded from the same corpus lexicon), so this is
       the exact-join source. Already computed by the retrieval that just
       ran; no new calls of any kind.
    2. FALLBACK (no vocabulary matches): ``concept_groups(query)`` keys —
       the same pure deterministic resolver that seeds the evidence plan.

    No LLM in either path. Bounded at _SHELF_RESERVE_MAX_CONCEPTS.
    """

    concepts: list[str] = []
    seen: set[str] = set()

    def _push(value: Any) -> None:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            return
        seen.add(key)
        concepts.append(text)

    diagnostics = (
        retrieval_diagnostics if isinstance(retrieval_diagnostics, dict) else {}
    )
    vocabulary = (
        diagnostics.get("vocabulary_resolution")
        if isinstance(diagnostics.get("vocabulary_resolution"), dict)
        else {}
    )
    for row in vocabulary.get("matches") or []:
        if isinstance(row, dict):
            _push(row.get("canonical_key") or row.get("canonical_name"))
        if len(concepts) >= _SHELF_RESERVE_MAX_CONCEPTS:
            return concepts
    if concepts:
        return concepts
    for group in concept_groups(query or "", max_groups=8):
        _push(group.key)
        if len(concepts) >= _SHELF_RESERVE_MAX_CONCEPTS:
            break
    return concepts


async def _shelf_reserve_context_for_pool(
    sources: list[SourceChunk],
    *,
    query_concepts: list[str],
    db: Any = None,
) -> ShelfReserveContext:
    """Build the shelf_reserve context for the pooled candidate documents.

    ONE Mongo find on ``librarian_cards`` over the pool's distinct
    ``(corpus_id, doc_id)`` pairs, slim projection. Failure-tolerant: any
    Mongo problem degrades to an empty card map, which the selector records
    as a per-role skip reason — never an exception on the chat path.
    """

    pairs: dict[str, set[str]] = {}
    for source in sources or []:
        doc_id = str(getattr(source, "doc_id", "") or "")
        corpus_id = str(getattr(source, "corpus_id", "") or "")
        if doc_id and corpus_id:
            pairs.setdefault(corpus_id, set()).add(doc_id)
    context = ShelfReserveContext(
        query_concepts=list(query_concepts or []),
        cards_by_doc={},
        enabled=True,
    )
    if db is None:
        db = getattr(conversation_service, "_db", None)
    if db is None or not pairs:
        return context
    query = {
        "$or": [
            {"corpus_id": corpus_id, "doc_id": {"$in": sorted(doc_ids)}}
            for corpus_id, doc_ids in sorted(pairs.items())
        ]
    }
    try:
        rows = (
            await db["librarian_cards"]
            .find(query, _SHELF_RESERVE_CARD_PROJECTION)
            .to_list(length=None)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("shelf_reserve card fetch failed; seat pass degrades: %s", exc)
        return context
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("corpus_id") or ""), str(row.get("doc_id") or ""))
        if key[1]:
            context.cards_by_doc.setdefault(key, row)
    return context


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
    per_doc_cap: int | None = None,
    selected_corpus_ids: list[str] | None = None,
    shelf_reserve_context: ShelfReserveContext | None = None,
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
        # 0 disables the cap; coerce to None so the selector skips the ceiling
        # entirely (per_doc_cap=0 would otherwise reject every candidate, since
        # doc_counts.get(doc_id, 0) >= 0 is always true).
        per_doc_cap=per_doc_cap or (_CHAT_PER_DOC_CAP or None),
        selected_corpus_ids=selected_corpus_ids or [],
        shelf_reserve_context=shelf_reserve_context,
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
        row for row in breakdown if isinstance(row, dict) and row.get("query_explicit")
    ]
    if not selected and not explicit:
        return None
    lane_counts = (
        meta.get("coverage_lane_counts")
        if isinstance(meta.get("coverage_lane_counts"), dict)
        else {}
    )
    uncovered = [
        str(name) for name in (meta.get("coverage_uncovered_lanes") or []) if name
    ]
    reports = (
        meta.get("lane_reports") if isinstance(meta.get("lane_reports"), list) else []
    )
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
    precomputed_facets: list[dict[str, Any]] | None = None,
    support_semaphore: "asyncio.Semaphore | None" = None,
    shelf_reserve_concepts: list[str] | None = None,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Add missing query-facet evidence using the same chat retrieval tier.

    This is query satisfaction, not source diversity for its own sake. Normal
    retrieval runs first. Only query-stated facets with weak coverage get a
    small support retrieval, and selected chunks must still fit the original
    query/facet before they enter the final prompt.
    """

    base_sources = list(sources or [])
    # Facet detection (its own embed + Qdrant facet search + Mongo lookup) is
    # independent of the main retrieval, so the caller can run it CONCURRENTLY
    # with the main retrieve and hand the result in here — saving its full cost
    # off the critical path. Fall back to detecting inline if not provided.
    facets = precomputed_facets
    if facets is None:
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
    existing_doc_ids = {
        str(source.doc_id or "") for source in base_sources if source.doc_id
    }
    support_sources: list[SourceChunk] = []

    # Each missing facet needs an independent support retrieval, so run them
    # CONCURRENTLY instead of one-after-another — a multi-part query that
    # decomposes into N facets otherwise pays N back-to-back full retrievals,
    # the dominant chat latency on slow models. Each facet selects against the
    # same BASE snapshot of already-chosen chunks/docs; cross-facet de-dup is
    # re-applied in `missing` order after the gather so the final selection
    # matches the old serial behavior. A semaphore bounds the fan-out.
    coverage_semaphore = support_semaphore or asyncio.Semaphore(
        _CHAT_COVERAGE_MAX_CONCURRENCY
    )

    # Coverage support retrievals fill per-facet TEXT gaps; they do NOT need
    # graph expansion (the relational context comes from the MAIN graph
    # retrieval + decoration). On a graph query each support pass otherwise
    # pays ~7s of Neo4j Mode-A expansion + fact seeding + a 48-candidate
    # graph-floor rerank — five times over. Downgrade them to the hybrid tier:
    # the hybrid `naive` collection is a superset of the graph collection's
    # chunks, so no supporting chunk is lost, only the graph overhead.
    coverage_tier = (
        RetrievalTier.qdrant_mongo
        if retrieval_tier == RetrievalTier.qdrant_mongo_graph
        else retrieval_tier
    )

    async def _cover_one_facet(
        facet: dict[str, Any],
    ) -> tuple[
        dict[str, Any], "SourceChunk | None", dict[str, Any], dict[str, Any], str
    ]:
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
        async with coverage_semaphore:
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
                        retrieval_tier=coverage_tier,
                        collections=collections,
                        retrieval_k=max(24, min(int(retrieval_k or 40), 48)),
                        # Coverage support retrievals pick ONE gap-fill chunk per
                        # facet, and the candidate is chosen by facet-fit scoring
                        # (_choose_chat_coverage_candidate_with_report) — not by the
                        # cross-encoder. Skipping rerank here removes the serial
                        # Metal-reranker contention across the ~5 parallel coverage
                        # passes (the dominant remaining coverage cost). Doc-spread
                        # is still enforced downstream by select_facet_final's
                        # distinct-doc cap, and the MAIN answer retrieval still
                        # reranks fully.
                        rerank_enabled=False,
                        support_profile=True,
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
                    logger.debug(
                        "chat coverage support retrieval skipped for %s: %s",
                        facet_name,
                        exc,
                    )
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
        return facet, chosen, chosen_report, lane_report, support_query

    facet_results = await asyncio.gather(
        *[_cover_one_facet(facet) for facet in missing]
    )

    # Apply marking + cross-facet de-dup in facet order. The serial loop never
    # picked a duplicate because it grew the seen-sets between facets; the
    # parallel facets all saw the base snapshot, so two could land on the same
    # chunk/doc — collapse those here (keep the first in facet order).
    for facet, chosen, chosen_report, lane_report, support_query in facet_results:
        if not chosen:
            lane_report["reason"] = "no_support_chunk_selected_after_fallbacks"
            meta["lane_reports"].append(lane_report)
            continue
        chunk_key = str(chosen.chunk_id or "")
        doc_key = str(chosen.doc_id or "")
        if chunk_key in existing_chunk_ids or (doc_key and doc_key in existing_doc_ids):
            lane_report["status"] = "deduped_parallel"
            lane_report["reason"] = "duplicate_selected_by_another_facet"
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
    # P1.5 shelf_reserve (dark behind SHELF_RESERVE_ENABLED): the caller hands
    # in resolved query concepts only when the flag is on and the request is
    # corpus-scoped; fetch librarian cards for the pooled documents (one Mongo
    # find) and pass the context into the final selector's seat pass.
    shelf_reserve_context: ShelfReserveContext | None = None
    if shelf_reserve_concepts is not None and settings.SHELF_RESERVE_ENABLED:
        shelf_reserve_context = await _shelf_reserve_context_for_pool(
            [*base_sources, *support_sources],
            query_concepts=shelf_reserve_concepts,
        )
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
        selected_corpus_ids=corpus_ids or [],
        shelf_reserve_context=shelf_reserve_context,
    )
    actual_support = [
        source
        for source in merged
        if isinstance(source.metadata, dict)
        and source.metadata.get("support_role") == "chat_semantic_facet_coverage"
    ]
    meta["added"] = len(actual_support)
    meta["support_doc_ids"] = [
        str(source.doc_id or "") for source in actual_support if source.doc_id
    ]
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
    meta["coverage_uncovered_priority_lanes"] = selector_meta.get(
        "uncovered_priority_lanes", []
    )
    for report in meta.get("lane_reports", []):
        lane = str(report.get("lane") or "")
        if (
            lane in meta["coverage_uncovered_lanes"]
            and report.get("status") == "selected"
        ):
            report["status"] = "selected_but_not_in_final_packet"
    return merged, meta


def _evidence_source_doc_key(source: SourceChunk) -> str:
    return str(
        source.doc_id or source.doc_name or source.parent_id or source.chunk_id or ""
    )


def _evidence_lane_match_score(source: SourceChunk, lane: EvidenceLane) -> int:
    cleaned = _clean_chat_source_text(source)
    metadata = cleaned.metadata if isinstance(cleaned.metadata, dict) else {}
    score = 0

    support_lane = str(metadata.get("support_lane") or "")
    if support_lane == f"evidence:{lane.name}":
        score += 20
    support_plan = (
        metadata.get("evidence_plan_lane")
        if isinstance(metadata.get("evidence_plan_lane"), dict)
        else {}
    )
    if str(support_plan.get("name") or "") == lane.name:
        score += 12

    grounding = (
        metadata.get("query_grounding")
        if isinstance(metadata.get("query_grounding"), dict)
        else {}
    )
    grounded = {str(item).strip().lower() for item in (grounding.get("matched") or [])}
    if lane.name.lower() in grounded or lane.concept_key.lower() in grounded:
        if lane.concept_key in {"personality", "personality_framework"}:
            # Query grounding can record the broad query word "personality" for
            # semantically adjacent but foreign documents (software books, app
            # design books, etc.). Personality lanes need source/text evidence
            # below; grounding alone must not satisfy the lane.
            score += 2
        else:
            score += 10

    text = _chat_source_text(cleaned)
    if lane.concept_key in {"personality", "personality_framework"}:
        high_text = _chat_coverage_norm(
            " ".join(
                str(v)
                for v in (
                    cleaned.doc_name,
                    cleaned.doc_id,
                    " ".join(str(h) for h in (cleaned.heading_path or [])),
                    metadata.get("title"),
                    metadata.get("filename"),
                    " ".join(metadata_facet_terms(metadata)),
                )
                if v
            )
        )
        strong_personality_terms = [
            term
            for term in lane.search_terms
            if term
            not in {"personality", "character", "trait", "traits", "type", "types"}
        ]
        if any(
            f" {_chat_coverage_norm(term)} " in f" {high_text} "
            for term in strong_personality_terms
        ):
            score += 10
        elif any(
            f" {_chat_coverage_norm(term)} " in f" {text} "
            for term in strong_personality_terms
        ):
            score += 8
        elif " personality " in f" {text} ":
            # A stray body mention ("seductive personality") is useful for
            # ranking but must not mark the personality side covered by itself.
            score += 4
    elif evidence_lane_matches_text(lane, text):
        score += 8
    padded = f" {text} "
    for term in lane.search_terms[:10]:
        norm = _chat_coverage_norm(term)
        if norm and f" {norm} " in padded:
            score += 2
    return score


def _evidence_lane_coverage(
    sources: list[SourceChunk],
    plan: EvidencePlan,
) -> dict[str, Any]:
    lane_doc_ids: dict[str, list[str]] = {}
    lane_doc_names: dict[str, list[str]] = {}
    lane_chunk_ids: dict[str, list[str]] = {}
    lane_scores: dict[str, int] = {}
    for lane in plan.required_lanes:
        doc_ids: list[str] = []
        doc_names: list[str] = []
        chunk_ids: list[str] = []
        best_score = 0
        seen_docs: set[str] = set()
        seen_chunks: set[str] = set()
        for source in sources or []:
            score = _evidence_lane_match_score(source, lane)
            if score <= 0:
                continue
            best_score = max(best_score, score)
            # A side is only *covered* by a chunk that matches it STRONGLY (a
            # real alias hit, score >= LANE_STRONG_SCORE). A weak term
            # co-occurrence — the word "type"/"character" inside a seduction
            # passage — is informative for ranking but must NOT let a foreign
            # book mark the personality side already covered. The strong-score
            # bar and RELATIONSHIP_LANE_MIN_SOURCES (per-lane distinct-doc count)
            # are both tunable via Settings; defaults: score>=8, min_sources=1.
            if score < _lane_strong_score():
                continue
            doc_key = _evidence_source_doc_key(source)
            if doc_key and doc_key not in seen_docs:
                seen_docs.add(doc_key)
                doc_ids.append(doc_key)
                doc_names.append(str(source.doc_name or source.doc_id or doc_key))
            chunk_key = str(source.chunk_id or "")
            if chunk_key and chunk_key not in seen_chunks:
                seen_chunks.add(chunk_key)
                chunk_ids.append(chunk_key)
        lane_doc_ids[lane.name] = doc_ids
        lane_doc_names[lane.name] = doc_names
        lane_chunk_ids[lane.name] = chunk_ids
        lane_scores[lane.name] = best_score

    # Evidence allocation uses the plan's own breadth floor. For multi-side
    # synthesis, one strong chunk is not enough to stop support retrieval; each
    # side needs its requested distinct-document depth before it is considered
    # covered in the evidence packet.
    covered = [
        lane.name
        for lane in plan.required_lanes
        if len(lane_doc_ids.get(lane.name) or []) >= max(1, int(lane.min_sources or 1))
    ]
    missing = [
        lane.name for lane in plan.required_lanes if lane.name not in set(covered)
    ]
    distinct_docs = sorted(
        {doc_id for doc_ids in lane_doc_ids.values() for doc_id in doc_ids if doc_id}
    )
    return {
        "covered_lanes": covered,
        "missing_lanes": missing,
        "lane_doc_ids": lane_doc_ids,
        "lane_doc_names": lane_doc_names,
        "lane_chunk_ids": lane_chunk_ids,
        "lane_scores": lane_scores,
        "distinct_doc_ids": distinct_docs,
        "distinct_doc_count": len(distinct_docs),
    }


def _evidence_lane_candidate_snapshot(
    chunk: SourceChunk,
    *,
    lane: EvidenceLane,
    reason: str,
) -> dict[str, Any]:
    cleaned = _clean_chat_source_text(chunk)
    return {
        "chunk_id": str(cleaned.chunk_id or ""),
        "doc_id": str(cleaned.doc_id or ""),
        "doc_name": str(cleaned.doc_name or ""),
        "score": float(cleaned.score or 0.0),
        "lane_score": _evidence_lane_match_score(cleaned, lane),
        "reason": reason,
    }


def _evidence_facet_hint_terms(facets: list[dict[str, Any]] | None) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for facet in facets or []:
        if not isinstance(facet, dict):
            continue
        values: list[Any] = [
            facet.get("label"),
            str(facet.get("name") or "").replace("_", " "),
            *(facet.get("matched") or []),
            *(facet.get("support_terms") or []),
            *(facet.get("triggers") or []),
        ]
        for doc in facet.get("facet_docs") or []:
            if isinstance(doc, dict):
                values.append(doc.get("filename"))
        for value in values:
            text = " ".join(str(value or "").split()).strip()
            key = _chat_coverage_norm(text)
            if text and key and key not in seen:
                seen.add(key)
                terms.append(text)
    return terms[:18]


def _evidence_facet_hint_doc_ids(facets: list[dict[str, Any]] | None) -> set[str]:
    doc_ids: set[str] = set()
    for facet in facets or []:
        if not isinstance(facet, dict):
            continue
        doc_ids.update(
            str(doc_id) for doc_id in (facet.get("facet_doc_ids") or []) if doc_id
        )
        for doc in facet.get("facet_docs") or []:
            if isinstance(doc, dict) and doc.get("doc_id"):
                doc_ids.add(str(doc.get("doc_id")))
    return doc_ids


async def _semantic_ingest_hints_for_evidence_lane(
    lane: EvidenceLane,
    corpus_ids: list[str] | None,
) -> dict[str, Any]:
    if not corpus_ids:
        return {"facets": [], "terms": [], "doc_ids": []}
    try:
        facets = await _chat_coverage_facets_for_query_with_corpus(
            lane.query,
            corpus_ids,
        )
    except Exception as exc:
        logger.debug(
            "evidence-plan semantic ingest hints skipped for %s: %s", lane.name, exc
        )
        facets = []
    terms = _evidence_facet_hint_terms(facets)
    doc_ids = sorted(_evidence_facet_hint_doc_ids(facets))
    return {
        "facets": [
            {
                "name": str(facet.get("name") or ""),
                "label": str(facet.get("label") or ""),
                "source": str(facet.get("source") or ""),
                "matched": facet.get("matched") or [],
                "support_terms": facet.get("support_terms") or [],
                "facet_doc_ids": facet.get("facet_doc_ids") or [],
                "facet_docs": facet.get("facet_docs") or [],
                "match_score": facet.get("match_score"),
                "vector_score": facet.get("vector_score"),
            }
            for facet in facets[:6]
            if isinstance(facet, dict)
        ],
        "terms": terms,
        "doc_ids": doc_ids,
    }


def _choose_evidence_lane_candidates_with_report(
    candidates: list[SourceChunk],
    *,
    lane: EvidenceLane,
    original_query: str,
    existing_chunk_ids: set[str],
    existing_doc_ids: set[str],
    semantic_doc_ids: set[str] | None = None,
    target_k: int,
    same_doc_target_k: int = 0,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    """Pick up to ``target_k`` lane-support chunks, each from a DISTINCT document.

    This is the per-side reservation: rather than a single chunk per lane, a
    side under-covered by the base retrieval gets real depth from several of its
    own documents. The distinct-document selection (and new-doc / ingest-doc
    preference) is delegated to the pure
    :func:`evidence_allocation.select_lane_support` so it stays unit-tested;
    production's metadata/grounding-aware scoring is supplied here via
    ``_evidence_lane_match_score``.
    """

    cleaned = [_clean_chat_source_text(chunk) for chunk in candidates or []]
    picks = _ea_select_lane_support(
        cleaned,
        lane=lane,
        target_k=target_k,
        existing_chunk_ids=set(existing_chunk_ids),
        existing_doc_ids=set(existing_doc_ids),
        semantic_doc_ids=set(semantic_doc_ids or set()),
        score_fn=_evidence_lane_match_score,
        chunk_id_fn=lambda c: str(c.chunk_id or ""),
        doc_id_fn=_evidence_source_doc_key,
        base_score_fn=lambda c: float(c.score or 0.0),
        low_value_fn=lambda c: _chat_source_is_low_value(c, original_query),
    )
    reason = "per_side_lane_support" if picks else "no_candidate_matched_lane"
    same_doc_target_k = max(0, int(same_doc_target_k or 0))
    if not picks and same_doc_target_k > 0:
        same_doc_candidates: list[tuple[float, SourceChunk]] = []
        for chunk in cleaned:
            chunk_id = str(chunk.chunk_id or "")
            if not chunk_id or chunk_id in existing_chunk_ids:
                continue
            if _chat_source_is_low_value(chunk, original_query):
                continue
            doc_id = _evidence_source_doc_key(chunk)
            if not doc_id or doc_id not in set(existing_doc_ids):
                continue
            lane_score = _evidence_lane_match_score(chunk, lane)
            if lane_score < _lane_strong_score():
                continue
            same_doc_candidates.append(
                (
                    lane_score * 10.0 + min(max(float(chunk.score or 0.0), 0.0), 1.0),
                    chunk,
                )
            )
        same_doc_candidates.sort(key=lambda row: row[0], reverse=True)
        if same_doc_candidates:
            picks = [chunk for _score, chunk in same_doc_candidates[:same_doc_target_k]]
            reason = "same_doc_deepening_after_new_doc_exhausted"
    report: dict[str, Any] = {
        "status": "selected" if picks else "uncovered",
        "selected_count": len(picks),
        "target_k": int(target_k),
        "reason": reason,
        "selected": [
            _evidence_lane_candidate_snapshot(
                pick,
                lane=lane,
                reason=(
                    "strong_support"
                    if _evidence_lane_match_score(pick, lane)
                    >= _EVIDENCE_LANE_STRONG_SCORE
                    else "weak_support"
                ),
            )
            for pick in picks
        ],
    }
    return picks, report


def _mark_evidence_plan_chunk(
    chunk: SourceChunk,
    *,
    lane: EvidenceLane,
    support_query: str,
    support_strength: str,
) -> SourceChunk:
    chunk = _clean_chat_source_text(chunk)
    data = chunk.model_dump()
    metadata = dict(data.get("metadata") or {})
    lane_score = _evidence_lane_match_score(chunk, lane)
    support_query_score = float(data.get("score") or 0.0)
    selection_score = min(
        0.97,
        (min(lane_score, 24) / 24.0 * 0.82)
        + (max(0.0, min(support_query_score, 1.0)) * 0.18),
    )
    data["score"] = round(selection_score, 6)
    metadata["support_role"] = "evidence_plan_lane"
    metadata["support_lane"] = f"evidence:{lane.name}"
    metadata["support_query"] = _clip_trace_value(support_query, 220)
    metadata["support_query_score"] = support_query_score
    metadata["support_selection_score"] = round(selection_score, 6)
    metadata["support_lane_score"] = lane_score
    metadata["support_strength"] = support_strength
    metadata["evidence_plan_lane"] = {
        "name": lane.name,
        "label": lane.label,
        "concept_key": lane.concept_key,
    }
    data["metadata"] = metadata
    return SourceChunk(**data)


def _evidence_support_query_variants(
    lane: EvidenceLane,
    original_query: str,
    semantic_hints: dict[str, Any] | None = None,
) -> list[str]:
    hint_terms = [
        str(term)
        for term in ((semantic_hints or {}).get("terms") or [])
        if str(term).strip()
    ]
    original_preserving_variant = " ".join([lane.query, original_query])
    variants = [
        " ".join([*hint_terms[:8], *lane.search_terms[:8]]),
        lane.query,
        " ".join([lane.label, *lane.search_terms[:10]]),
        original_preserving_variant,
    ]
    queries: list[str] = []
    seen: set[str] = set()
    for variant in variants:
        text = " ".join(str(variant or "").split())
        key = text.lower()
        if text and key not in seen:
            queries.append(text)
            seen.add(key)
    if (
        temporal_routing_enabled(settings)
        and detect_temporal_intent(original_query).active
    ):
        combined = " ".join(str(original_preserving_variant or "").split())
        if combined:
            first_two = [query for query in queries if query.lower() != combined.lower()][
                :2
            ]
            return [*first_two, combined]
    return queries[:3]


def _select_evidence_plan_sources(
    base_sources: list[SourceChunk],
    support_sources: list[SourceChunk],
    *,
    max_sources: int,
) -> list[SourceChunk]:
    max_sources = max(
        1, int(max_sources or len(base_sources) or len(support_sources) or 1)
    )
    if not support_sources:
        return base_sources[:max_sources]
    support_keys = {str(source.chunk_id or "") for source in support_sources}
    deduped_base = [
        source
        for source in base_sources
        if str(source.chunk_id or "") not in support_keys
    ]
    base_budget = max(0, max_sources - len(support_sources))
    selected = [*deduped_base[:base_budget], *support_sources[:max_sources]]
    if len(selected) < max_sources:
        used = {str(source.chunk_id or "") for source in selected}
        for source in deduped_base[base_budget:]:
            key = str(source.chunk_id or "")
            if key in used:
                continue
            selected.append(source)
            used.add(key)
            if len(selected) >= max_sources:
                break
    return selected[:max_sources]


async def _enforce_evidence_plan_lanes(
    *,
    original_query: str,
    sources: list[SourceChunk],
    evidence_plan: EvidencePlan,
    corpus_ids: list[str] | None,
    retrieval_tier: RetrievalTier,
    collections: list[str] | None,
    retrieval_k: int | None,
    top_k_summary: int | None,
    rerank_top_n: int | None,
    similarity_threshold: float | None,
    neo4j_expansion_cap: int | None,
    max_corpora_per_query: int | None,
    fact_seed_limit: int | None,
    final_top_k: int | None,
    source_cap: int | None = None,
    support_semaphore: "asyncio.Semaphore | None" = None,
    enabled: bool = True,
) -> tuple[list[SourceChunk], dict[str, Any]]:
    base_sources = list(sources or [])
    meta: dict[str, Any] = {
        "active": bool(enabled and evidence_plan.active),
        "feature_enabled": bool(enabled),
        "plan_active": evidence_plan.active,
        "plan": evidence_plan_to_dict(evidence_plan),
        "mode": evidence_plan.mode,
        "reason": evidence_plan.reason,
        "required_lanes": [lane.name for lane in evidence_plan.required_lanes],
        "initial": {},
        "final": {},
        "covered_lanes": [],
        "missing_lanes": [],
        "added": 0,
        "support_doc_ids": [],
        "support_lanes": [],
        "lane_reports": [],
        "support_search_mode": None,
    }
    if not enabled:
        meta["skipped"] = "relationship_evidence_allocation_disabled_or_ineligible"
        return base_sources, meta
    if not evidence_plan.active:
        return base_sources, meta

    initial = _evidence_lane_coverage(base_sources, evidence_plan)
    meta["initial"] = initial
    missing = [
        lane
        for lane in evidence_plan.required_lanes
        if lane.name in set(initial.get("missing_lanes") or [])
    ]
    if not corpus_ids or not missing:
        meta["final"] = initial
        meta["covered_lanes"] = initial.get("covered_lanes", [])
        meta["missing_lanes"] = initial.get("missing_lanes", [])
        return base_sources, meta

    meta["support_search_mode"] = "local"
    existing_chunk_ids = {str(source.chunk_id or "") for source in base_sources}
    existing_doc_ids = {
        _evidence_source_doc_key(source)
        for source in base_sources
        if _evidence_source_doc_key(source)
    }
    support_sources: list[SourceChunk] = []
    support_tier = (
        RetrievalTier.qdrant_mongo
        if retrieval_tier == RetrievalTier.qdrant_mongo_graph
        else retrieval_tier
    )
    evidence_semaphore = support_semaphore or asyncio.Semaphore(
        _CHAT_COVERAGE_MAX_CONCURRENCY
    )

    async def _cover_one_lane(
        lane: EvidenceLane,
        semantic_hints: dict[str, Any],
    ) -> tuple[EvidenceLane, list[SourceChunk], dict[str, Any], dict[str, Any], str]:
        lane_report: dict[str, Any] = {
            "lane": lane.name,
            "label": lane.label,
            "status": "uncovered",
            "attempts": [],
        }
        # Reserve up to the side's min_sources DISTINCT-document chunks so one
        # side of a multi-document question gets real depth, not a single quote.
        # Documents already in the context (base + other lanes) are excluded so
        # the side is backed by its OWN books.
        initial_lane_doc_count = len(
            (initial.get("lane_doc_ids") or {}).get(lane.name, [])
        )
        initial_lane_chunk_count = len(
            (initial.get("lane_chunk_ids") or {}).get(lane.name, [])
        )
        target_k = max(
            1,
            int(getattr(lane, "min_sources", 1) or 1) - initial_lane_doc_count,
        )
        same_doc_target_k = max(
            0,
            int(getattr(lane, "min_sources", 1) or 1) - initial_lane_chunk_count,
        )
        chosen_list: list[SourceChunk] = []
        chosen_report: dict[str, Any] = {}
        support_query = ""
        lane_chunk_ids: set[str] = set(existing_chunk_ids)
        lane_doc_ids: set[str] = set(existing_doc_ids)
        async with evidence_semaphore:
            lane_report["semantic_ingest_hints"] = {
                "terms": semantic_hints.get("terms", [])[:10],
                "doc_ids": semantic_hints.get("doc_ids", [])[:8],
                "facets": semantic_hints.get("facets", [])[:4],
            }
            semantic_doc_ids = {
                str(doc_id)
                for doc_id in (semantic_hints.get("doc_ids") or [])
                if str(doc_id).strip()
            }
            for support_query in _evidence_support_query_variants(
                lane,
                original_query,
                semantic_hints=semantic_hints,
            ):
                if len(chosen_list) >= target_k:
                    break
                attempt: dict[str, Any] = {
                    "query": _clip_trace_value(support_query, 220),
                    "search_mode": "local",
                    "returned": 0,
                    "status": "started",
                }
                # Cross-encoder rerank for support pools (RERANK_EVIDENCE_SUPPORT,
                # default OFF — Metal contention A/B, see config.py; Q1 2026-07-03
                # re-verified in situ): lane selection is lexical, so an un-reranked pool
                # surfaces the right BOOK but often the wrong PASSAGE (live probe
                # 2026-07-01: Le Guin doc-hit/passage-miss). When reranking, pull
                # a tighter pool — the cross-encoder pays per candidate and the
                # lane selector only needs target_k good rows. The knob's off
                # position restores the previous shape exactly.
                support_rerank = _rerank_evidence_support()
                try:
                    result = await retriever_orchestrator.retrieve(
                        query=support_query,
                        corpus_ids=corpus_ids,
                        retrieval_tier=support_tier,
                        collections=collections,
                        retrieval_k=(
                            max(16, min(int(retrieval_k or 24), 32))
                            if support_rerank
                            else max(24, min(int(retrieval_k or 40), 56))
                        ),
                        rerank_enabled=support_rerank,
                        support_profile=True,
                        ranking_query=support_query,
                        top_k_summary=top_k_summary,
                        rerank_top_n=max(12, min(int(rerank_top_n or 24), 32)),
                        similarity_threshold=similarity_threshold,
                        neo4j_expansion_cap=neo4j_expansion_cap,
                        max_corpora_per_query=max_corpora_per_query,
                        # Pull a wide-enough pool so target_k distinct documents
                        # are actually reachable for this side.
                        final_top_k=(
                            max(6, target_k * 4)
                            if support_rerank
                            else max(8, target_k * 6)
                        ),
                        fact_seed_limit=fact_seed_limit,
                        search_mode="local",
                    )
                except Exception as exc:
                    attempt["status"] = "retrieval_error"
                    attempt["error"] = _clip_trace_value(exc, 180)
                    lane_report["attempts"].append(attempt)
                    logger.debug(
                        "evidence-plan support retrieval skipped for %s: %s",
                        lane.name,
                        exc,
                    )
                    continue
                candidates = list(getattr(result, "chunks", []) or [])
                attempt["returned"] = len(candidates)
                picks, chosen_report = _choose_evidence_lane_candidates_with_report(
                    candidates,
                    lane=lane,
                    original_query=original_query,
                    existing_chunk_ids=lane_chunk_ids,
                    existing_doc_ids=lane_doc_ids,
                    semantic_doc_ids=semantic_doc_ids,
                    target_k=max(1, target_k - len(chosen_list)),
                    same_doc_target_k=max(0, same_doc_target_k - len(chosen_list)),
                )
                allow_same_doc_deepening = (
                    chosen_report.get("reason")
                    == "same_doc_deepening_after_new_doc_exhausted"
                )
                for pick in picks:
                    pick_chunk_id = str(pick.chunk_id or "")
                    pick_doc_id = _evidence_source_doc_key(pick)
                    if pick_chunk_id and pick_chunk_id in lane_chunk_ids:
                        continue
                    if (
                        pick_doc_id
                        and pick_doc_id in lane_doc_ids
                        and not allow_same_doc_deepening
                    ):
                        continue
                    chosen_list.append(pick)
                    if pick_chunk_id:
                        lane_chunk_ids.add(pick_chunk_id)
                    if pick_doc_id and not allow_same_doc_deepening:
                        lane_doc_ids.add(pick_doc_id)
                    if len(chosen_list) >= target_k:
                        break
                attempt.update(
                    {
                        "status": "selected"
                        if chosen_list
                        else chosen_report.get("status", "uncovered"),
                        "selected_count": len(chosen_list),
                        "selected": chosen_report.get("selected"),
                        "reason": chosen_report.get("reason"),
                    }
                )
                lane_report["attempts"].append(attempt)
        return lane, chosen_list, chosen_report, lane_report, support_query

    # De-stagger (task #6, probe-driven 2026-07-01): hints used to be awaited
    # per-lane INSIDE the semaphore, so lanes started their retrievals at
    # different moments — their query embeds missed the _QueryEmbedBatcher
    # window and serialized on the Metal GPU (measured embed= 0.13s -> 2.5-4.2s).
    # Phase 1: fan out ALL lane hint lookups concurrently (independent, light).
    # Phase 2: lanes proceed to retrieval together; simultaneous embeds coalesce.
    _EMPTY_HINTS: dict[str, Any] = {"facets": [], "terms": [], "doc_ids": []}
    hint_results = await asyncio.gather(
        *[
            _semantic_ingest_hints_for_evidence_lane(lane, corpus_ids)
            for lane in missing
        ],
        return_exceptions=True,
    )
    lane_hints: list[dict[str, Any]] = []
    for lane, hints in zip(missing, hint_results):
        if isinstance(hints, dict):
            lane_hints.append(hints)
        else:
            logger.debug(
                "evidence-plan semantic hints failed for %s: %s", lane.name, hints
            )
            lane_hints.append(dict(_EMPTY_HINTS))
    lane_results = await asyncio.gather(
        *[_cover_one_lane(lane, hints) for lane, hints in zip(missing, lane_hints)]
    )
    for lane, chosen_list, chosen_report, lane_report, support_query in lane_results:
        if not chosen_list:
            lane_report["reason"] = "no_support_chunk_selected"
            meta["lane_reports"].append(lane_report)
            continue
        added_for_lane = 0
        selected_docs: list[str] = []
        selected_chunks: list[str] = []
        for chosen in chosen_list:
            chunk_key = str(chosen.chunk_id or "")
            if chunk_key and chunk_key in existing_chunk_ids:
                continue
            strength = (
                "strong"
                if _evidence_lane_match_score(chosen, lane)
                >= _EVIDENCE_LANE_STRONG_SCORE
                else "weak"
            )
            marked = _mark_evidence_plan_chunk(
                chosen,
                lane=lane,
                support_query=support_query,
                support_strength=strength,
            )
            support_sources.append(marked)
            existing_chunk_ids.add(str(marked.chunk_id or ""))
            doc_key = _evidence_source_doc_key(marked)
            if doc_key:
                existing_doc_ids.add(doc_key)
                selected_docs.append(doc_key)
            selected_chunks.append(str(marked.chunk_id or ""))
            added_for_lane += 1
        if not added_for_lane:
            lane_report["status"] = "deduped_parallel"
            lane_report["reason"] = "duplicate_selected_by_another_lane"
            meta["lane_reports"].append(lane_report)
            continue
        lane_report["status"] = "selected"
        lane_report["selected_count"] = added_for_lane
        lane_report["selected_doc_ids"] = selected_docs
        lane_report["selected_chunk_ids"] = selected_chunks
        meta["lane_reports"].append(lane_report)

    _base_budget = int(final_top_k or len(base_sources) or 8)
    # Support is ADDITIVE: per-side reservations grow the final context rather
    # than displacing the base ranking, so every side gets depth. The downstream
    # per-doc cap (per_doc_cap_for_plan, applied in the stream path) trims any
    # single over-represented book back to its fair share.
    max_sources = max(_base_budget, int(source_cap or 0)) + len(support_sources)
    merged = _select_evidence_plan_sources(
        base_sources,
        support_sources,
        max_sources=max_sources,
    )
    actual_support = [
        source
        for source in merged
        if isinstance(source.metadata, dict)
        and source.metadata.get("support_role") == "evidence_plan_lane"
    ]
    final = _evidence_lane_coverage(merged, evidence_plan)
    final_undercovered = [
        str(name) for name in (final.get("missing_lanes") or []) if name
    ]
    final_lane_doc_ids = (
        final.get("lane_doc_ids") if isinstance(final.get("lane_doc_ids"), dict) else {}
    )
    final_thin_lanes = [
        lane_name
        for lane_name in final_undercovered
        if final_lane_doc_ids.get(lane_name)
    ]
    final_missing_lanes = [
        lane_name
        for lane_name in final_undercovered
        if lane_name not in set(final_thin_lanes)
    ]
    final["undercovered_lanes"] = final_undercovered
    final["thin_lanes"] = final_thin_lanes
    final["missing_lanes"] = final_missing_lanes
    meta["final"] = final
    meta["covered_lanes"] = final.get("covered_lanes", [])
    meta["missing_lanes"] = final_missing_lanes
    meta["undercovered_lanes"] = final_undercovered
    meta["thin_lanes"] = final_thin_lanes
    meta["added"] = len(actual_support)
    meta["support_doc_ids"] = [
        _evidence_source_doc_key(source)
        for source in actual_support
        if _evidence_source_doc_key(source)
    ]
    meta["support_lanes"] = [
        str(source.metadata.get("support_lane") or "")
        for source in actual_support
        if isinstance(source.metadata, dict)
    ]
    for report in meta.get("lane_reports", []):
        lane = str(report.get("lane") or "")
        if lane in meta["missing_lanes"] and report.get("status") == "selected":
            report["status"] = "selected_but_not_in_final_packet"
    return merged, meta


def _format_evidence_plan_trace(meta: dict[str, Any]) -> str:
    plan = meta.get("plan") if isinstance(meta.get("plan"), dict) else {}
    lanes = [
        str(lane.get("name") or "")
        for lane in (plan.get("lanes") or [])
        if isinstance(lane, dict) and str(lane.get("name") or "")
    ]
    covered = [str(name) for name in (meta.get("covered_lanes") or []) if name]
    missing = [str(name) for name in (meta.get("missing_lanes") or []) if name]
    thin = [str(name) for name in (meta.get("thin_lanes") or []) if name]
    final = meta.get("final") if isinstance(meta.get("final"), dict) else {}
    lines = [
        "[Evidence plan]",
        f"mode: {meta.get('mode') or plan.get('mode') or 'unknown'}",
        f"required_lanes: {', '.join(lanes) if lanes else 'none'}",
        f"covered: {', '.join(covered) if covered else 'none'}",
        f"thin: {', '.join(thin) if thin else 'none'}",
        f"missing: {', '.join(missing) if missing else 'none'}",
        (
            "support: "
            f"added={meta.get('added', 0)} "
            f"docs={', '.join(meta.get('support_doc_ids') or []) or 'none'}"
        ),
        (
            "lane_docs: "
            f"{json.dumps(final.get('lane_doc_names', {}), sort_keys=True) if final else '{}'}"
        ),
        "rule: each required lane must have retrieved source evidence before synthesis.",
    ]
    return "\n".join(lines)


def _format_evidence_plan_prompt_note(meta: dict[str, Any] | None) -> str | None:
    if not meta or not meta.get("active"):
        return None
    required = ", ".join(meta.get("required_lanes") or []) or "none"
    covered = ", ".join(meta.get("covered_lanes") or []) or "none"
    missing = ", ".join(meta.get("missing_lanes") or []) or "none"
    thin = ", ".join(meta.get("thin_lanes") or []) or "none"
    lines = [
        "Internal evidence plan contract (do not mention this block):",
        f"- Required source-backed evidence lanes: {required}.",
        f"- Retrieved lane coverage: covered={covered}; thin={thin}; missing={missing}.",
    ]
    plan = meta.get("plan") if isinstance(meta.get("plan"), dict) else {}
    lane_rows = {
        str(lane.get("name") or ""): lane
        for lane in (plan.get("lanes") or [])
        if isinstance(lane, dict) and str(lane.get("name") or "")
    }
    covered_requirements: list[str] = []
    for lane_name in meta.get("covered_lanes") or []:
        lane = lane_rows.get(str(lane_name or "")) or {}
        label = " ".join(
            str(lane.get("label") or lane_name or "").replace("_", " ").split()
        )
        evidence_need = " ".join(str(lane.get("query") or "").split())
        if len(evidence_need) > 220:
            evidence_need = evidence_need[:217].rstrip() + "..."
        if label:
            covered_requirements.append(
                f"  {len(covered_requirements) + 1}. {label}"
                + (f" - {evidence_need}" if evidence_need else "")
            )
    if covered_requirements:
        lines.extend(
            [
                "- Final-answer coverage checklist (internal):",
                *covered_requirements,
                (
                    "- Before finalizing, silently verify that every checklist item "
                    "appears as at least one explicit, source-grounded claim, "
                    "recommendation, step, or caveat. Do not leave a covered lane "
                    "merely implied, and do not name the lane/checklist machinery."
                ),
            ]
        )
    if meta.get("missing_lanes"):
        lines.append(
            "- HARD LIMIT: do not synthesize a full relationship answer across "
            "lanes that are still missing retrieved evidence."
        )
    elif meta.get("thin_lanes"):
        lines.append(
            "- The retrieved packet has at least one source for every required "
            "lane, but some lanes are thin. Synthesize carefully and avoid "
            "overstating the thin lanes."
        )
    else:
        lines.append(
            "- The retrieved packet contains evidence for every required lane. "
            "Synthesize across lanes, keep claims tied to what the sources show, "
            "and satisfy the explicit final-answer checklist above."
        )
    return "\n".join(lines)


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
    fetches = (
        pipeline.get("fetches") if isinstance(pipeline.get("fetches"), list) else []
    )
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
        pipeline.get("final_reranked_results") or payload.get("reranked_results") or 0
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
        "<" in text or "{" in text or "invoke" in text or "parameter" in text
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
            schema for schema in available if _tool_schema_name(schema) == "web_search"
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
    return [name for schema in tool_schemas if (name := _tool_schema_name(schema))]


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

        data["text"] = (
            _clip_source_text(data.get("text"), _MAX_PERSISTED_SOURCE_TEXT_CHARS) or ""
        )
        if data.get("summary"):
            data["summary"] = _clip_source_text(
                data.get("summary"), _MAX_PERSISTED_SOURCE_SUMMARY_CHARS
            )
        if isinstance(data.get("provenance"), list):
            data["provenance"] = data["provenance"][:5]
        metadata = data.get("metadata")
        if isinstance(metadata, dict) and isinstance(
            metadata.get("atomic_claim_anchors"), list
        ):
            if not bool(getattr(settings, "ATOMIC_CLAIM_ANCHORS_ENABLED", False)):
                metadata = dict(metadata)
                metadata.pop("atomic_claim_anchors", None)
                data["metadata"] = metadata
            else:
                anchor_preview_cap = max(
                    1, int(getattr(settings, "ATOMIC_CLAIM_ANCHORS_PER_SOURCE", 2))
                )
                compact_anchors: list[dict[str, Any]] = []
                for anchor in metadata["atomic_claim_anchors"][:anchor_preview_cap]:
                    if not isinstance(anchor, dict):
                        continue
                    compact_anchors.append(
                        {
                            key: value
                            for key, value in {
                                "schema_version": anchor.get("schema_version"),
                                "claim_id": anchor.get("claim_id"),
                                "claim_text": _clip_source_text(
                                    anchor.get("claim_text"), 240
                                ),
                                "exact_sentence": _clip_source_text(
                                    anchor.get("exact_sentence"), 500
                                ),
                                "evidence_ref_id": anchor.get("evidence_ref_id"),
                                "source_version_id": anchor.get("source_version_id"),
                                "child_id": anchor.get("child_id"),
                                "selected_chunk_id": anchor.get("selected_chunk_id"),
                                "mapped_parent_id": anchor.get("mapped_parent_id"),
                                "start": anchor.get("start"),
                                "end": anchor.get("end"),
                                "compilation_revision_id": anchor.get(
                                    "compilation_revision_id"
                                ),
                            }.items()
                            if value is not None
                        }
                    )
                metadata = dict(metadata)
                metadata["atomic_claim_anchors"] = compact_anchors
                data["metadata"] = metadata

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
    """True only for the Neo4j-backed Graph Augmentation retrieval tier."""
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
    header.append(
        "broad_concept_rule: If the user's term is overloaded and retrieved "
        "evidence spans multiple meanings, answer anyway. Start with the "
        "common core, then group by meaning only where it helps; do not ask "
        "for clarification as a substitute for answering."
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
                "The answer should feel structurally different from Fast Search "
                "and Hybrid Search: relationship-first, not merely a definition or a list "
                "of source excerpts."
            ),
        ]
    else:
        body = [
            "Required answer shape: hydrated corpus synthesis.",
            (
                "This lens overrides the default short-answer compression. Do "
                "not stop at the same generic definition Fast Search would give."
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
                "The answer should feel deeper than Fast Search: more grounded, "
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
    """.split()
)

_BROAD_CONCEPT_FRAME_RULES: dict[str, tuple[dict[str, Any], ...]] = {
    "ontology": (
        {
            "frame": "technical ontology / knowledge graph",
            "patterns": (
                r"\bknowledge graphs?\b",
                r"\bsemantic web\b",
                r"\blinked data\b",
                r"\brdf\b",
                r"\bowl\b",
                r"\bschema\b",
                r"\bcyc\b",
                r"\bdomain model\b",
                r"\bupper[- ]level ontology\b",
                r"\bformal(?:ly)? represented\b",
                r"\bquery languages?\b",
            ),
        },
        {
            "frame": "NLP / language-system ontology",
            "patterns": (
                r"\bnatural language\b",
                r"\bnlp\b",
                r"\blanguage generation\b",
                r"\blanguage processing\b",
                r"\bcomputational linguistics\b",
                r"\btext generation\b",
                r"\bdiscourse\b",
                r"\bsemantic types?\b",
            ),
        },
        {
            "frame": "philosophical ontology / being",
            "patterns": (
                r"\bbeing\b",
                r"\bexistence\b",
                r"\breality\b",
                r"\bmetaphysic",
                r"\bepistemolog",
                r"\bstate of being\b",
                r"\bwhat there is\b",
                r"\btrue score\b",
            ),
        },
        {
            "frame": "social or self ontology",
            "patterns": (
                r"\bsocial ontology\b",
                r"\bself\b",
                r"\bidentity\b",
                r"\bsubjectiv",
                r"\bpersonal experience\b",
                r"\bsocial construction\b",
                r"\binstitution",
                r"\bcollective intentionality\b",
            ),
        },
    )
}


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


def _query_broad_concepts(query: str | None) -> list[str]:
    text = str(query or "").lower()
    concepts: list[str] = []
    if re.search(r"\bontolog(?:y|ies|ical|ically)\b", text):
        concepts.append("ontology")
    return concepts


def _detect_broad_concept_frames(
    *,
    query: str | None,
    source_texts: list[tuple[str, str]],
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Detect overloaded concept frames represented in the retrieved packet.

    This is deliberately tiny and deterministic. It does not classify the
    user's intent; it tells the answer model, "the retrieved evidence spans
    these senses, so answer the question by grouping them instead of silently
    choosing one or asking for clarification."
    """

    concepts = _query_broad_concepts(query)
    if not concepts or not source_texts:
        return []

    frames: list[dict[str, Any]] = []
    for concept in concepts:
        for rule in _BROAD_CONCEPT_FRAME_RULES.get(concept, ()):
            patterns = [
                re.compile(pattern, re.IGNORECASE) for pattern in rule["patterns"]
            ]
            matches: list[dict[str, Any]] = []
            terms: set[str] = set()
            for title, text in source_texts:
                haystack = f"{title}\n{text}"
                matched_terms = []
                for pattern in patterns:
                    found = pattern.search(haystack)
                    if not found:
                        continue
                    token = re.sub(r"\s+", " ", found.group(0).strip())
                    matched_terms.append(token)
                    terms.add(token.lower())
                if matched_terms:
                    matches.append(
                        {
                            "source": title or "retrieved source",
                            "terms": matched_terms[:3],
                        }
                    )
            if len(matches) < 1:
                continue
            frames.append(
                {
                    "concept": concept,
                    "frame": str(rule["frame"]),
                    "source_count": len({m["source"] for m in matches}),
                    "terms": sorted(terms)[:6],
                }
            )

    frames.sort(key=lambda row: (-int(row.get("source_count") or 0), row["frame"]))
    return frames[:limit]


def _build_retrieval_nuance_digest(
    *,
    tier: Any,
    query: str | None = None,
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
    source_texts: list[tuple[str, str]] = []

    for source in sources or []:
        title, text = _retrieval_nuance_source_text(source)
        source_texts.append((title, text))
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
        for term in _retrieval_nuance_rank_terms(
            token_tf, token_df, phrase=False, limit=12
        )
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
        "broad_concept_frames": _detect_broad_concept_frames(
            query=query,
            source_texts=source_texts,
        ),
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
    broad_frames = [
        item
        for item in (digest.get("broad_concept_frames") or [])
        if isinstance(item, dict) and item.get("concept") and item.get("frame")
    ][:4]
    if not any((terms, relationships, broad_frames)):
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
    if broad_frames:
        lines.append("broad_concept_frames:")
        for item in broad_frames:
            terms_text = ", ".join(str(term) for term in (item.get("terms") or [])[:4])
            suffix = f" ({terms_text})" if terms_text else ""
            lines.append(
                f"- {item.get('concept')}: {item.get('frame')}"
                f"; sources={item.get('source_count', 1)}{suffix}"
            )
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
            (
                "- If broad_concept_frames are present, the query term is "
                "overloaded in the retrieved evidence. Do NOT ask the user to "
                "clarify and do NOT silently choose one sense. Answer the "
                "question directly first, then group the explanation by the "
                "frames only as much as needed."
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
    funnel_s = float(
        timings.get("funnels") or timings.get("candidate_generation") or 0.0
    )
    hydrate_s = float(timings.get("hydrate") or timings.get("hydrate_finalists") or 0.0)
    final_mix = (
        ", ".join(
            f"{tier}={count}" for tier, count in sorted(final_source_tiers.items())
        )
        if isinstance(final_source_tiers, dict) and final_source_tiers
        else "none"
    )
    sufficiency = {}
    selection = diag.get("selection") if isinstance(diag.get("selection"), dict) else {}
    if isinstance(selection.get("sufficiency"), dict):
        sufficiency = selection["sufficiency"]
    suff_missing = [
        str(atom)
        for atom in (sufficiency.get("missing_atoms") or [])
        if str(atom).strip()
    ]
    vocabulary = (
        diag.get("vocabulary_resolution")
        if isinstance(diag.get("vocabulary_resolution"), dict)
        else {}
    )
    vocabulary_matches = [
        row for row in (vocabulary.get("matches") or []) if isinstance(row, dict)
    ]
    vocabulary_labels = [
        str(row.get("term") or row.get("canonical_name") or "").strip()
        for row in vocabulary_matches
        if str(row.get("term") or row.get("canonical_name") or "").strip()
    ]
    vocabulary_expansion = (
        vocabulary.get("expansion")
        if isinstance(vocabulary.get("expansion"), dict)
        else {}
    )
    grounded_planner = (
        vocabulary.get("grounded_planner")
        if isinstance(vocabulary.get("grounded_planner"), dict)
        else {}
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
            "answerability: "
            f"{'yes' if sufficiency.get('answerable') else 'no'} "
            f"coverage={sufficiency.get('required_coverage', '?')} "
            f"missing={', '.join(suff_missing) if suff_missing else 'none'}"
        )
        if sufficiency
        else "answerability: unavailable",
        (
            # Doc-spread telemetry — distinct_docs_* are computed in the
            # retriever but were previously dropped here; max_doc_share_final
            # surfaces candidate-collapse (one doc dominating the final set).
            "diversity: "
            f"docs_merged={_count('distinct_docs_merged')} "
            f"docs_in_pool={_count('distinct_docs_in_pool')} "
            f"docs_final={int(diag.get('unique_docs_final') or 0)} "
            f"max_doc_share_final={float(diag.get('max_doc_share_final') or 0.0):.2f}"
        ),
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
            f"funnels={funnel_s:.2f}s "
            f"graph={float(timings.get('graph') or 0):.2f}s "
            f"rerank={float(timings.get('rerank') or 0):.2f}s "
            f"hydrate={hydrate_s:.2f}s"
        ),
        (
            "vocabulary: "
            f"status={vocabulary.get('status') or 'unavailable'} "
            f"matched={', '.join(vocabulary_labels[:6]) if vocabulary_labels else 'none'} "
            f"translated={len(vocabulary_expansion.get('translation_lane_ids') or [])} "
            f"step_back={len(vocabulary_expansion.get('step_back_lane_ids') or [])} "
            f"rejected={len(vocabulary.get('rejected_expansions') or [])} "
            f"stores={','.join(key for key, used in (vocabulary.get('store_usage') or {}).items() if used) or 'none'}"
        ),
        (
            "grounded_planner: "
            f"status={grounded_planner.get('status') or 'skipped'} "
            f"provider_calls={int(grounded_planner.get('provider_calls') or 0)} "
            f"cache_hit={'yes' if grounded_planner.get('cache_hit') else 'no'}"
        ),
    ]
    if str(effective) == RetrievalTier.qdrant_mongo_graph.value:
        lines.append(
            "graph_advantage: "
            f"facts={_count('facts')} "
            f"fact_seed_chunks={_count('fact_seed_chunks')} "
            f"relations={_count('graph_decorations')} "
            f"expanded_chunks={_count('graph_expanded')} "
            f"seed_chunks={_count('graph_seed_chunks')} "
            f"prefilter={_count('graph_prefilter_pool') or _count('merged_after_graph_boost') or _count('merged_after_graph')} "
            f"mlx_pool={_count('rerank_top_n_graph_cap') or _count('merged_after_rerank_cap')} "
            f"near_duplicates={_count('near_duplicate_pairs')} "
            f"repair_rounds={_count('sufficiency_repair_rounds')}"
        )
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
    "- Write in prose by default, but use Markdown structure when it makes "
    "the answer easier to understand. Bullets, numbered lists, grid-style "
    "Markdown tables, headings, fenced JSON blocks, and fenced text diagrams "
    "are allowed when the answer has parts, comparisons, steps, structured "
    "data, evidence, or system structure.\n"
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
    "- If the user asks about a broad or overloaded concept and the retrieved "
    "evidence frames it in multiple ways, still answer the question. Do not "
    "stall on clarification and do not silently pick one sense. Start with the "
    "shared answer, then group the important senses only as needed.\n"
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
    "- Use grid-style Markdown tables only when comparing options, sources, "
    "statuses, tradeoffs, fields, scores, or structured data. Keep table "
    "cells short.\n"
    "- Use numbered lists for ordered procedures, sequences, ranking, setup "
    "steps, or diagnostic checklists. Use bullets for unordered groups.\n"
    "- If the user asks for JSON, schema, entity extraction, spans, offsets, "
    "or structured examples, include a fenced `json` block and preserve "
    "field names exactly when the user provides them.\n"
    "- Put install/run commands in fenced shell blocks so the UI can render "
    "them as command cards.\n"
    "- No exclamation marks unless quoting. No 'Great question!' preambles. "
    "Use visual markers only as semantic navigation, never decoration. The "
    "safe palette is → for reasoning bridges, ✓/✗ for binary status, and a "
    "single warning marker for real risks or failure modes.\n"
    "- Default to the KVP list pattern (`**key:** value`) for any factual "
    "rundown of 2-6 attributes. Default heading hierarchy is h2 then h3.\n"
    "- Use Markdown as the rendering language for all assistant answers. "
    "The UI supports headings, bold text, bullets, numbered lists, GFM "
    "grid tables, blockquotes, fenced code blocks, fenced `json` examples, "
    "and plain-text ASCII diagrams; choose the smallest visual surface that "
    "makes the answer clearer.\n"
    "- When the answer involves a process, pipeline, architecture, graph, "
    "data flow, causal chain, or retrieval stack, include a compact ASCII "
    "diagram in a fenced `text` block when it will clarify the structure. "
    "Do not use ASCII art as decoration.\n"
    "- When the answer compares options or evaluates evidence, prefer a "
    "short table with 3-5 columns. When the answer contains numeric counts "
    "or scores from retrieved evidence, a tiny ASCII bar chart is allowed; "
    "never invent numbers just to make a chart.\n"
    "- For direct factual questions, do not over-format. A bold thesis plus "
    "one short paragraph is better than a template.\n"
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
    "- For retrieval, data, code, graph, ontology, or system-design answers, "
    "the preferred visual grammar is: bold thesis → compact table or ASCII "
    "map if useful → explanatory prose → caveats/next validation. Use this "
    "grammar without naming it.\n"
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
    "Mandatory display contract:\n"
    "- For answers with two or more moving parts, do not return a plain wall "
    "of prose. Use at least one visible structure: a compact table, a short "
    "`**key:** value` block, a fenced `text` map, or content-driven headings.\n"
    "- For graph, ontology, retrieval, data-flow, pipeline, architecture, or "
    "system-design questions, include a fenced `text` ASCII map unless the "
    "answer is genuinely one paragraph.\n"
    "- For comparison or tradeoff questions, include a Markdown table unless "
    "there are fewer than two comparable items.\n"
    "- For why/explain questions, open with a bold thesis and then break the "
    "reasons into either `**key:** value` lines or compact bullets.\n"
    "- When the user explicitly asks for tables, grid tables, bullets, "
    "numbered lists, or JSON examples, satisfy that requested display form "
    "instead of reverting to prose.\n"
    "- For entity extraction, schema, span, offset, or typed-result examples, "
    'show a fenced `json` block such as `{ "entities": [...] }` when useful.\n'
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
        primary_entry_id: str | None = None

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
            web_search_enabled and not request.selected_tools and not agentic_on_request
        )
        tool_route_active = bool(request.selected_tools or agentic_on_request)
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
                primary_entry_id = str(_resolved.get("entry_id") or _id or "") or None
                profile_creds = {
                    "api_base": _resolved.get("api_base"),
                    "api_key": _resolved.get("api_key"),
                    "extra_params": _resolved.get("extra_params") or None,
                }
                model_used = _resolved["model"]
                logger.info(
                    "%s resolved: user=%s id=%s → %s",
                    prefix,
                    user_id,
                    _id,
                    model_used,
                )
            else:
                logger.warning(
                    "%s not found: user=%s id=%s; "
                    "falling back to DEFAULT_COMPLETION_MODEL.",
                    prefix,
                    user_id,
                    _id,
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
                await resolve_query_model_kind(user_id, "agentic") if user_id else None
            )
            if qres:
                primary_entry_id = str(qres.get("entry_id") or "") or None
                model_used = qres["model"]
                profile_creds = {
                    "api_base": qres["api_base"],
                    "api_key": qres["api_key"],
                    "extra_params": qres["extra_params"] or None,
                }
                logger.info(
                    "Phase F query prefs resolution: user=%s kind=%s → %s",
                    user_id,
                    "agentic",
                    model_used,
                )
            else:
                model_used = settings.AGENTIC_MODEL
                profile_creds = {}
                logger.info(
                    "Agentic env fallback resolution: user=%s kind=agentic → %s",
                    user_id or "-",
                    model_used,
                )

        elif (
            user_id
            and not profile_creds
            and not (
                model_used.startswith("pool:") or model_used.startswith("profile:")
            )
            and not (request.overrides and request.overrides.model)
            and model_used
            in (settings.DEFAULT_COMPLETION_MODEL, settings.AGENTIC_MODEL)
        ):
            qres = await resolve_query_model_kind(user_id, "query")
            if qres:
                primary_entry_id = str(qres.get("entry_id") or "") or None
                model_used = qres["model"]
                profile_creds = {
                    "api_base": qres["api_base"],
                    "api_key": qres["api_key"],
                    "extra_params": qres["extra_params"] or None,
                }
                logger.info(
                    "Phase F query prefs resolution: user=%s kind=%s → %s",
                    user_id,
                    "query",
                    model_used,
                )

        if request.overrides is not None:
            request.overrides.model = model_used

        yield _record_trace_event(
            lane="model_call",
            title="Chat model route",
            status="done",
            content=(
                "Resolved the final chat model before retrieval and tool " "execution."
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

            if attachments_include_image(request.attachments) and not supports_vision(
                model_used
            ):
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
                (hyde_route or {}).get("model") or settings.HYDE_MODEL or model_used
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
        if not hyde_applied:
            retrieval_query = contextualize_followup_query(
                retrieval_query,
                existing_messages,
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

        requested_mode = (
            getattr(request.overrides, "search_mode", None)
            if request.overrides
            else None
        )
        resolved_mode = resolve_search_mode(requested_mode, request.message)
        evidence_plan, evidence_plan_llm_task = await self._resolve_evidence_plan(
            request,
            user_id=user_id,
            retrieval_query=retrieval_query,
            llm_decompose=bool(
                settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED
                and profile_cfg.get("evidence_plan_llm_decompose")
            ),
            fallback_model=model_used,
            fallback_api_base=profile_creds.get("api_base"),
            fallback_api_key=profile_creds.get("api_key"),
            fallback_extra=profile_creds.get("extra_params"),
        )
        librarian_plan_trace = await _build_librarian_plan_trace(
            query=request.message,
            corpus_ids=request.corpus_ids,
            requested_tier=request.retrieval_tier,
            enabled=bool(getattr(settings, "LIBRARIAN_PLANNER_ENABLED", False)),
            shadow=bool(getattr(settings, "LIBRARIAN_PLANNER_SHADOW", False)),
        )
        query_plan = _build_chat_query_plan(
            query=request.message,
            retrieval_query=retrieval_query,
            requested_tier=request.retrieval_tier,
            corpus_ids=request.corpus_ids,
            collections=request.collections,
            profile_cfg=profile_cfg,
            search_mode=resolved_mode,
            hyde_applied=hyde_applied,
            librarian_plan=librarian_plan_trace,
        )
        yield _record_trace_event(
            lane="planning",
            title="Query plan",
            status="done",
            content=_format_chat_query_plan_trace(query_plan),
            metadata=query_plan,
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

        # Overlap facet detection (its own embed + Qdrant facet search + Mongo
        # lookup) with the main retrieval + intent classifier so its cost is off
        # the critical path. Awaited at the coverage step; falls back to inline
        # detection if it fails. Only meaningful when there is a corpus to scope.
        coverage_facets_task = (
            asyncio.ensure_future(
                _chat_coverage_facets_for_query_with_corpus(
                    request.message, request.corpus_ids
                )
            )
            if request.corpus_ids and not settings.QUERY_PLAN_V2
            else None
        )

        # Step 3.5: Retrieval Pipeline
        #   atomic mode: decompose query → fan-out retrieval → merge
        #   all other modes: standard single-query retrieval
        # Tier-authoritative routing: GLOBAL (the 50-summary overview) is only
        # ever the user's explicit choice. The old LLM "overview intent" second
        # chance silently upgraded local→global, overrode the user's tier, and
        # added an LLM call — removed so the selected tier + mode drive the work.
        if settings.QUERY_PLAN_V2:
            grounded_planner_route = None
            if request.corpus_ids and bool(
                getattr(settings, "GROUNDED_QUERY_PLANNER_ENABLED", False)
            ):
                grounded_planner_route = (
                    await resolve_query_model_kind(user_id, "utility")
                    if user_id
                    else None
                )
                if not grounded_planner_route:
                    grounded_planner_route = {
                        "model": model_used,
                        "api_base": profile_creds.get("api_base"),
                        "api_key": profile_creds.get("api_key"),
                        "extra_params": profile_creds.get("extra_params"),
                        "source": "active_chat_model",
                    }
            retrieval = await retriever_orchestrator.retrieve_planned(
                plan=build_query_plan_v2(
                    request.message,
                    corpus_ids=request.corpus_ids,
                    standalone_query=retrieval_query,
                ),
                corpus_ids=request.corpus_ids,
                retrieval_tier=request.retrieval_tier,
                collections=request.collections,
                retrieval_k=profile_k,
                rerank_enabled=profile_rerank,
                top_k_summary=profile_cfg["top_k_summary"],
                rerank_top_n=profile_cfg["rerank_top_n"],
                final_top_k=profile_cfg["final_top_k"],
                fact_seed_limit=profile_cfg["fact_seed_limit"],
                search_mode=resolved_mode,
                disabled_lexicon_ids=(
                    request.overrides.disabled_lexicon_ids
                    if request.overrides
                    else None
                ),
                grounded_planner_route=grounded_planner_route,
            )
        elif reasoning_mode == "atomic":
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
        retrieval_sources = await _drop_noisy_retrieval_chunks(
            list(retrieval.chunks or [])
        )
        # Fast path: a SPECIFIC single-concept lookup gains nothing from facet
        # coverage or per-side evidence-plan retrievals — each is an extra full
        # retrieve() round-trip that only pays off for broad / multi-document
        # questions. Skip both and answer from the base retrieval. Gated tightly
        # (deterministic intent=specific + <2 evidence lanes + not global mode) so
        # comparative / broad questions keep the full machinery.
        try:
            _fast_intent = infer_retrieval_intent(request.message).need.value
        except Exception:
            _fast_intent = ""
        effective_retrieval_tier = getattr(
            retrieval, "effective_tier", request.retrieval_tier
        )
        _plan_diagnostics = getattr(retrieval, "diagnostics", {}) or {}
        _plan_selection = _plan_diagnostics.get("selection") or {}
        _plan_sufficiency = _plan_selection.get("sufficiency") or {}
        _query_plan_fast_path = bool(
            settings.QUERY_PLAN_V2
            and str(_plan_diagnostics.get("complexity") or "") == "simple"
            and bool(_plan_sufficiency.get("answerable"))
        )
        _retrieval_fast_path = (
            _query_plan_fast_path
            or str(getattr(effective_retrieval_tier, "value", effective_retrieval_tier))
            == RetrievalTier.qdrant_only.value
            or (
                str(_fast_intent or "").lower() == "specific"
                and len(evidence_plan.required_lanes) < 2
                and str(resolved_mode or "").lower() != "global"
            )
        )
        if settings.QUERY_PLAN_V2:
            if evidence_plan_llm_task is not None:
                evidence_plan_llm_task.cancel()
            if coverage_facets_task is not None:
                try:
                    await coverage_facets_task
                except Exception:
                    pass
            coverage_sources = retrieval_sources
            required_coverage = dict(
                _plan_diagnostics.get("required_concept_coverage") or {}
            )
            required_lane_ids = list(required_coverage.get("required_lane_ids") or [])
            covered_lane_ids = list(required_coverage.get("supported_lane_ids") or [])
            covered_lane_set = set(covered_lane_ids)
            missing_lane_ids = [
                lane_id
                for lane_id in required_lane_ids
                if lane_id not in covered_lane_set
            ]
            coverage_meta = {
                "query_plan_v2": True,
                "integrated": True,
                "added": 0,
                "duration_s": perf_counter() - coverage_start,
                "coverage_uncovered_lanes": missing_lane_ids,
                "skipped": "legacy_facet_support_pass",
            }
            relationship_evidence_allocation = bool(
                settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED
                and _relationship_allocation_eligible(evidence_plan)
            )
            if relationship_evidence_allocation:
                evidence_plan_start = perf_counter()
                (
                    evidence_sources,
                    evidence_plan_meta,
                ) = await _enforce_evidence_plan_lanes(
                    original_query=request.message,
                    sources=retrieval_sources,
                    evidence_plan=evidence_plan,
                    corpus_ids=request.corpus_ids,
                    retrieval_tier=getattr(
                        retrieval, "effective_tier", request.retrieval_tier
                    ),
                    collections=request.collections,
                    retrieval_k=profile_k,
                    top_k_summary=profile_cfg["top_k_summary"],
                    rerank_top_n=profile_cfg["rerank_top_n"],
                    similarity_threshold=profile_cfg["similarity_threshold"],
                    neo4j_expansion_cap=profile_cfg["neo4j_expansion_cap"],
                    max_corpora_per_query=profile_cfg["max_corpora_per_query"],
                    fact_seed_limit=profile_cfg["fact_seed_limit"],
                    final_top_k=profile_cfg["final_top_k"],
                    source_cap=profile_cfg.get("source_cap"),
                    enabled=True,
                )
                evidence_plan_meta["duration_s"] = perf_counter() - evidence_plan_start
                evidence_plan_meta["query_plan_v2"] = True
                evidence_plan_meta["integrated_required_lanes"] = required_lane_ids
                evidence_plan_meta["integrated_covered_lanes"] = covered_lane_ids
            else:
                evidence_sources = coverage_sources
                evidence_plan_meta = {
                    "active": True,
                    "feature_enabled": bool(
                        settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED
                    ),
                    "eligible": _relationship_allocation_eligible(evidence_plan),
                    "mode": "query_plan_v2_integrated",
                    "plan": evidence_plan_to_dict(evidence_plan),
                    "required_lanes": required_lane_ids,
                    "covered_lanes": covered_lane_ids,
                    "missing_lanes": missing_lane_ids,
                    "lane_reports": _plan_diagnostics.get("lanes") or [],
                    "added": 0,
                    "duration_s": 0.0,
                    "skipped": "relationship_evidence_allocation_disabled_or_ineligible",
                }
        elif _retrieval_fast_path:
            if evidence_plan_llm_task is not None:
                evidence_plan_llm_task.cancel()
            # Don't leak the concurrently-started coverage-facet prefetch.
            if coverage_facets_task is not None:
                try:
                    await coverage_facets_task
                except Exception:
                    pass
            coverage_sources = retrieval_sources
            coverage_meta = {
                "fast_path": True,
                "skipped": "facet_coverage+evidence_plan",
                "intent": _fast_intent,
                "duration_s": perf_counter() - coverage_start,
            }
            evidence_sources = coverage_sources
            evidence_plan_meta = {"active": False, "fast_path": True, "duration_s": 0.0}
            logger.info(
                "chat_fast_path: tier=%s intent=%s — skipped coverage+evidence (%d sources)",
                str(
                    getattr(effective_retrieval_tier, "value", effective_retrieval_tier)
                ),
                _fast_intent,
                len(coverage_sources),
            )
        else:
            precomputed_coverage_facets = None
            if coverage_facets_task is not None:
                try:
                    precomputed_coverage_facets = await coverage_facets_task
                except Exception as exc:
                    logger.debug(
                        "coverage facet prefetch failed; detecting inline: %s", exc
                    )
            # Speed campaign (2026-07-02): coverage and evidence-plan used to
            # run SEQUENTIALLY (~8s + ~7.5s measured) even though they fill
            # ORTHOGONAL gaps — coverage adds facet evidence, evidence-plan
            # adds per-lane depth. Both now run in parallel against the MAIN
            # retrieval's sources; the chunk_id-deduped union below plus the
            # existing per-doc cap absorb any overlap. One SHARED semaphore
            # keeps total concurrent support retrievals at
            # _CHAT_COVERAGE_MAX_CONCURRENCY — without it the parallel passes
            # would double Metal/event-loop pressure and re-create the
            # uniform-stall regime the funnel_detail probes measured.
            # Adopt the LLM-refined plan if its background racer finished in
            # time (it overlapped the main retrieval; internally bounded by
            # _EVIDENCE_LLM_DECOMPOSE_DEADLINE).
            if evidence_plan_llm_task is not None:
                try:
                    refined_plan = await evidence_plan_llm_task
                except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
                    logger.debug("evidence-plan LLM refinement dropped: %s", exc)
                    refined_plan = None
                if refined_plan is not None:
                    evidence_plan = refined_plan
            relationship_evidence_allocation = bool(
                settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED
                and _relationship_allocation_eligible(evidence_plan)
            )
            shared_support_semaphore = asyncio.Semaphore(_CHAT_COVERAGE_MAX_CONCURRENCY)
            # P1.5 shelf_reserve (dark behind SHELF_RESERVE_ENABLED): resolve
            # deterministic query concept ids ONLY for corpus-scoped requests.
            # Source choice documented in _shelf_reserve_query_concepts
            # (vocabulary_resolution canonical keys, concept_groups fallback).
            shelf_reserve_concepts: list[str] | None = None
            if settings.SHELF_RESERVE_ENABLED and (request.corpus_ids or []):
                shelf_reserve_concepts = _shelf_reserve_query_concepts(
                    request.message,
                    _plan_diagnostics,
                )
            evidence_plan_start = perf_counter()
            (
                (coverage_sources, coverage_meta),
                (evidence_only_sources, evidence_plan_meta),
            ) = await asyncio.gather(
                _enforce_chat_query_coverage(
                    original_query=request.message,
                    retrieval_query=retrieval_query,
                    sources=retrieval_sources,
                    corpus_ids=request.corpus_ids,
                    retrieval_tier=getattr(
                        retrieval, "effective_tier", request.retrieval_tier
                    ),
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
                    precomputed_facets=precomputed_coverage_facets,
                    support_semaphore=shared_support_semaphore,
                    shelf_reserve_concepts=shelf_reserve_concepts,
                ),
                _enforce_evidence_plan_lanes(
                    original_query=request.message,
                    sources=retrieval_sources,
                    evidence_plan=evidence_plan,
                    corpus_ids=request.corpus_ids,
                    retrieval_tier=getattr(
                        retrieval, "effective_tier", request.retrieval_tier
                    ),
                    collections=request.collections,
                    retrieval_k=profile_k,
                    top_k_summary=profile_cfg["top_k_summary"],
                    rerank_top_n=profile_cfg["rerank_top_n"],
                    similarity_threshold=profile_cfg["similarity_threshold"],
                    neo4j_expansion_cap=profile_cfg["neo4j_expansion_cap"],
                    max_corpora_per_query=profile_cfg["max_corpora_per_query"],
                    fact_seed_limit=profile_cfg["fact_seed_limit"],
                    final_top_k=profile_cfg["final_top_k"],
                    source_cap=profile_cfg.get("source_cap"),
                    support_semaphore=shared_support_semaphore,
                    enabled=relationship_evidence_allocation,
                ),
            )
            coverage_meta["duration_s"] = perf_counter() - coverage_start
            coverage_meta["parallel_with_evidence_plan"] = True
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
            # Union of the two parallel passes: evidence-plan output first
            # (its lane-marked support chunks must survive), then any
            # coverage-added chunks not already present.
            _merged_ids = {str(chunk.chunk_id or "") for chunk in evidence_only_sources}
            evidence_sources = list(evidence_only_sources)
            for chunk in coverage_sources:
                _cid = str(chunk.chunk_id or "")
                if _cid and _cid in _merged_ids:
                    continue
                _merged_ids.add(_cid)
                evidence_sources.append(chunk)
            # v4 scoring-wall — support chunks scored DETERMINISTICALLY, no 2nd
            # CE pass. Support picks are chosen by facet-fit / lane-match
            # heuristics, NOT the cross-encoder. A second CE pass to re-score
            # them cost ~5s on the serialized Metal GPU (measured 2026-07-02)
            # and, when it timed out, leaked raw lexical scores (Expert-Systems
            # at 194) into the packet. Instead we treat support additions as
            # SUPPLEMENTARY: cap their score into a band strictly BELOW the
            # CE-confirmed main evidence, so they fill genuine gaps but can
            # never outrank a chunk the authority actually scored. Reserved
            # side-guarantee seats are protected by curation's per-side
            # reservation regardless of score, so coverage is preserved.
            #   _SUPPORT_SCORE_CAP = 0.50  (< typical strong CE ~0.6-0.95)
            #   raw >1.0 (never CE-scored) -> 0.10 (junk floor)
            _base_ids = {str(c.chunk_id or "") for c in retrieval_sources}
            _support_capped = 0
            for _chunk in evidence_sources:
                _cid = str(_chunk.chunk_id or "")
                try:
                    _s = float(_chunk.score or 0.0)
                except (TypeError, ValueError):
                    _s = 0.0
                if _s > 1.0:  # raw lexical/BM25 leak — never faced the authority
                    _chunk.score = 0.10
                    _support_capped += 1
                elif _cid not in _base_ids and _s > _SUPPORT_SCORE_CAP:
                    # Heuristic-scored support add sitting in the CE band — cap
                    # it below CE-confirmed evidence.
                    _chunk.score = _SUPPORT_SCORE_CAP
                    _support_capped += 1
            evidence_plan_meta["support_scores_capped"] = _support_capped
            evidence_plan_meta["duration_s"] = perf_counter() - evidence_plan_start
            evidence_plan_meta["parallel_with_coverage"] = True
        if evidence_plan_meta.get("active"):
            if evidence_plan_meta.get("added") or evidence_plan_meta.get(
                "missing_lanes"
            ):
                logger.info(
                    "chat_evidence_plan: mode=%s added=%s covered=%s missing=%s docs=%s",
                    evidence_plan_meta.get("mode"),
                    evidence_plan_meta.get("added"),
                    evidence_plan_meta.get("covered_lanes"),
                    evidence_plan_meta.get("missing_lanes"),
                    evidence_plan_meta.get("support_doc_ids"),
                )
            yield _record_trace_event(
                lane="planning",
                title="Evidence plan",
                status=(
                    "done" if not evidence_plan_meta.get("missing_lanes") else "warning"
                ),
                content=_format_evidence_plan_trace(evidence_plan_meta),
                metadata=evidence_plan_meta,
            )
        sources = _dedupe_sources_for_context(evidence_sources)
        # Per-document cap. For a genuine multi-side question, no single book may
        # contribute more than one side's worth of evidence — this is what turns
        # "4 of 5 chunks from the title-matching book" into a balanced packet.
        # Reserved per-side support chunks are protected so the cap trims the
        # dominant book, not the evidence we added on purpose. Single-side /
        # non-plan queries fall back to the legacy universal guard (a no-op
        # unless _CHAT_PER_DOC_CAP is configured), so ordinary answers are
        # unchanged.
        relationship_evidence_allocation = bool(
            settings.RELATIONSHIP_EVIDENCE_ALLOCATION_ENABLED
            and _relationship_allocation_eligible(evidence_plan)
        )
        _evidence_doc_cap = (
            _evidence_per_doc_cap_for_plan(evidence_plan, budget=len(sources))
            if relationship_evidence_allocation
            else 0
        )
        if _evidence_doc_cap > 0:
            sources = _ea_cap_chunks_per_doc(
                sources,
                cap=_evidence_doc_cap,
                doc_id_fn=lambda s: str(getattr(s, "doc_id", "") or ""),
                protect_fn=_is_reserved_support_chunk,
            )
        else:
            sources = _cap_chunks_per_doc(sources)
        retrieval_diagnostics = getattr(retrieval, "diagnostics", {}) or {}
        required_planned_lane_ids = list(
            (retrieval_diagnostics.get("required_concept_coverage") or {}).get(
                "required_lane_ids"
            )
            or []
        )
        (
            sources,
            answerability_chunk_gate_meta,
        ) = _apply_final_context_answerability_gate(
            sources,
            query=request.message,
            evidence_plan=evidence_plan,
            required_planned_lane_ids=required_planned_lane_ids,
            search_mode=resolved_mode,
        )
        sources = _filter_sources_to_selected_corpora(sources, request.corpus_ids)
        # Exact-sentence candidate anchors are attached strictly after final
        # answerability/corpus selection. Keep the sealed evidence packet so
        # any non-anchor mutation fails closed.
        atomic_claim_anchor_meta: dict[str, Any] = {
            "enabled": False,
            "aggregate_calls": 0,
        }
        if settings.ATOMIC_CLAIM_ANCHORS_ENABLED and sources:
            final_selected_sources = list(sources)
            try:
                from services.ingestion_service import ingestion_service
                from services.retriever.atomic_claim_anchors import (
                    maybe_attach_atomic_claim_anchors,
                    source_additivity_receipt,
                )

                selected_receipt = source_additivity_receipt(final_selected_sources)
                claim_db = getattr(ingestion_service, "db", None)
                if claim_db is not None:
                    (
                        sources,
                        atomic_claim_anchor_meta,
                    ) = await maybe_attach_atomic_claim_anchors(
                        claim_db,
                        sources,
                        query=request.message,
                    )
                    enriched_receipt = source_additivity_receipt(sources)
                    if (
                        selected_receipt["source_ids"] != enriched_receipt["source_ids"]
                        or selected_receipt["non_anchor_evidence_sha256"]
                        != enriched_receipt["non_anchor_evidence_sha256"]
                        or selected_receipt["non_anchor_evidence_bytes"]
                        != enriched_receipt["non_anchor_evidence_bytes"]
                    ):
                        sources = final_selected_sources
                        atomic_claim_anchor_meta.update(
                            {
                                "reason": (
                                    "orchestrator_final_selection_additivity_failed"
                                ),
                                "source_identity_additive": False,
                                "non_anchor_evidence_additive": False,
                                "additivity_verified": False,
                                "anchors_attached": 0,
                                "sources_anchored": 0,
                            }
                        )
                else:
                    atomic_claim_anchor_meta = {
                        "enabled": True,
                        "aggregate_calls": 0,
                        "reason": "database_unavailable",
                    }
            except Exception as exc:
                sources = final_selected_sources
                logger.warning("Atomic claim anchors skipped: %s", exc)
                atomic_claim_anchor_meta = {
                    "enabled": True,
                    "aggregate_calls": 0,
                    "reason": f"{type(exc).__name__}: {exc}"[:240],
                }
        if answerability_chunk_gate_meta.get("enabled"):
            retrieval_diagnostics[
                "answerability_chunk_gate"
            ] = answerability_chunk_gate_meta
        # P1.5 shelf_reserve: surface the final selector's seat-pass
        # diagnostics (the reading-path seed) into the retrieval trace
        # metadata alongside the corpus floor (selection.corpus_floor).
        _shelf_reserve_meta = (coverage_meta.get("final_selector") or {}).get(
            "shelf_reserve"
        )
        if _shelf_reserve_meta:
            retrieval_diagnostics["shelf_reserve"] = _shelf_reserve_meta
        retrieval_diagnostics["atomic_claim_anchors"] = atomic_claim_anchor_meta
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
                "retrieval_diagnostics": retrieval_diagnostics,
                "answerability_chunk_gate": answerability_chunk_gate_meta,
                "atomic_claim_anchors": atomic_claim_anchor_meta,
                "coverage_detected_facets": coverage_meta.get("detected_facets", []),
                "coverage_query_facet_breakdown": coverage_meta.get(
                    "query_facet_breakdown", []
                ),
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
                "coverage_support_search_mode": coverage_meta.get(
                    "support_search_mode"
                ),
                "coverage_support_doc_ids": coverage_meta.get("support_doc_ids", []),
                "coverage_duration_s": coverage_meta.get("duration_s", 0.0),
                "evidence_filtered_low_value": coverage_meta.get(
                    "filtered_low_value", 0
                ),
                "evidence_cleaned_frontmatter": coverage_meta.get(
                    "cleaned_frontmatter", 0
                ),
                "coverage_priority_lanes": coverage_meta.get(
                    "coverage_priority_lanes", []
                ),
                "coverage_uncovered_priority_lanes": coverage_meta.get(
                    "coverage_uncovered_priority_lanes", []
                ),
                "coverage_lane_counts": coverage_meta.get("coverage_lane_counts", {}),
                "coverage_uncovered_lanes": coverage_meta.get(
                    "coverage_uncovered_lanes", []
                ),
                "coverage_lane_reports": coverage_meta.get("lane_reports", []),
                "evidence_plan": evidence_plan_meta.get("plan", {}),
                "evidence_plan_added": evidence_plan_meta.get("added", 0),
                "evidence_plan_required_lanes": evidence_plan_meta.get(
                    "required_lanes", []
                ),
                "evidence_plan_covered_lanes": evidence_plan_meta.get(
                    "covered_lanes", []
                ),
                "evidence_plan_missing_lanes": evidence_plan_meta.get(
                    "missing_lanes", []
                ),
                "evidence_plan_lane_reports": evidence_plan_meta.get(
                    "lane_reports", []
                ),
                "evidence_plan_duration_s": evidence_plan_meta.get("duration_s", 0.0),
            },
        )
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
        facts = _filter_facts_to_selected_corpora(facts, request.corpus_ids)
        # Deterministic FACTS counter for the UI — the real number of graph facts
        # seeded into the answer context (post tier-gating), taken straight from
        # the retrieval result. Never an LLM-authored value, so it cannot lie.
        facts_seeded = len(facts)

        answerability_gate = _build_retrieval_answerability_gate(
            query=request.message,
            diagnostics=retrieval_diagnostics,
            sources=sources,
            facts=facts,
            corpus_ids=request.corpus_ids,
            web_search_enabled=web_search_enabled,
            evidence_plan_meta=evidence_plan_meta,
        )
        yield _record_trace_event(
            lane="planning",
            title="Answerability gate",
            status=(
                "done"
                if answerability_gate.get("status") in {"answerable", "not_enforced"}
                else "warning"
            ),
            content=_format_retrieval_answerability_trace(answerability_gate),
            metadata=answerability_gate,
        )

        # Pt 10d (Cluster 2 — Graph Decoration) — graph-tier-only
        # post-retrieval enrichment. Fast Search and Hybrid Search never call Neo4j
        # here. When facts already answer the query, decoration is redundant,
        # so skip the extra traversal.
        decoration: list = []
        if graph_context_enabled:
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
                # GERG: ALWAYS decorate on the graph tier. The old `facts>=3`
                # skip suppressed typed-edge evidence in exactly the regime
                # where graph seeding is strongest (facts=12 on this corpus).
                # The edges are QUERY-RANKED (query=...) so only query-relevant
                # typed relations survive — un-gating cannot flood the prompt
                # with confidence-DESC catalog noise, and on a query with no
                # matching typed structure the decoration is correctly empty.
                decoration_started = perf_counter()
                decoration = await _graph_decorator.decorate_winners(
                    winning_chunks=sources,
                    corpus_ids=request.corpus_ids,
                    wanted_families=None,  # v1: no QueryFacets yet — accept all families
                    neighbor_limit=(
                        getattr(settings, "GRAPH_DECORATE_MAX_PATHS_PER_CHUNK", 3)
                        * getattr(settings, "GRAPH_DECORATE_MAX_CHUNKS", 8)
                    ),
                    chunks_per_neighbor=getattr(
                        settings,
                        "GRAPH_DECORATE_EVIDENCE_CHUNKS_PER_PATH",
                        2,
                    ),
                    db=_db_for_decoration,
                    query=request.message,
                )
                decoration_ms = (perf_counter() - decoration_started) * 1000
                retrieval_diagnostics.setdefault("counts", {})[
                    "graph_decorations"
                ] = len(decoration)
                retrieval_diagnostics.setdefault("timings_s", {})[
                    "graph_decoration"
                ] = (decoration_ms / 1000.0)
                logger.info(
                    "Graph decoration final-only: ms=%.1f chunks=%d arrows=%d",
                    decoration_ms,
                    min(
                        len(sources),
                        int(getattr(settings, "GRAPH_DECORATE_MAX_CHUNKS", 8)),
                    ),
                    len(decoration),
                )
            except Exception as exc:
                logger.warning("Graph decoration skipped: %s", exc)
                decoration = []

        retrieval_nuance_digest = _build_retrieval_nuance_digest(
            tier=retrieval.effective_tier,
            query=request.message,
            sources=sources,
            facts=facts,
            decoration=decoration,
            diagnostics=retrieval_diagnostics,
        )
        if graph_context_enabled:
            graph_counts = retrieval_diagnostics.get("counts") or {}
            graph_timings = retrieval_diagnostics.get("timings_s") or {}
            graph_entity_names = {
                str(value).strip()
                for value in [
                    *[getattr(fact, "subject", "") for fact in facts or []],
                    *[getattr(edge, "seed_entity", "") for edge in decoration or []],
                    *[
                        getattr(edge, "neighbor_entity", "")
                        for edge in decoration or []
                    ],
                ]
                if str(value).strip()
            }
            graph_advantage = {
                "entities_resolved": len(graph_entity_names),
                "facts_used": len(facts),
                "relations_used": len(decoration),
                "evidence_paths": len(decoration),
                "graph_expanded_chunks": graph_counts.get("graph_expanded", 0),
                "final_chunks": len(sources or []),
                "final_docs": len(
                    {
                        str(getattr(source, "doc_id", "") or "")
                        for source in sources or []
                        if getattr(source, "doc_id", None)
                    }
                ),
                "timing_s": {
                    "graph": graph_timings.get("graph", 0.0),
                    "rerank": graph_timings.get("rerank", 0.0),
                    "graph_decoration": graph_timings.get("graph_decoration", 0.0),
                },
            }
            graph_signal_count = (
                int(graph_advantage["facts_used"] or 0)
                + int(graph_advantage["relations_used"] or 0)
                + int(graph_advantage["graph_expanded_chunks"] or 0)
            )
            advantage_established = graph_signal_count > 0
            graph_advantage["advantage_established"] = advantage_established
            graph_advantage["why_better_than_hybrid"] = (
                [
                    "Added Neo4j fact/entity evidence to the hybrid seed pool",
                    "Expanded only from bounded top hybrid chunks",
                    "Decorated only final selected chunks with graph relations",
                    "Verified final answer context through Mongo-hydrated chunks",
                ]
                if advantage_established
                else [
                    "No graph-specific fact, relation, or expansion was established",
                    "Returned only source-backed hybrid evidence without inventing an edge",
                ]
            )
            trace_title = (
                "Graph Advantage" if advantage_established else "Graph Augmentation"
            )
            yield _record_trace_event(
                lane="retrieval",
                title=trace_title,
                status="done" if advantage_established else "degraded",
                content=(
                    f"{trace_title}\n"
                    f"facts={graph_advantage['facts_used']} · "
                    f"relations={graph_advantage['relations_used']} · "
                    f"expanded_chunks={graph_advantage['graph_expanded_chunks']} · "
                    f"final_docs={graph_advantage['final_docs']} · "
                    f"decoration={float(graph_advantage['timing_s']['graph_decoration'] or 0.0):.2f}s"
                ),
                metadata=graph_advantage,
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

        if _should_short_circuit_answerability(
            answerability_gate,
            web_search_enabled=web_search_enabled,
            selected_tools=request.selected_tools,
        ):
            assistant_content = _format_answerability_short_circuit_response(
                answerability_gate,
                query=request.message,
                sources=sources,
            )
            user_saved = await conversation_service.append_message(
                str(conversation_id),
                self._create_user_message(
                    request.message,
                    model_used,
                    request.attachments,
                ),
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
            yield _record_trace_event(
                lane="final",
                title="Assistant final answer",
                status="done",
                content=(
                    "Answerability gate completed the turn without a chat "
                    "model call because selected corpus evidence was "
                    f"{answerability_gate.get('status')}."
                ),
                metadata={
                    "model_skipped": True,
                    "answerability": answerability_gate,
                    "content_chars": len(assistant_content),
                },
            )
            yield build_sse_chunk(
                ChatChunk(
                    type="token",
                    content=assistant_content,
                    conversation_id=str(conversation_id),
                )
            )
            try:
                await self._save_assistant_message(
                    conversation_id,
                    assistant_content,
                    None,
                    model_used,
                    False,
                    chunks_returned=chunks_returned,
                    facts_seeded=facts_seeded,
                    strategy_used=strategy_used,
                    query_profile_used=query_profile_used,
                    reasoning_mode_used=reasoning_mode_used,
                    hyde_applied=hyde_applied,
                    agentic_mode_used=agentic_mode_used,
                    downgrade_reason=downgrade_reason,
                    collections_queried=collections_queried_for_msg,
                    skills_used=[],
                    tools_used=[],
                    reasoning_cascade_applied=False,
                    sources=sources,
                    trace_events=trace_events,
                )
            except Exception as exc:
                logger.error(
                    "Failed to persist answerability-gated assistant message for %s: %s",
                    conversation_id,
                    exc,
                )
                yield build_sse_chunk(
                    ChatChunk(
                        type="error",
                        content=(
                            "The retrieval answerability result was generated, "
                            "but the backend could not save it. Please retry."
                        ),
                        conversation_id=str(conversation_id),
                    )
                )
                return
            yield build_sse_chunk(
                ChatChunk(
                    type="done",
                    conversation_id=str(conversation_id),
                    model_used=model_used,
                    chunks_returned=chunks_returned,
                    facts_seeded=facts_seeded,
                    strategy_used=strategy_used,
                    query_profile_used=query_profile_used,
                    reasoning_mode_used=reasoning_mode_used,
                    hyde_applied=hyde_applied,
                    agentic_mode_used=agentic_mode_used,
                    downgrade_reason=downgrade_reason,
                    collections_queried=collections_queried_for_msg,
                    skills_used=[],
                    tools_used=[],
                    reasoning_cascade_applied=False,
                )
            )
            logger.info(
                "Chat answerability short-circuited conv=%s status=%s chunks=%d",
                conversation_id,
                answerability_gate.get("status"),
                chunks_returned,
            )
            return

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
        # Tracks whether the OPT-IN reasoning cascade actually executed this
        # turn. It must NOT be inferred from `analysis_text` later, because
        # analysis_text is reused below to carry the always-on synthesis-lens +
        # nuance-digest blocks — so `bool(analysis_text)` is true on nearly every
        # turn and would falsely report "cascade applied" with no toggle set.
        cascade_ran = False
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
                    chat_api_base=profile_creds.get("api_base")
                    if profile_creds
                    else None,
                    chat_api_key=profile_creds.get("api_key")
                    if profile_creds
                    else None,
                    chat_extra_params=profile_creds.get("extra_params")
                    if profile_creds
                    else None,
                )
                # Captured BEFORE analysis_text is merged with the always-on
                # synthesis-lens / nuance blocks, so the signal reflects the
                # cascade itself, not those blocks.
                cascade_ran = bool(analysis_text and analysis_text.strip())
            except Exception as exc:
                logger.warning("Reasoning cascade failed: %s", exc)

        answerability_prompt_note = _format_retrieval_answerability_prompt_note(
            answerability_gate
        )
        evidence_plan_prompt_note = _format_evidence_plan_prompt_note(
            evidence_plan_meta
        )
        coverage_prompt_note = _format_chat_coverage_prompt_note(coverage_meta)
        analysis_blocks = [
            block
            for block in (
                analysis_text,
                synthesis_lens_contract,
                retrieval_nuance_contract,
                evidence_plan_prompt_note,
                answerability_prompt_note,
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

                if not _should_skip_inline_decoration_fn(
                    reasoning_mode, reasoning_blend
                ):
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
        if (
            active_skills_dicts
            and len(active_skills_dicts) > skill_count_before_code_lane
        ):
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
                attachments,
                model_used,
            )
            if attachment_tokens > 0:
                user_message.token_count = (
                    user_message.token_count or 0
                ) + attachment_tokens
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
                    f'<attached_file name="{att.filename}" '
                    f'mime="{att.mime_type}">\n{body_text}{marker}\n</attached_file>'
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

        prompt_budget_meta: dict[str, Any] = {}
        if sources or facts or active_skills_dicts or analysis_text:
            # W2 §10.3 — the waterfall packet replaces the CORPUS context
            # render only; web-blended turns keep the legacy per-source loop
            # (the packet was allocated from corpus chunks alone).
            _wf_packet = None
            try:
                _wf_packet = getattr(retrieval, "packet", None)
            except NameError:  # no corpus retrieval this turn (web/pure chat)
                _wf_packet = None
            if _wf_packet and any(
                str(getattr(s, "source_tier", "") or "") == "web_search"
                for s in sources
            ):
                _wf_packet = None
            user_message.content, prompt_budget_meta = _build_budgeted_augmented_prompt(
                query=user_message.content,
                sources=sources,
                facts=facts,
                corpus_ids=request.corpus_ids,
                reasoning_mode=reasoning_mode,
                reasoning_blend=reasoning_blend,
                active_skills=active_skills_dicts or None,
                analysis=analysis_text,
                decoration=inline_decoration,
                model=model_used,
                packet=_wf_packet,
            )
            user_message.token_count = count_tokens(user_message.content, model_used)
            if settings.ATOMIC_CLAIM_ANCHORS_ENABLED:
                atomic_claim_anchor_meta["prompt_render_count"] = int(
                    prompt_budget_meta.get("atomic_claim_anchor_render_count") or 0
                )
                yield _record_trace_event(
                    lane="planning",
                    title="Atomic claim anchors",
                    status=(
                        "done"
                        if atomic_claim_anchor_meta["prompt_render_count"] > 0
                        else "warning"
                    ),
                    content=(
                        "Exact-sentence candidate anchors were evaluated after "
                        "final prompt compaction."
                    ),
                    metadata=atomic_claim_anchor_meta,
                )
            if prompt_budget_meta.get("compacted"):
                warning = (
                    "SYSTEM_WARN: Current RAG context compacted before model call "
                    f"({prompt_budget_meta.get('before_tokens')} -> "
                    f"{prompt_budget_meta.get('after_tokens')} tokens; "
                    f"budget={prompt_budget_meta.get('budget_tokens')})."
                )
                logger.warning(
                    "%s shape=%s hard_clipped=%s over_budget=%s",
                    warning,
                    prompt_budget_meta.get("shape"),
                    prompt_budget_meta.get("hard_clipped"),
                    prompt_budget_meta.get("over_budget_after_compaction"),
                )
                yield _record_trace_event(
                    lane="planning",
                    title="Context Budget",
                    status=(
                        "warning"
                        if prompt_budget_meta.get("over_budget_after_compaction")
                        else "done"
                    ),
                    content=warning,
                    metadata=prompt_budget_meta,
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
        prompt_compacted = bool(prompt_budget_meta.get("compacted"))
        effective_trimming_applied = bool(trimming_applied or prompt_compacted)
        if prompt_compacted:
            prompt_compaction_details = (
                "Current RAG context compacted before model call: "
                f"{prompt_budget_meta.get('before_tokens')} -> "
                f"{prompt_budget_meta.get('after_tokens')} tokens "
                f"(budget {prompt_budget_meta.get('budget_tokens')})."
            )
            trimming_details = (
                f"{prompt_compaction_details} {trimming_details}"
                if trimming_applied
                else prompt_compaction_details
            )

        # Always emit a budget frame so the UI can render "X / Y tokens"
        yield build_sse_chunk(
            ChatChunk(
                type="budget",
                conversation_id=str(conversation_id),
                tokens_used=tokens_used_post_trim,
                tokens_max=tokens_max,
                trimming_applied=effective_trimming_applied,
            )
        )

        # Send trimming notification if history or the current RAG prompt was compacted.
        if effective_trimming_applied:
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
            self._create_user_message(
                request.message,
                model_used,
                request.attachments,
            ),
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
                            data_url = f"data:{att.mime_type};base64,{att.content}"
                            content_blocks.append(
                                {
                                    "type": "image_url",
                                    "image_url": {"url": data_url},
                                }
                            )
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
                        chunk.get("content")
                        or chunk.get("thinking")
                        or chunk.get("tool_calls")
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
                if not assistant_content.strip() and not tool_calls:
                    fallback = await _resolve_chat_fallback(
                        user_id,
                        primary_model=model_used,
                        primary_entry_id=primary_entry_id,
                    )
                    try:
                        if fallback:
                            logger.warning(
                                "stream fallback %s -> %s source=%s",
                                model_used,
                                fallback["model"],
                                "configured_pool"
                                if fallback.get("entry_id")
                                else "static",
                            )
                            async for fb in llm_service.stream_chat(
                                messages=message_dicts,
                                model=fallback["model"],
                                overrides=None,
                                tools=None,
                                api_base=fallback.get("api_base"),
                                api_key=fallback.get("api_key"),
                                extra_params=fallback.get("extra_params"),
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

            (
                response_text,
                response_call,
                non_response_tool_calls,
            ) = _extract_response_tool_text(tool_calls)
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
                                    "id": response_call.get("id")
                                    or "response_before_web",
                                    "type": response_call.get("type") or "function",
                                    "function": response_call.get("function") or {},
                                }
                            ],
                        }
                    )
                    react_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": response_call.get("id")
                            or "response_before_web",
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
                            or assistant_tool_calls[
                                min(i, len(assistant_tool_calls) - 1)
                            ]["id"]
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
                sources = _filter_sources_to_selected_corpora(
                    sources,
                    request.corpus_ids,
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
                if not assistant_content.strip():
                    fallback = await _resolve_chat_fallback(
                        user_id,
                        primary_model=model_used,
                        primary_entry_id=primary_entry_id,
                    )
                    try:
                        if fallback:
                            logger.warning(
                                "final-stream fallback %s -> %s source=%s",
                                model_used,
                                fallback["model"],
                                "configured_pool"
                                if fallback.get("entry_id")
                                else "static",
                            )
                            async for fb in llm_service.stream_chat(
                                messages=final_messages,
                                model=fallback["model"],
                                overrides=None,
                                tools=None,
                                api_base=fallback.get("api_base"),
                                api_key=fallback.get("api_key"),
                                extra_params=fallback.get("extra_params"),
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
            fallback = (
                await _resolve_chat_fallback(
                    user_id,
                    primary_model=model_used,
                    primary_entry_id=primary_entry_id,
                )
                if last_generation_messages
                else None
            )
            if fallback and last_generation_messages:
                yield _record_trace_event(
                    lane="model_call",
                    title="Chat model fallback",
                    status="running",
                    content=(
                        "The selected model completed without user-facing "
                        "tokens; retrying once on the fallback chat model."
                    ),
                    metadata={
                        "from_model": model_used,
                        "to_model": fallback["model"],
                        "source": "configured_pool"
                        if fallback.get("entry_id")
                        else "static",
                    },
                )
                fallback_start = perf_counter()
                try:
                    async for fb in llm_service.stream_chat(
                        messages=last_generation_messages,
                        model=fallback["model"],
                        overrides=None,
                        tools=None,
                        api_base=fallback.get("api_base"),
                        api_key=fallback.get("api_key"),
                        extra_params=fallback.get("extra_params"),
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
                            "to_model": fallback["model"],
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
                        metadata={
                            "from_model": model_used,
                            "to_model": fallback["model"],
                        },
                    )

        if not assistant_content.strip():
            logger.error(
                "LLM returned an empty assistant response for %s", conversation_id
            )
            yield _record_trace_event(
                lane="final",
                title="Assistant final answer",
                status="error",
                content="The model returned no user-facing answer after retrieval.",
                metadata={
                    "model_skipped": False,
                    "model": model_used,
                },
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
                    critique = "; ".join(
                        issues[:3]
                    )  # cap at first 3 issues for display
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
                    assistant_content = (
                        f"{assistant_content}\n\n---\n**Revised answer:**\n\n{revised}"
                    )
            except Exception as exc:
                logger.warning(
                    "self_correct post-pass failed (%s) — keeping draft", exc
                )

        # Phase 24 — collect skill/tool/reasoning trust signals for this turn
        skills_used_names = [s["name"] for s in active_skills_dicts]
        final_tools_used = list(tools_used_names)
        reasoning_cascade_applied = cascade_ran
        yield _record_trace_event(
            lane="final",
            title="Assistant final answer",
            status="done",
            content=(
                "Final answer assembled and ready to persist. "
                f"content_chars={len(assistant_content)}"
            ),
            metadata={
                "model_skipped": False,
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
                effective_trimming_applied,
                chunks_returned=chunks_returned,
                facts_seeded=facts_seeded,
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
            logger.error(
                "Failed to persist assistant message for %s: %s", conversation_id, exc
            )
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
                facts_seeded=facts_seeded,
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
        stream_s = (
            (stream_end - first_token_at) if (first_token_at and stream_end) else None
        )
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
        # v4 fetch ladder (owner-directed 2026-07-02): fetch depth is cheap
        # (Qdrant ms-level); recall INTO the rank-fused pool is what feeds
        # the cross-encoder. fast 10->70, balanced 40->100, thorough 60->160.
        "fast": {"retrieval_k": 50, "rerank_enabled": False, "hyde_enabled": False},
        # HyDE is explicit-only. Deterministic phrase-aware planning provides
        # cross-domain decomposition without a pre-retrieval model call or a
        # hypothetical answer that can drift away from the user's wording.
        # v4 P1: pools widened toward the listwise reranker's 64-doc
        # capacity (one forward pass). balanced 24->32, thorough 32->40.
        # Owner budget shape: retrieve 70-200 (cheap Qdrant fetch) -> rerank
        # with the TRUE fp16 cross-encoder -> return 8-16. The fp16 CE is
        # ~3.4s/32 docs on the single contended Metal GPU, and each turn also
        # spends a support-authority CE pass — so the rerank pool is held at
        # 32/40 where the GPU keeps up. The owner's 50-doc pool lands after
        # serving consolidation (task #11, one continuous-batching server).
        "balanced": {
            "retrieval_k": 70,
            "rerank_enabled": True,
            "hyde_enabled": False,
            "rerank_top_n": 32,
        },
        "thorough": {
            "retrieval_k": 120,
            "rerank_enabled": True,
            "hyde_enabled": False,
            "rerank_top_n": 40,
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
            overrides.query_profile
            if overrides and overrides.query_profile
            else "balanced"
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
            "evidence_plan_llm_decompose": False,
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
                extras["evidence_plan_llm_decompose"] = bool(
                    getattr(
                        saved_retrieval_settings,
                        "evidence_plan_llm_decompose",
                        False,
                    )
                )
                # Fact seeding has ONE user-facing knob — graph_fact_seeds (the
                # "Fact seeds" slider; graph-tier scoped, since fact seeding only
                # runs at qdrant_mongo_graph). Source it here for EVERY profile so
                # the saved value always applies (a per-request override still
                # wins). The legacy generic `fact_seed_limit` is just a synced
                # alias (the settings panel mirrors it) kept for back-compat.
                extras["fact_seed_limit"] = getattr(
                    saved_retrieval_settings, "graph_fact_seeds", None
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
                        "fact_seed_limit": getattr(rs, "graph_fact_seeds", None),
                        "source_cap": getattr(
                            rs, "source_cap", _CHAT_COVERAGE_SOURCE_CAP
                        ),
                        "evidence_plan_llm_decompose": bool(
                            getattr(rs, "evidence_plan_llm_decompose", False)
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
        # Global master switch: HyDE is opt-in. Unless HYDE_ENABLED is on, it
        # runs ONLY when the request EXPLICITLY toggled it (hyde_explicit). A
        # profile preset that merely defaults hyde_enabled=True is not enough —
        # HyDE costs an extra pre-retrieval LLM round-trip, so it stays off the
        # hot path until a user asks for it.
        if not settings.HYDE_ENABLED and not hyde_explicit:
            logger.info(
                "HyDE skipped (global default off; not explicitly toggled): '%s'",
                request.message[:80],
            )
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

    async def _llm_decompose_sides(
        self,
        query: str,
        *,
        route: dict[str, Any] | None,
    ) -> list[dict]:
        """Optional LLM decomposition of a query into source sides (E).

        Behind the ``evidence_plan_llm_decompose`` setting (default off). Reuses
        the HyDE model route and the same short-leash completion client. Any
        failure or malformed reply yields ``[]`` so the deterministic plan
        stands — the LLM only proposes the *sides*; documents are still grounded
        downstream from ingestion metadata.
        """

        route = route or {}
        model = route.get("model") or settings.HYDE_MODEL
        if not model:
            return []
        # Keep the instruction TERSE. A verbose prompt makes thinking-capable
        # models (minimax) over-reason and return empty content; the concise
        # form below is reliably answered with clean JSON.
        prompt = (
            "Reply ONLY with JSON of the form "
            '{"sides": [{"name": "topic", "search_terms": ["term", "term", "term"]}]} '
            "splitting the question into its 2-4 distinct evidence topics, each "
            "with 3-6 search terms.\n\nQuestion: " + str(query or "")
        )

        # Thinking-capable HyDE models (e.g. minimax) are high-variance: the
        # same prompt occasionally returns empty content even with an ample
        # budget. RACE the attempts concurrently (ElevenLabs pattern) — first
        # valid decomposition wins — under one hard deadline. The old shape
        # (3 SEQUENTIAL retries x 40s timeout) budgeted up to 120s inside the
        # retrieval block and produced observed 26s turns.
        async def _one_attempt(attempt: int) -> list[dict]:
            try:
                reply = await llm_service.complete_sync(
                    messages=[{"role": "user", "content": prompt}],
                    model=model,
                    # Identical temp-0 racers can collapse to the same failure;
                    # a slightly warmed second racer decorrelates them.
                    temperature=0.0 if attempt == 0 else 0.2,
                    # At 300 tokens a thinking model spends the budget reasoning
                    # and the visible content comes back EMPTY; 800 fits both.
                    max_tokens=800,
                    api_base=route.get("api_base"),
                    api_key=route.get("api_key"),
                    extra_params=route.get("extra_params") or None,
                    timeout=_EVIDENCE_LLM_DECOMPOSE_TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "evidence-plan LLM decomposition attempt %d failed: %s",
                    attempt,
                    exc,
                )
                return []
            sides = parse_llm_sides(reply)
            return sides if len(sides) >= 2 else []

        tasks = [
            asyncio.create_task(_one_attempt(attempt))
            for attempt in range(_EVIDENCE_LLM_DECOMPOSE_ATTEMPTS)
        ]
        try:
            for completed in asyncio.as_completed(
                tasks, timeout=_EVIDENCE_LLM_DECOMPOSE_DEADLINE
            ):
                sides = await completed
                if sides:
                    return sides
        except (asyncio.TimeoutError, TimeoutError):
            logger.info(
                "evidence-plan LLM decomposition hit the %.0fs deadline — "
                "deterministic plan stands",
                _EVIDENCE_LLM_DECOMPOSE_DEADLINE,
            )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        return []

    async def _resolve_evidence_plan(
        self,
        request: ChatRequest,
        *,
        user_id: str | None,
        llm_decompose: bool,
        retrieval_query: str | None = None,
        fallback_model: str | None = None,
        fallback_api_base: str | None = None,
        fallback_api_key: str | None = None,
        fallback_extra: dict | None = None,
    ) -> tuple[EvidencePlan, "asyncio.Task[EvidencePlan | None] | None"]:
        """Build the evidence plan, generalizing beyond the curated concepts (E).

        Returns ``(plan, llm_refinement_task)``. The task is non-None only
        when the LLM decomposer is enabled AND the deterministic plan is
        weak — the caller overlaps it with the main retrieval and awaits it
        just before the evidence-plan pass.

        1. Deterministic plan (curated concepts + token lanes).
        2. If not already multi-side, try the no-LLM heuristic side split
           ("X versus Y", "between A and B") — any corpus, zero added latency.
        3. If still single-side and the LLM decomposer is enabled, ask the model
           to name the sides. Always falls back to the deterministic plan.
        """

        query = retrieval_query or request.message
        query_plan_v2 = build_query_plan_v2(
            request.message,
            corpus_ids=request.corpus_ids,
            standalone_query=query,
        )
        if settings.QUERY_PLAN_V2:
            v2_sides = query_plan_evidence_sides(query_plan_v2)
            if v2_sides:
                return (
                    build_evidence_plan_from_sides(
                        query,
                        v2_sides,
                        allow_single=True,
                    ),
                    None,
                )
        plan = build_evidence_plan(query)
        # A deterministic plan is good as-is only when it already has >=2 sides
        # AND none of them is a bare token lane. A token lane ("metacognition",
        # "self-analysis") retrieves on a generic embedding and can pull
        # cross-domain noise, so a multi-side-but-weak plan is still a candidate
        # for sharper re-decomposition.
        weak = any(
            not is_curated_concept(lane.concept_key) for lane in plan.required_lanes
        )
        if len(plan.required_lanes) >= 2 and not weak:
            return plan, None

        heuristic_sides = split_query_sides(query)
        if len(heuristic_sides) >= 2:
            return build_evidence_plan_from_sides(query, heuristic_sides), None

        if llm_decompose:
            # Speed campaign (2026-07-02): do NOT await the LLM here. The
            # refined plan is consumed only by the evidence-plan pass, which
            # runs AFTER the main retrieval — so the decomposition runs as a
            # background task that hides behind ~5-8s of retrieval work. The
            # caller awaits the task (deadline-bounded) right before the pass
            # and keeps the deterministic plan when the LLM loses the race.
            async def _refine() -> EvidencePlan | None:
                route = await self._resolve_hyde_route(
                    request,
                    user_id=user_id,
                    fallback_model=fallback_model,
                    fallback_api_base=fallback_api_base,
                    fallback_api_key=fallback_api_key,
                    fallback_extra=fallback_extra,
                )
                sides = await self._llm_decompose_sides(query, route=route)
                if len(sides) >= 2:
                    return build_evidence_plan_from_sides(query, sides)
                return None

            return plan, asyncio.create_task(_refine())

        return plan, None

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

    def _create_user_message(
        self,
        message: str,
        model: str,
        attachments: list[Any] | None = None,
    ) -> ChatMessage:
        """Create a user message object without saving it."""
        attachment_receipts = [
            {
                "filename": attachment.filename,
                "mime_type": attachment.mime_type,
                "size_bytes": attachment.size_bytes,
                "kind": attachment.kind,
            }
            for attachment in (attachments or [])
        ]
        return ChatMessage(
            role="user",
            content=message,
            token_count=count_tokens(message, model),
            created_at=datetime.utcnow(),
            metadata={"attachments": attachment_receipts}
            if attachment_receipts
            else {},
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
        facts_seeded: int | None = None,
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
            facts_seeded=facts_seeded,
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
            max_results = int(args.get("max_results") or web_options["max_sources"])
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
                (
                    pass_fetched,
                    pass_fetch_stats,
                    pass_hits_to_fetch,
                    pass_web_pipeline,
                ) = await live_web_search._fetch_pages_for_search(
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
                            hit.published_date
                            if hit
                            else (chunk.metadata or {}).get("published_date")
                        ),
                        "search_query": metadata.get("search_query"),
                        "time_range": metadata.get("time_range"),
                        "full_page_fetched": bool(metadata.get("full_page_fetched")),
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
                        "content_truncated": bool(metadata.get("content_truncated")),
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
                "youtube_transcripts_enabled": bool(web_options["youtube_transcripts"]),
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
                youtube_transcripts_enabled=bool(web_options["youtube_transcripts"]),
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
                    result.text
                    and result.chars >= int(settings.OBSCURA_MAX_CHARS or 4000)
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
                                "source_text_max_chars": int(
                                    settings.OBSCURA_MAX_CHARS or 4000
                                ),
                                "content_truncated": bool(
                                    result.text
                                    and result.chars
                                    >= int(settings.OBSCURA_MAX_CHARS or 4000)
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
